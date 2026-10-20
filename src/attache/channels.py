"""Route messages to channels: the terminal, the web page and sidecar programs for chat apps.

Several channels may share a name: the terminal and the web page are both "local", the owner's
one conversation (chat key "local:me"), so a reply reaches both. A sidecar is a skill's program
that speaks JSON lines on stdin and stdout; docs/channels.md has the protocol.

ChannelManager also asks questions and takes their answers. A question goes to one person in a
chat: Slack shows buttons, Signal a reaction prompt, the web page buttons, and every channel
accepts a typed reply naming an option or its number. Only the person asked can answer. Open
questions live in memory, keyed by a random ask_id; one that outlives its timeout, or
Attaché, is simply gone.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, NamedTuple

from attache import skills
from attache.config import Config
from attache.types import Channel, Inbound, OnAnswer, OnMessage, Outbound

logger = logging.getLogger(__name__)
MESSAGE_KEYS = {f.name for f in fields(Inbound)} - {"channel", "ts", "origin", "files"}
STEADY_SECONDS = 60.0  # a sidecar up this long starts its restart delays from the beginning
PLAIN_NAME = re.compile(r"(?!\.\.?$)[\w.-]+")  # a file name, not "." or ".."
SDK_KEYS = {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY"}  # SDK defaults
# A sidecar's own process group: Ctrl+C reaches it only through `stop`, and kill_tree can find
# its children. start_new_session is ignored on Windows, where the flag does the same.
PIPE = asyncio.subprocess.PIPE
SPAWN: dict[str, Any] = {"stdin": PIPE, "stdout": PIPE, "limit": 2**24, "start_new_session": True}
SPAWN["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


Shown = Callable[[], object] | None  # ask()'s callback for "a surface has shown it"


class Pending(NamedTuple):
    """A question waiting for one person's answer."""

    chat_key: str
    person_id: str
    text: str
    options: list[str]
    future: asyncio.Future[str]


def choose(options: list[str], text: str) -> str | None:
    """The option `text` names, case-insensitively, or by its 1-based number; else None."""
    typed = text.strip().strip(".!").casefold()
    for number, option in enumerate(options, 1):
        if typed in (option.casefold(), str(number)):
            return option
    return None


async def kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill a process and every process it started, then reap it. On POSIX it must lead its
    own session (start_new_session=True), so its children share its process group."""
    if proc.returncode is None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
        else:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
    await proc.wait()


class ChannelManager:
    def __init__(self) -> None:
        self.channels: dict[str, list[Channel]] = {}
        self.pending: dict[str, Pending] = {}  # open questions by ask_id, oldest first
        self.tasks: set[asyncio.Task[Any]] = set()
        self.closing = False  # set by stop(): no new work, so nothing outlives the store
        self.ask_seconds = 600.0  # how long ask() waits for an answer

    def add(self, channel: Channel) -> None:
        self.channels.setdefault(channel.name, []).append(channel)

    def spawn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any] | None:
        """Run coro in a task that is kept until it is done; stop() cancels it. Once stop()
        has begun, coro is closed unrun and None returned."""
        if self.closing:
            coro.close()
            return None
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def _targets(self, chat_key: str) -> list[Channel]:
        """The channels named by the part of chat_key before the first ":"; none is logged."""
        targets = self.channels.get(chat_key.split(":", 1)[0], [])
        if not targets:
            logger.warning("no channel for %s", chat_key)
        return targets

    async def send(self, msg: Outbound) -> None:
        for channel in self._targets(msg.chat_key):
            await channel.send(msg)

    async def ask(
        self, chat_key: str, person_id: str, text: str, options: list[str], shown: Shown = None
    ) -> str | None:
        """Show the question, with native buttons where a channel has ask() and as numbered
        text elsewhere, and wait for person_id's choice; None after ask_seconds. `shown` is
        called once, as soon as a surface has shown it; LookupError when none could.
        person_id is a config person id ("owner", "sam")."""
        ask_id = secrets.token_hex(8)
        chat_id = chat_key.split(":", 1)[1]
        future = asyncio.get_running_loop().create_future()
        self.pending[ask_id] = Pending(chat_key, person_id, text, options, future)
        numbered = ", ".join(f"{n} for {option}" for n, option in enumerate(options, 1))
        try:
            seen = False
            for channel in self._targets(chat_key):
                if hasattr(channel, "ask"):
                    reached = await channel.ask(chat_id, ask_id, text, options)
                else:
                    await channel.send(Outbound(chat_key, f"{text}\nReply {numbered}."))
                    reached = True
                if reached and not seen and shown:
                    shown()  # once, at the first surface that showed it
                seen = seen or reached
            if not seen:
                raise LookupError(f"nothing in {chat_key} could show the question")
            return await asyncio.wait_for(future, self.ask_seconds)
        except TimeoutError:
            return None
        finally:
            del self.pending[ask_id]

    def answer(self, ask_id: str, person_id: str, choice: str) -> bool:
        """Resolve an open question when person_id is the person asked and choice an option."""
        pending = self.pending.get(ask_id)
        if pending is None or pending.person_id != person_id or pending.future.done():
            return False
        option = choose(pending.options, choice)
        if option is not None:
            pending.future.set_result(option)
        return option is not None

    def match(self, chat_key: str, person_id: str, text: str) -> bool:
        """Typed-reply fallback: when exactly one open question in chat_key waits for
        person_id and text names one of its options, answer it and return True; the router
        then treats the message as an answer, not a request. With two or more open, a typed
        reply could mean either, so it is an ordinary message; buttons still answer."""
        key = (chat_key, person_id)
        waiting = [i for i, p in self.pending.items() if (p.chat_key, p.person_id) == key]
        return len(waiting) == 1 and self.answer(waiting[0], person_id, text)

    async def start(self, handler: OnMessage) -> list[asyncio.Task[None]]:
        """Start every channel. Each gets a callback that runs handler in a task of its own
        and returns at once, so a turn never holds up its channel (or an answer behind it)."""

        async def deliver(msg: Inbound) -> None:
            self.spawn(handler(msg))

        return [asyncio.create_task(c.start(deliver)) for g in self.channels.values() for c in g]

    async def stop(self) -> None:
        """Cancel the turns still running and wait for them, then stop every channel at once."""
        self.closing = True
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        everything = [c.stop() for group in self.channels.values() for c in group]
        await asyncio.gather(*everything, return_exceptions=True)


class TerminalChannel:
    """Chat in the terminal as the owner, in "local:me" (shared with the web page). A daemon
    thread reads stdin and queues the lines, so the event loop never waits on it and Ctrl+C
    exits at once. EOF, "/quit" or "/exit" sets `done`. Prints the web page's URL at start.
    Empty lines go through too: the router ignores them."""

    name = "local"

    def __init__(self, owner_name: str, url: str) -> None:
        self.done, self.lines = asyncio.Event(), asyncio.Queue[str]()
        self.owner_name, self.url = owner_name, url

    async def start(self, on_message: OnMessage) -> None:
        threading.Thread(target=self._read, args=(asyncio.get_running_loop(),), daemon=True).start()
        print(f"Chat here, or open {self.url}\nType /quit to leave.", flush=True)
        while (line := (await self.lines.get()).strip()) not in ("/quit", "/exit"):
            msg = Inbound("local", "me", "owner", self.owner_name, line, origin="terminal")
            await on_message(msg)
        self.done.set()

    def _read(self, loop: asyncio.AbstractEventLoop) -> None:
        """Queue lines until EOF ("") or a quit line; that last one start() still waits for."""
        # Keep sys.stdin.readline: a daemon thread blocked in sys.stdin.buffer crashes the
        # interpreter's exit on Windows.
        while (line := sys.stdin.readline() or "/quit").strip() not in ("/quit", "/exit"):
            loop.call_soon_threadsafe(self.lines.put_nowait, line)
        loop.call_soon_threadsafe(self.lines.put_nowait, "/quit")

    async def send(self, msg: Outbound) -> None:
        """Print a reply, or a message the owner typed on the web page (`author` set)."""
        if msg.author and msg.origin == "terminal":
            return  # typed here, so it is already on screen
        who = f"{msg.author} (web)" if msg.author else "attaché"
        print(f"{who}> {msg.text}" + "".join(f"\n  file: {f}" for f in msg.files), flush=True)

    async def stop(self) -> None:
        self.lines.put_nowait("/quit")  # start() then sets done


@dataclass
class SidecarChannel:
    """A skill's sidecar program (docs/channels.md). Its stderr goes to Attaché's stderr; its
    env's ATTACHE_FILES is the only folder its messages' files may come from."""

    name: str
    argv: list[str]
    cwd: Path
    env: dict[str, str]
    on_answer: OnAnswer
    backoff: tuple[float, float] = (1.0, 60.0)  # the first and the longest restart delay
    proc: asyncio.subprocess.Process | None = field(default=None, init=False)
    stopping: bool = field(default=False, init=False)

    async def start(self, on_message: OnMessage) -> None:
        """Run the sidecar until stop(), restarting it when it exits. Never raises."""
        delay = self.backoff[0]
        while not self.stopping:
            began = time.monotonic()
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    *self.argv, cwd=self.cwd, env=self.env, **SPAWN
                )
                async for line in self.proc.stdout:
                    try:
                        await self._handle(json.loads(line), on_message)
                    except Exception as e:  # not JSON, or not a valid event: ignored
                        logger.warning("channel %s ignored %.200r (%s)", self.name, line, e)
            except Exception:
                logger.exception("channel %s failed", self.name)
            if self.stopping:
                return
            if self.proc:  # after a reader error it may still run: never leave it behind
                await kill_tree(self.proc)
            if time.monotonic() - began >= STEADY_SECONDS:
                delay = self.backoff[0]
            logger.warning("channel %s exited; restarting in %s s", self.name, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.backoff[1])

    async def _handle(self, event: dict[str, Any], on_message: OnMessage) -> None:
        """Act on one event from the sidecar; unknown types are ignored."""
        kind = event.get("type")
        if kind == "message":
            inbox = Path(self.env["ATTACHE_FILES"]).resolve()
            files = [f for f in event.get("files", []) if Path(f).resolve().is_relative_to(inbox)]
            known = {k: v for k, v in event.items() if k in MESSAGE_KEYS}
            await on_message(Inbound(self.name, **known, files=files))  # others are dropped
        elif kind == "answer":
            ask_id, sender_id = event["ask_id"], event["sender_id"]
            accepted = self.on_answer(self.name, ask_id, sender_id, event["choice"])
            result = {"ask_id": ask_id, "sender_id": sender_id, "accepted": accepted}
            await self._write(type="answered", **result)  # Slack updates its buttons on this
        elif kind in ("ready", "log"):
            logger.info("channel %s %s: %s", self.name, kind, event.get("text", event))

    async def _write(self, **event: Any) -> bool:
        """Send one command; False, with a warning, when the sidecar is not running."""
        if self.proc and self.proc.returncode is None:
            with contextlib.suppress(OSError):  # it exited meanwhile; start() restarts it
                self.proc.stdin.write(json.dumps(event).encode() + b"\n")
                await self.proc.stdin.drain()
                return True
        logger.warning("channel %s is not running; dropped a %s", self.name, event["type"])
        return False

    async def send(self, msg: Outbound) -> None:
        files = [str(Path(f).resolve()) for f in msg.files]  # the protocol wants absolute paths
        event = {"chat_id": msg.chat_key.split(":", 1)[1], "text": msg.text, "files": files}
        await self._write(type="send", **event, reply_to=msg.reply_to)

    async def ask(self, chat_id: str, ask_id: str, text: str, options: list[str]) -> bool:
        """True when the question reached the sidecar."""
        event = {"chat_id": chat_id, "ask_id": ask_id, "text": text, "options": options}
        return await self._write(type="ask", **event)

    async def stop(self) -> None:
        """Ask the sidecar to stop, close its stdin, and kill it if it is still up after 5 s."""
        self.stopping = True
        if self.proc is None or self.proc.returncode is not None:
            return
        await self._write(type="stop")
        self.proc.stdin.close()
        try:
            await asyncio.wait_for(self.proc.wait(), 5)
        except TimeoutError:
            await kill_tree(self.proc)


def channel_script(skill: skills.Skill) -> Path | None:
    """The sidecar script a skill names in metadata["attache-channel"]: a plain file name
    (not a path) of a file in the skill's folder. None for anything else."""
    metadata = skill.meta.get("metadata")
    name = str(metadata.get("attache-channel", "")) if isinstance(metadata, dict) else ""
    script = skill.path / name
    return script if PLAIN_NAME.fullmatch(name) and script.is_file() else None


def sidecars(config: Config, on_answer: OnAnswer) -> list[SidecarChannel]:
    """A SidecarChannel for each enabled [channels.<name>] whose skill has a channel_script().
    Others are logged and skipped. A sidecar's environment has no model API keys."""
    found = {skill.name: skill for skill in skills.discover(config.home)}
    secret = {e.key_env.upper() for e in config.endpoints.values() if e.key_env} | SDK_KEYS
    clean = {k: v for k, v in os.environ.items() if k.upper() not in secret}
    uv, made = shutil.which("uv"), []
    for name, settings in config.channels.items():
        if not settings["enabled"]:
            continue
        skill = found.get(name)
        script = channel_script(skill) if skill else None
        if script is None or not uv:
            why = "uv is not installed" if script else f"no attache-channel file in skills/{name}"
            logger.warning("channel %s skipped: %s", name, why)
            continue
        inbox = config.home / "files" / "inbox" / name
        inbox.mkdir(parents=True, exist_ok=True)
        env = {**clean, **{f"ATTACHE_{k.upper()}": str(v) for k, v in settings.items()}}
        env.update(ATTACHE_HOME=str(config.home), ATTACHE_CHANNEL=name, ATTACHE_FILES=str(inbox))
        made.append(SidecarChannel(name, [uv, "run", str(script)], script.parent, env, on_answer))
    return made
