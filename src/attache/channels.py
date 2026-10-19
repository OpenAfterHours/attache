"""Route outbound messages to channels by chat key prefix; the terminal REPL. Owner: runtime."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from attache.types import Channel, Inbound, OnMessage, Outbound


class ChannelManager:
    def __init__(self) -> None:
        self.channels: dict[str, Channel] = {}

    def add(self, channel: Channel) -> None:
        self.channels[channel.name] = channel

    async def send(self, msg: Outbound) -> None:  # an unknown channel is logged, not raised
        channel = self.channels.get(msg.chat_key.split(":", 1)[0])
        await channel.send(msg) if channel else logging.warning("no channel for %s", msg.chat_key)

    async def start(self, on_message: OnMessage) -> list[asyncio.Task[None]]:
        return [asyncio.create_task(c.start(on_message)) for c in self.channels.values()]

    async def stop(self) -> None:
        await asyncio.gather(*(c.stop() for c in self.channels.values()), return_exceptions=True)


class TerminalChannel:  # chat key "terminal:local", sender "owner"; EOF, /quit or /exit sets done
    def __init__(self, owner_name: str, web_url: str) -> None:
        self.name, self.done, self.user, self.url = "terminal", asyncio.Event(), owner_name, web_url

    async def start(self, on_message: OnMessage) -> None:
        print(f"Attaché is also at {self.url}. Type /quit to leave.")
        with contextlib.suppress(EOFError):  # input() runs in a thread so the loop keeps going
            while (line := await asyncio.to_thread(input, "> ")) not in ("/quit", "/exit"):
                await on_message(Inbound("terminal", "local", "owner", self.user, line))
        self.done.set()

    async def send(self, msg: Outbound) -> None:
        print(f"attaché> {msg.text}" + "".join(f"\n  file: {f}" for f in msg.files), flush=True)

    async def stop(self) -> None:
        self.done.set()
