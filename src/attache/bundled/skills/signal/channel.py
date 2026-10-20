# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Attaché's Signal channel: a sidecar between Attaché and `signal-cli -a <number> jsonRpc`.

    uv run channel.py                 run as a sidecar (Attaché starts it; docs/channels.md)
    uv run channel.py doctor [+44…]   check java, signal-cli and the account (SKILL.md: setup)

Settings come from [channels.signal] as environment variables: ATTACHE_NUMBER (required),
ATTACHE_SIGNAL_CLI (the program's path, if it is not on PATH) and ATTACHE_UUID (the bot's ACI,
normally read from `signal-cli listAccounts`). signal-cli keeps the account's keys itself, so
this script stores no secrets. Without a number or a working signal-cli it says why on stderr
and waits for `stop`, so Attaché does not restart it over and over.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
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
E164 = re.compile(r"\+[1-9]\d{6,14}")  # a phone number: the only kind passed to signal-cli.bat
UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
MAX_FILE = 25 * 1024 * 1024  # attachments bigger than this are not copied
RELEASES = "https://api.github.com/repos/AsamK/signal-cli/releases"
Asks = dict[int, tuple[str, list[str]]]  # sent timestamp -> (ask_id, options)
Names = dict[str, str]  # sender uuid -> the name signal-cli gave them (sourceName)
FAILED = (OSError, ValueError, RuntimeError, subprocess.SubprocessError)  # running signal-cli
HERE = Path(__file__).resolve().parent  # the skill folder: a signal-cli here is never run
JAVA = "winget install EclipseAdoptium.Temurin.25.JRE (Windows), or Java 25 from adoptium.net"


def emit(event: dict[str, Any]) -> None:
    """Write one protocol line to Attaché."""
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def log(level: str, text: str) -> None:
    emit({"type": "log", "level": level, "text": text})


# Translation between signal-cli's JSON and the sidecar protocol. Pure, so tests import them.


def inbound(envelope: dict[str, Any], self_uuid: str, names: Names) -> dict[str, Any] | None:
    """The `message` event for a signal-cli envelope (files not yet copied), or None to skip.
    `names` maps uuids to the names signal-cli gave their senders, for other people's mentions."""
    data = envelope.get("dataMessage") or {}
    sender = envelope.get("sourceUuid") or ""
    text = data.get("message") or ""
    if not sender or sender == self_uuid or data.get("reaction"):
        return None
    if not text and not data.get("attachments"):
        return None  # sync, receipt and typing envelopes, group updates, deletions
    text, mentioned = resolve_mentions(text, data.get("mentions") or [], self_uuid, names)
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


def resolve_mentions(
    text: str, mentions: list[dict[str, Any]], self_uuid: str, names: Names
) -> tuple[str, bool]:
    """Replace each mention's placeholder (U+FFFC) with "@name"; True if one is the bot.
    Someone without a known name is "@someone": a number or uuid never goes into the text."""
    units, mentioned = text.encode("utf-16-le"), False  # Signal counts in UTF-16 code units
    for mention in sorted(mentions, key=lambda m: m.get("start", 0), reverse=True):
        is_bot = bool(self_uuid) and mention.get("uuid") == self_uuid
        mentioned = mentioned or is_bot
        name = NAME if is_bot else names.get(mention.get("uuid") or "", "someone")
        start = 2 * int(mention.get("start", 0))
        end = start + 2 * int(mention.get("length", 1))
        units = units[:start] + f"@{name}".encode("utf-16-le") + units[end:]
    return units.decode("utf-16-le", errors="replace"), mentioned


def send_params(cmd: dict[str, Any]) -> dict[str, Any]:
    """signal-cli `send` params for an Attaché `send` (or `ask`) command."""
    chat = str(cmd["chat_id"])
    direct = UUID.fullmatch(chat) or E164.fullmatch(chat)  # else a base64 group id
    params: dict[str, Any] = {"recipient": [chat]} if direct else {"groupId": chat}
    params["message"] = cmd.get("text") or ""
    if cmd.get("files"):
        params["attachments"] = [str(path) for path in cmd["files"]]
    when, _, author = str(cmd.get("reply_to") or "").partition(":")  # "<timestamp>:<uuid>"
    if when.isdigit() and author:
        params.update(quoteTimestamp=int(when), quoteAuthor=author)
    return params


def ask_text(text: str, options: list[str]) -> str:
    """The question plus how to answer it, since Signal has no buttons. Approvals have two
    options; any further ones can only be typed."""
    pairs = zip(THUMBS, options, strict=False)
    reacts = ", ".join(f"{thumb} for {option}" for thumb, option in pairs)
    return f"{text}\n\nReact {reacts}, or reply with the option."


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
        if not stored.is_file() or stored.stat().st_size > MAX_FILE:
            log("warning", f"attachment {item.get('id')!r} is missing or over 25 MB; skipped")
            continue
        name = Path(str(item.get("filename") or stored.name)).name  # chosen by the sender
        name = re.sub(r"[^\w.\- ]", "_", name)
        dest = target / f"{envelope.get('timestamp')}-{n}-{name}"
        shutil.copyfile(stored, dest)
        saved.append(str(dest.resolve()))
    return saved


# Settings, and signal-cli itself.


def data_dir() -> Path:
    """signal-cli's data folder, found the way signal-cli finds it."""
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / "signal-cli"


def signal_cli() -> str | None:
    """The absolute path of signal-cli (signal-cli.bat on Windows), or None when it cannot be
    found. One in the skill folder or the working directory, where shutil.which looks first on
    Windows, is never used: anyone who can drop a file there would get it run."""
    found = shutil.which(os.environ.get("ATTACHE_SIGNAL_CLI") or "signal-cli")
    path = Path(found).resolve() if found else None
    if path is None or path.is_relative_to(HERE) or path.parent == Path.cwd().resolve():
        return None
    return str(path)


def run(cmd: list[str], wait: float = 120) -> subprocess.CompletedProcess[str]:
    """Run a command to completion, capturing its output as text. It gets an empty stdin of its
    own: on Windows a child sharing ours stalls while read_stdin waits on it."""
    return subprocess.run(
        cmd, input="", capture_output=True, encoding="utf-8", errors="replace", timeout=wait
    )


def bot_aci(cli: str, number: str) -> str:
    """The bot's ACI from `signal-cli listAccounts`, or "" if the number is not listed.

    Raises one of FAILED when signal-cli fails, prints something other than JSON, or times
    out (as it does while another signal-cli holds the account)."""
    done = run([cli, "-o", "json", "listAccounts"])
    if done.returncode:
        raise RuntimeError((done.stderr.strip().splitlines() or [f"exit {done.returncode}"])[-1])
    accounts = json.loads(done.stdout or "[]")
    return next((a.get("aci") or "" for a in accounts if a.get("number") == number), "")


def kill_tree(pid: int) -> None:
    """Kill signal-cli and everything it started: on Windows, killing signal-cli.bat alone
    leaves Java running, and Java keeps the account locked."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, input=b"")
    else:  # signal-cli leads its own process group (start_new_session)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pid, signal.SIGKILL)


@dataclass
class Bridge:
    """One `signal-cli jsonRpc` process and what the sidecar remembers about it."""

    proc: asyncio.subprocess.Process
    self_uuid: str
    attachments: Path = field(default_factory=lambda: data_dir() / "attachments")  # signal-cli's
    files: Path = field(default_factory=lambda: Path(os.environ.get("ATTACHE_FILES") or "inbox"))
    pending: dict[int, tuple[str, Any]] = field(default_factory=dict)  # request id -> why
    asks: Asks = field(default_factory=dict)
    names: Names = field(default_factory=dict)
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
            elif message := inbound(envelope, self.self_uuid, self.names):
                self.names[message["sender_id"]] = message["sender_name"] or "someone"
                if message["mentioned"] or not message["is_group"]:  # not every group upload
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
    """Carry out Attaché's commands until `stop` or the end of stdin. Others, such as
    `answered`, need nothing on Signal and are ignored."""
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


async def idle(why: str, commands: asyncio.Queue[Any]) -> int:
    """Say why on stderr, then wait for `stop`: Attaché restarts a sidecar that exits."""
    print(f"Signal: {why}. Fix it (run doctor), then restart Attaché.", file=sys.stderr, flush=True)
    while (cmd := await commands.get()) is not None and cmd.get("type") != "stop":
        pass  # nothing can be sent until the setup is fixed
    return 0


def self_id(cli: str, number: str) -> str:
    """The bot's ACI, for spotting mentions, or "" with a warning when signal-cli can't say
    (signal-cli's own error shows when jsonRpc starts)."""
    with contextlib.suppress(*FAILED):
        if aci := bot_aci(cli, number):
            return aci
    log("warning", f"no Signal id for {number}, so group mentions go unnoticed; run doctor")
    return ""


async def sidecar() -> int:
    """Run signal-cli until Attaché says stop (0) or signal-cli exits (non-zero)."""
    commands: asyncio.Queue[Any] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    threading.Thread(target=read_stdin, args=(loop, commands), daemon=True).start()
    number, cli = os.environ.get("ATTACHE_NUMBER", ""), signal_cli()
    if not E164.fullmatch(number):
        return await idle("[channels.signal] number must look like +447700900123", commands)
    if not cli:
        return await idle("signal-cli not found", commands)
    self_uuid = os.environ.get("ATTACHE_UUID") or await asyncio.to_thread(self_id, cli, number)
    # Java writes redirected stdout in the native code page (cp1252 on Windows); make it UTF-8.
    java = f"{os.environ.get('JAVA_TOOL_OPTIONS', '')} -Dstdout.encoding=UTF-8".strip()
    env = {**os.environ, "JAVA_TOOL_OPTIONS": java}
    pipe, argv = asyncio.subprocess.PIPE, [cli, "-a", number, "jsonRpc"]
    proc = await asyncio.create_subprocess_exec(  # its own process group, for kill_tree
        *argv, stdin=pipe, stdout=pipe, env=env, limit=2**24, start_new_session=True
    )
    bridge = Bridge(proc, self_uuid)
    with contextlib.suppress(OSError):  # signal-cli may have quit already; read_signal sees it
        await bridge.request("version", {}, ("ready", None))  # the reply means it is up
    reader = asyncio.create_task(read_signal(bridge))
    attache = asyncio.create_task(serve(bridge, commands))
    await asyncio.wait({reader, attache}, return_when=asyncio.FIRST_COMPLETED)
    proc.stdin.close()  # signal-cli exits at the end of its input
    try:
        code = await asyncio.wait_for(proc.wait(), 4)
    except TimeoutError:
        kill_tree(proc.pid)
        code = await proc.wait()
    if attache.done():  # Attaché said stop, or closed stdin
        return 0
    attache.cancel()
    if code == 1 and ("ready", None) in bridge.pending.values():  # a setup error, before ready
        return await idle("signal-cli could not start the account; its error is above", commands)
    log("error", f"signal-cli exited with code {code}; its own message is in the log")
    return code or 1


# doctor.


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


def check_version(cli: str | None) -> bool:
    if not cli:
        return report("FAIL", "signal-cli: not found on PATH (SKILL.md step 3)")
    done = run([cli, "--version"], 60)
    found = re.search(r"\d+\.\d+\.\d+", done.stdout)
    if not found:
        error = (done.stderr or done.stdout).strip()[-300:]
        return report("FAIL", f"signal-cli ({cli}) did not run: {error}")
    text = f"signal-cli {found[0]} ({cli})"
    if not (released := release_date(found[0])):
        return report("ok", f"{text}; release date unknown (offline?)")
    age = (date.today() - released).days
    if age > 90:  # Signal's servers may stop accepting older releases
        return report("WARN", f"{text} is {age} days old and may stop working: update it")
    return report("ok", f"{text}, released {released}")


def check_account(cli: str, number: str) -> bool:
    if not E164.fullmatch(number):
        return report("FAIL", f"account: {number!r} is not a number like +447700900123")
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
    number = args[0] if args else os.environ.get("ATTACHE_NUMBER", "")
    return 0 if check_account(cli, number) and java_ok else 1


def release_date(version: str) -> date | None:
    """When a signal-cli version was released, from GitHub; None when offline."""
    try:
        with urllib.request.urlopen(f"{RELEASES}/tags/v{version}", timeout=5) as response:
            return datetime.fromisoformat(json.load(response)["published_at"]).date()
    except (OSError, ValueError, KeyError, TypeError):
        return None


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # the protocol is UTF-8 on every platform
    # Windows (any case): shutil.which on 3.12+, and cmd.exe running signal-cli.bat, then never
    # run a program from the working directory.
    os.environ["NODEFAULTCURRENTDIRECTORYINEXEPATH"] = "1"
    if argv[1:]:  # `doctor`, or `setup` from older instructions: SKILL.md is the guide now
        return doctor(argv[2:])
    return asyncio.run(sidecar())


if __name__ == "__main__":
    sys.exit(main(sys.argv))
