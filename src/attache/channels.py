"""Route messages to channels: the terminal, the web page and sidecar programs for chat apps.

Several channels may share a name: the terminal and the web page are both "local", the owner's
one conversation (chat key "local:me"), so a reply reaches both. A sidecar is a skill's program
that speaks JSON lines on stdin and stdout; docs/channels.md has the protocol.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from attache import skills
from attache.config import Config
from attache.types import Channel, Inbound, OnAnswer, OnMessage, Outbound

logger = logging.getLogger(__name__)
MESSAGE_KEYS = {f.name for f in fields(Inbound)} - {"channel", "ts", "origin"}  # from a sidecar
STEADY_SECONDS = 60.0  # a sidecar up this long starts its restart delays from the beginning


class ChannelManager:
    def __init__(self) -> None:
        self.channels: dict[str, list[Channel]] = {}

    def add(self, channel: Channel) -> None:
        self.channels.setdefault(channel.name, []).append(channel)

    def _targets(self, chat_key: str) -> list[Channel]:
        """The channels named by the part of chat_key before the first ":"; none is logged."""
        targets = self.channels.get(chat_key.split(":", 1)[0], [])
        if not targets:
            logger.warning("no channel for %s", chat_key)
        return targets

    async def send(self, msg: Outbound) -> None:
        for channel in self._targets(msg.chat_key):
            await channel.send(msg)

    async def ask(self, chat_key: str, ask_id: str, text: str, options: list[str]) -> None:
        """Show a question with native buttons where a channel has ask(), else as numbered text."""
        numbered = ", ".join(f"{n} for {option}" for n, option in enumerate(options, 1))
        for channel in self._targets(chat_key):
            if hasattr(channel, "ask"):
                await channel.ask(chat_key.split(":", 1)[1], ask_id, text, options)
            else:
                await channel.send(Outbound(chat_key, f"{text}\nReply {numbered}."))

    async def start(self, on_message: OnMessage) -> list[asyncio.Task[None]]:
        return [asyncio.create_task(c.start(on_message)) for g in self.channels.values() for c in g]

    async def stop(self) -> None:
        for channel in [c for group in self.channels.values() for c in group]:
            await channel.stop()


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
    """A skill's sidecar program (docs/channels.md). Its stderr goes to Attaché's stderr."""

    name: str
    argv: list[str]
    cwd: Path
    env: dict[str, str]
    on_answer: OnAnswer
    backoff: tuple[float, float] = (1.0, 60.0)  # the first and the longest restart delay
    proc: asyncio.subprocess.Process | None = field(default=None, init=False)
    stopping: bool = field(default=False, init=False)
    turns: set[asyncio.Task[None]] = field(default_factory=set, init=False)

    async def start(self, on_message: OnMessage) -> None:
        """Run the sidecar until stop(), restarting it when it exits. Never raises."""
        delay = self.backoff[0]
        while not self.stopping:
            began, pipe = time.monotonic(), asyncio.subprocess.PIPE
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    *self.argv, cwd=self.cwd, env=self.env, stdin=pipe, stdout=pipe
                )
                async for line in self.proc.stdout:
                    try:
                        self._handle(json.loads(line), on_message)
                    except Exception as e:  # not JSON, or not a valid event: ignored
                        logger.warning("channel %s ignored %.200r (%s)", self.name, line, e)
                await self.proc.wait()
            except Exception:
                logger.exception("channel %s failed", self.name)
            if self.stopping:
                return
            if time.monotonic() - began >= STEADY_SECONDS:
                delay = self.backoff[0]
            logger.warning("channel %s exited; restarting in %s s", self.name, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.backoff[1])

    def _handle(self, event: dict[str, Any], on_message: OnMessage) -> None:
        """Act on one event from the sidecar; unknown types are ignored."""
        kind = event.get("type")
        if kind == "message":  # a task, so that a turn waiting for an answer can still read it
            inbound = Inbound(self.name, **{k: v for k, v in event.items() if k in MESSAGE_KEYS})
            turn = asyncio.create_task(on_message(inbound))
            self.turns.add(turn)  # held until done, so it is not garbage-collected
            turn.add_done_callback(self.turns.discard)
        elif kind == "answer":
            self.on_answer(event["ask_id"], event["sender_id"], event["choice"])
        elif kind in ("ready", "log"):
            level = logging.WARNING if event.get("level") in ("warning", "error") else logging.INFO
            logger.log(level, "channel %s %s: %s", self.name, kind, event.get("text", event))

    async def _write(self, **event: Any) -> None:
        try:
            self.proc.stdin.write(json.dumps(event).encode() + b"\n")
            await self.proc.stdin.drain()
        except (AttributeError, OSError):  # not started yet, or it just exited
            logger.warning("channel %s is not running; dropped a %s", self.name, event["type"])

    async def send(self, msg: Outbound) -> None:
        files = [str(Path(f).resolve()) for f in msg.files]  # the protocol wants absolute paths
        event = {"chat_id": msg.chat_key.split(":", 1)[1], "text": msg.text, "files": files}
        await self._write(type="send", **event, reply_to=msg.reply_to)

    async def ask(self, chat_id: str, ask_id: str, text: str, options: list[str]) -> None:
        await self._write(type="ask", chat_id=chat_id, ask_id=ask_id, text=text, options=options)

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
            self.proc.kill()


def sidecars(config: Config, on_answer_for: Callable[[str], OnAnswer]) -> list[SidecarChannel]:
    """A SidecarChannel for each enabled [channels.<name>] whose skill names its script in
    metadata["attache-channel"]. Others, and all of them when uv is missing, are logged."""
    found = {skill.name: skill for skill in skills.discover(config.home)}
    uv, made = shutil.which("uv"), []
    for name, settings in config.channels.items():
        skill = found.get(name)
        script = (skill.meta.get("metadata") or {}).get("attache-channel") if skill else None
        if not settings["enabled"]:
            continue
        if not script or not uv:
            why = "uv is not installed" if script else f"skills/{name} has no attache-channel"
            logger.warning("channel %s skipped: %s", name, why)
            continue
        inbox = config.home / "files" / "inbox" / name
        inbox.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, **{f"ATTACHE_{k.upper()}": str(v) for k, v in settings.items()}}
        env.update(ATTACHE_HOME=str(config.home), ATTACHE_CHANNEL=name, ATTACHE_FILES=str(inbox))
        argv = [uv, "run", str(skill.path / script)]
        made.append(SidecarChannel(name, argv, skill.path, env, on_answer_for(name)))
    return made
