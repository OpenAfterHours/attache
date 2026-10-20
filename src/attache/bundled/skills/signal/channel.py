# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Attaché's Signal channel: a sidecar between Attaché and `signal-cli -a <number> jsonRpc`.

    uv run channel.py                 run as a sidecar (Attaché starts it; docs/channels.md)
    uv run channel.py doctor [+44…]   check java, signal-cli and the account
    uv run channel.py setup [+44…]    print the setup steps from SKILL.md, then run doctor

Settings come from [channels.signal] as environment variables: ATTACHE_NUMBER (required),
ATTACHE_SIGNAL_CLI (a path, or a JSON list of arguments) and ATTACHE_UUID (the bot's ACI,
normally read from `signal-cli listAccounts`). signal-cli keeps the account's keys itself, so
this script stores no secrets.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

NAME = "Attaché"
THUMBS = ("👍", "👎")  # react with these for the first and second option of an ask
UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
STALE_DAYS = 90  # Signal's servers may stop accepting older signal-cli releases
RELEASES = "https://api.github.com/repos/AsamK/signal-cli/releases"
Asks = dict[int, tuple[str, list[str]]]  # sent timestamp -> (ask_id, options)
FAILED = (OSError, ValueError, RuntimeError, subprocess.SubprocessError)  # running signal-cli
JAVA = "winget install EclipseAdoptium.Temurin.25.JRE (Windows), or Java 25 from adoptium.net"


def emit(event: dict[str, Any]) -> None:
    """Write one protocol line to Attaché."""
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def log(level: str, text: str) -> None:
    emit({"type": "log", "level": level, "text": text})


# Translation between signal-cli's JSON and the sidecar protocol. Pure, so tests import them.


def inbound(envelope: dict[str, Any], self_uuid: str) -> dict[str, Any] | None:
    """The `message` event for a signal-cli envelope (files not yet copied), or None to skip."""
    data = envelope.get("dataMessage") or {}
    sender = envelope.get("sourceUuid") or ""
    text = data.get("message") or ""
    if not sender or sender == self_uuid or data.get("reaction"):
        return None
    if not text and not data.get("attachments"):
        return None  # sync, receipt and typing envelopes, group updates, deletions
    text, mentioned = resolve_mentions(text, data.get("mentions") or [], self_uuid)
    replied = bool(self_uuid) and (data.get("quote") or {}).get("authorUuid") == self_uuid
    group = (data.get("groupInfo") or {}).get("groupId")
    return {
        "type": "message",
        "chat_id": group or sender,
        "message_id": f"{envelope.get('timestamp')}:{sender}",
        "sender_id": sender,
        "sender_name": envelope.get("sourceName") or "",
        "text": text,
        "is_group": bool(group),
        "mentioned": mentioned or replied,
        "files": [],
    }


def resolve_mentions(text: str, mentions: list[dict[str, Any]], self_uuid: str) -> tuple[str, bool]:
    """Replace each mention's placeholder (U+FFFC) with "@name"; True if one is the bot."""
    units, mentioned = text.encode("utf-16-le"), False  # Signal counts in UTF-16 code units
    for mention in sorted(mentions, key=lambda m: m.get("start", 0), reverse=True):
        is_bot = bool(self_uuid) and mention.get("uuid") == self_uuid
        mentioned = mentioned or is_bot
        name = NAME if is_bot else (mention.get("number") or mention.get("uuid") or "someone")
        start = 2 * int(mention.get("start", 0))
        end = start + 2 * int(mention.get("length", 1))
        units = units[:start] + f"@{name}".encode("utf-16-le") + units[end:]
    return units.decode("utf-16-le", errors="replace"), mentioned


def send_params(cmd: dict[str, Any]) -> dict[str, Any]:
    """signal-cli `send` params for an Attaché `send` (or `ask`) command."""
    chat = str(cmd["chat_id"])
    direct = UUID.fullmatch(chat) or chat.startswith("+")  # anything else is a group id
    params: dict[str, Any] = {"recipient": [chat]} if direct else {"groupId": chat}
    params["message"] = cmd.get("text") or ""
    if cmd.get("files"):
        params["attachments"] = [str(path) for path in cmd["files"]]
    when, _, author = str(cmd.get("reply_to") or "").partition(":")  # "<timestamp>:<uuid>"
    if when.isdigit() and author:
        params.update(quoteTimestamp=int(when), quoteAuthor=author)
    return params


def ask_text(text: str, options: list[str]) -> str:
    """The question plus how to answer it, since Signal has no buttons."""
    pairs = zip(THUMBS, options, strict=False)
    hint = "React " + ", ".join(f"{thumb} for {option}" for thumb, option in pairs)
    hint += ", or reply with the option"
    if len(options) > 2:
        hint += ": " + ", ".join(f"{n} {option}" for n, option in enumerate(options, 1))
    return f"{text}\n\n{hint}."


def answer_from_reaction(envelope: dict[str, Any], asks: Asks) -> dict[str, Any] | None:
    """An `answer` event when 👍 or 👎 reacts to an ask we sent (by its timestamp), else None."""
    reaction = (envelope.get("dataMessage") or {}).get("reaction") or {}
    ask = asks.get(reaction.get("targetSentTimestamp"))
    if not ask or reaction.get("isRemove"):
        return None
    ask_id, options = ask
    emoji = str(reaction.get("emoji") or "")  # startswith below: 👍🏽 counts as 👍
    chosen = [opt for thumb, opt in zip(THUMBS, options, strict=False) if emoji.startswith(thumb)]
    if not chosen:
        return None
    sender = envelope.get("sourceUuid")
    return {"type": "answer", "ask_id": ask_id, "sender_id": sender, "choice": chosen[0]}


def save_attachments(envelope: dict[str, Any], source: Path, target: Path) -> list[str]:
    """Copy a message's attachments from signal-cli's folder into `target`; absolute paths."""
    saved = []
    for n, item in enumerate((envelope.get("dataMessage") or {}).get("attachments") or []):
        stored = source / Path(str(item.get("id") or "")).name  # signal-cli names files by id
        if not stored.is_file():
            log("warning", f"attachment {item.get('id')!r} is not in {source}; skipped")
            continue
        name = Path(str(item.get("filename") or stored.name)).name  # chosen by the sender
        name = re.sub(r"[^\w.\- ]", "_", name)
        target.mkdir(parents=True, exist_ok=True)
        dest = target / f"{envelope.get('timestamp')}-{n}-{name}"
        shutil.copyfile(stored, dest)
        saved.append(str(dest.resolve()))
    return saved


# Settings, and signal-cli itself.


def number_from(raw: str) -> str:
    return re.sub(r"[\s()-]", "", raw)  # signal-cli wants +447700900123, not "+44 7700 900123"


def data_dir() -> Path:
    """signal-cli's data folder, found the way signal-cli finds it."""
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / "signal-cli"


def signal_cli() -> list[str] | None:
    """The arguments that run signal-cli, or None when it cannot be found."""
    configured = os.environ.get("ATTACHE_SIGNAL_CLI", "").strip()
    if configured.startswith("["):
        argv = [str(part) for part in json.loads(configured)]
    else:
        found = configured or shutil.which("signal-cli") or shutil.which("signal-cli.bat")
        argv = [found] if found else []
    return argv if argv and shutil.which(argv[0]) else None


def java_env() -> dict[str, str]:
    # Piped Java output can use a legacy Windows code page; signal-cli must write UTF-8.
    options = os.environ.get("JAVA_TOOL_OPTIONS", "") + " -Dstdout.encoding=UTF-8"
    return {**os.environ, "JAVA_TOOL_OPTIONS": options.strip()}


def run(argv: list[str], wait: float = 120) -> subprocess.CompletedProcess[str]:
    """Run a command to completion, capturing its output as text."""
    return subprocess.run(
        argv, capture_output=True, encoding="utf-8", errors="replace", env=java_env(), timeout=wait
    )


def bot_aci(cli: list[str], number: str) -> str:
    """The bot's ACI from `signal-cli listAccounts`, or "" if the number is not listed.

    Raises one of FAILED when signal-cli fails, prints something other than JSON, or times
    out (as it does while another signal-cli holds the account)."""
    done = run([*cli, "-o", "json", "listAccounts"])
    if done.returncode:
        raise RuntimeError((done.stderr.strip().splitlines() or [f"exit {done.returncode}"])[-1])
    accounts = json.loads(done.stdout or "[]")
    return next((a.get("aci") or "" for a in accounts if a.get("number") == number), "")


@dataclass
class Bridge:
    """One `signal-cli jsonRpc` process and what the sidecar remembers about it."""

    proc: asyncio.subprocess.Process
    self_uuid: str
    attachments: Path  # signal-cli's attachments folder
    files: Path  # ATTACHE_FILES
    pending: dict[int, tuple[str, Any]] = field(default_factory=dict)  # request id -> why
    asks: Asks = field(default_factory=dict)
    last_id: int = 0

    async def request(self, method: str, params: dict[str, Any], why: tuple[str, Any]) -> None:
        self.last_id += 1
        self.pending[self.last_id] = why
        line = {"jsonrpc": "2.0", "method": method, "params": params, "id": self.last_id}
        self.proc.stdin.write(json.dumps(line).encode("ascii") + b"\n")  # ASCII: any charset
        await self.proc.stdin.drain()

    def on_line(self, line: bytes) -> None:
        """Handle one line from signal-cli: an incoming envelope, or a reply to a request."""
        msg = json.loads(line)  # read_signal logs anything odd, such as a non-object
        if msg.get("method") == "receive":
            envelope = (msg.get("params") or {}).get("envelope") or {}
            if answer := answer_from_reaction(envelope, self.asks):
                emit(answer)
            elif message := inbound(envelope, self.self_uuid):
                message["files"] = save_attachments(envelope, self.attachments, self.files)
                emit(message)
        elif msg.get("id") in self.pending:
            why, info = self.pending.pop(msg["id"])
            sent = (msg.get("result") or {}).get("timestamp")
            if why == "ready":
                emit({"type": "ready", "self_id": self.self_uuid, "name": NAME})
            elif "error" in msg:
                log("error", f"signal-cli could not send: {(msg['error'] or {}).get('message')}")
            elif why == "ask" and sent:
                self.asks[sent] = info


def read_stdin(loop: asyncio.AbstractEventLoop, commands: asyncio.Queue[Any]) -> None:
    """Queue Attaché's commands, then None (a thread, as Windows pipes cannot be awaited)."""
    with contextlib.suppress(RuntimeError):  # the loop closed first: we are exiting anyway
        # The raw stream: a thread blocked in the buffered one crashes interpreter shutdown.
        for line in sys.stdin.buffer.raw:
            with contextlib.suppress(ValueError):
                if isinstance(cmd := json.loads(line), dict):
                    loop.call_soon_threadsafe(commands.put_nowait, cmd)
        loop.call_soon_threadsafe(commands.put_nowait, None)


async def read_signal(bridge: Bridge) -> None:
    async for line in bridge.proc.stdout:
        try:
            bridge.on_line(line)
        except Exception as e:  # one odd line must not stop the channel
            log("warning", f"skipped a signal-cli line ({e!r}): {line[:200]!r}")


async def serve(bridge: Bridge, commands: asyncio.Queue[Any]) -> None:
    """Carry out Attaché's commands until `stop` or the end of stdin. `typing` is ignored."""
    while (cmd := await commands.get()) is not None:
        kind = cmd.get("type")
        try:
            if kind == "send":
                await bridge.request("send", send_params(cmd), ("send", None))
            elif kind == "ask":
                params = send_params({**cmd, "text": ask_text(cmd["text"], cmd["options"])})
                await bridge.request("send", params, ("ask", (cmd["ask_id"], cmd["options"])))
            elif kind == "stop":
                return
        except (KeyError, TypeError, ValueError, OSError) as e:
            log("error", f"could not {kind}: {e!r}")


async def sidecar(cli: list[str], number: str, self_uuid: str) -> int:
    """Run signal-cli until Attaché says stop (0) or signal-cli exits (non-zero)."""
    pipe = asyncio.subprocess.PIPE
    proc = await asyncio.create_subprocess_exec(
        *cli, "-a", number, "jsonRpc", stdin=pipe, stdout=pipe, env=java_env(), limit=2**24
    )
    files = Path(os.environ.get("ATTACHE_FILES") or "inbox")
    bridge = Bridge(proc, self_uuid, data_dir() / "attachments", files)
    commands: asyncio.Queue[Any] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    threading.Thread(target=read_stdin, args=(loop, commands), daemon=True).start()
    with contextlib.suppress(OSError):  # signal-cli may have quit already; read_signal sees it
        await bridge.request("version", {}, ("ready", None))  # the reply means it is up
    signal = asyncio.create_task(read_signal(bridge))
    attache = asyncio.create_task(serve(bridge, commands))
    await asyncio.wait({signal, attache}, return_when=asyncio.FIRST_COMPLETED)
    proc.stdin.close()  # signal-cli exits at the end of its input
    try:
        code = await asyncio.wait_for(proc.wait(), 4)
    except TimeoutError:
        proc.kill()
        code = await proc.wait()
    if attache.done():  # Attaché said stop, or closed stdin
        return 0
    log("error", f"signal-cli exited with code {code}; its own message is in the log")
    return code or 1


# doctor and setup.


def report(status: str, text: str) -> bool:
    print(f"{status:<4}  {text}", flush=True)
    return status != "FAIL"


def check_java() -> bool:
    java = shutil.which("java")
    if not java:
        return report("FAIL", f"java: not found on PATH. Install Java 25: {JAVA}")
    found = re.search(r'^.*version "(\d+).*$', run([java, "-version"], 30).stderr, re.M)
    line = found[0].strip() if found else f"{java} did not say its version"
    if not found or int(found[1]) < 25:
        return report("FAIL", f"java: {line}. signal-cli needs Java 25: {JAVA}")
    return report("ok", f"java: {line}")


def check_version(cli: list[str] | None) -> bool:
    if not cli:
        return report("FAIL", "signal-cli: not found on PATH (SKILL.md step 3)")
    try:
        done = run([*cli, "--version"], 60)
    except FAILED as e:
        return report("FAIL", f"signal-cli ({cli[0]}) did not run: {e}")
    found = re.search(r"\d+\.\d+\.\d+", done.stdout)
    if not found:
        error = (done.stderr or done.stdout).strip()[-300:]
        return report("FAIL", f"signal-cli ({cli[0]}) did not run: {error}")
    text = f"signal-cli {found[0]} ({cli[0]})"
    if not (released := release_date(found[0])):
        return report("ok", f"{text}; release date unknown (offline?)")
    age = (date.today() - released).days
    if age > STALE_DAYS:
        return report("WARN", f"{text} is {age} days old and may stop working: update it")
    return report("ok", f"{text}, released {released}")


def check_account(cli: list[str], number: str) -> bool:
    if not number:
        return report("FAIL", "account: no number given; run: doctor +447700900123")
    try:
        aci = bot_aci(cli, number)
    except FAILED as e:
        return report("FAIL", f"account {number}: listAccounts failed (is Attaché running?): {e}")
    if not aci:
        return report("FAIL", f"account {number} is not registered (SKILL.md step 4)")
    return report("ok", f"account {number} is registered; the bot's Signal id is {aci}")


def doctor(args: list[str]) -> int:
    """Check java, signal-cli and the account, saying what to fix. 0 when nothing failed."""
    java_ok = check_java()
    cli = signal_cli()
    if not check_version(cli) or not cli:  # check_version also reports a missing signal-cli
        return 1
    number = number_from(args[0] if args else os.environ.get("ATTACHE_NUMBER", ""))
    return 0 if check_account(cli, number) and java_ok else 1


def release_date(version: str) -> date | None:
    """When a signal-cli version was released, from GitHub; None when offline."""
    try:
        with urllib.request.urlopen(f"{RELEASES}/tags/v{version}", timeout=5) as response:
            return datetime.fromisoformat(json.load(response)["published_at"]).date()
    except (OSError, ValueError, KeyError, TypeError):
        return None


def setup_steps() -> str:
    """The "## Setup" section of SKILL.md, so the steps live in one place."""
    text = Path(__file__).with_name("SKILL.md").read_text(encoding="utf-8")
    return text.split("## Setup", 1)[-1].split("\n## ", 1)[0].strip()


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # the protocol is UTF-8 on every platform
    command = argv[1] if len(argv) > 1 else "run"
    if command == "setup":
        print(f"Setting up Signal for Attaché\n\n{setup_steps()}\n\nChecking:", flush=True)
    if command in ("setup", "doctor"):
        return doctor(argv[2:])
    number = number_from(os.environ.get("ATTACHE_NUMBER", ""))
    if not number:
        log("error", 'no number: add number = "+447700900123" under [channels.signal]')
        return 2
    cli = signal_cli()
    if not cli:
        log("error", "signal-cli not found: add it to PATH, or set signal_cli in [channels.signal]")
        return 1
    self_uuid = os.environ.get("ATTACHE_UUID", "")
    if not self_uuid:
        try:
            self_uuid = bot_aci(cli, number)
        except FAILED as e:
            log("warning", f"signal-cli listAccounts failed: {e}")
    if not self_uuid:
        log("warning", f"no Signal id for {number}, so group mentions go unnoticed; run doctor")
    return asyncio.run(sidecar(cli, number, self_uuid))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
