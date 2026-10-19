"""Route outbound messages to channels, and the terminal channel.

Several channels may share a name: the terminal and the web page are both "local", the owner's
one conversation (chat key "local:me"), so a reply reaches both. Sidecar channels (Slack,
Signal) arrive in phase 2 and plug in here as Channel objects.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading

from attache.types import Channel, Inbound, OnMessage, Outbound


class ChannelManager:
    def __init__(self) -> None:
        self.channels: dict[str, list[Channel]] = {}

    def add(self, channel: Channel) -> None:
        self.channels.setdefault(channel.name, []).append(channel)

    async def send(self, msg: Outbound) -> None:
        """Deliver to every channel named by the part of chat_key before the first ":".
        An unknown channel is logged, not raised."""
        targets = self.channels.get(msg.chat_key.split(":", 1)[0], [])
        if not targets:
            logging.getLogger(__name__).warning("no channel for %s", msg.chat_key)
        for channel in targets:
            await channel.send(msg)

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
            await on_message(Inbound("local", "me", "owner", self.owner_name, line))
        self.done.set()

    def _read(self, loop: asyncio.AbstractEventLoop) -> None:
        """Queue lines until EOF ("") or a quit line; that last one start() still waits for."""
        while (line := sys.stdin.readline() or "/quit").strip() not in ("/quit", "/exit"):
            loop.call_soon_threadsafe(self.lines.put_nowait, line)
        loop.call_soon_threadsafe(self.lines.put_nowait, "/quit")

    async def send(self, msg: Outbound) -> None:
        print(f"attaché> {msg.text}" + "".join(f"\n  file: {f}" for f in msg.files), flush=True)

    async def stop(self) -> None:
        self.lines.put_nowait("/quit")  # start() then sets done
