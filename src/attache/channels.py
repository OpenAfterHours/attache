"""Route outbound messages to channels; the terminal channel. Owner: runtime. Budget ~55 lines.

Sidecar channels (Slack, Signal) arrive in phase 2 and plug in here as Channel objects.
"""

from __future__ import annotations

import asyncio

from attache.types import Channel, OnMessage, Outbound


class ChannelManager:
    def __init__(self) -> None:
        self.channels: dict[str, Channel] = {}
        raise NotImplementedError

    def add(self, channel: Channel) -> None:
        raise NotImplementedError

    async def send(self, msg: Outbound) -> None:
        """Deliver via the channel named by the part of chat_key before the first ":".
        An unknown channel is logged, not raised."""
        raise NotImplementedError

    async def start(self, on_message: OnMessage) -> list[asyncio.Task[None]]:
        raise NotImplementedError

    async def stop(self) -> None:
        raise NotImplementedError


class TerminalChannel:
    """Chat in the terminal. Chat key "terminal:local", sender id "owner".

    Reads lines in a worker thread so the event loop keeps running; prints replies as they
    arrive. EOF, "/quit" or "/exit" sets `done`. Prints the web URL once at start."""

    name = "terminal"

    def __init__(self, owner_name: str, web_url: str) -> None:
        self.done = asyncio.Event()
        raise NotImplementedError

    async def start(self, on_message: OnMessage) -> None:
        raise NotImplementedError

    async def send(self, msg: Outbound) -> None:
        raise NotImplementedError

    async def stop(self) -> None:
        raise NotImplementedError
