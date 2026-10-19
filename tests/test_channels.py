from __future__ import annotations

import asyncio
import io
import logging
import sys
import threading
from typing import Any

import pytest

from attache.channels import ChannelManager, TerminalChannel
from attache.types import Inbound, OnMessage, Outbound


class Channel:
    def __init__(self, name: str) -> None:
        self.name, self.sent, self.started, self.stopped = name, [], False, False

    async def start(self, on_message: OnMessage) -> None:
        self.started = True
        await on_message(Inbound(self.name, "c1", "u1", "U", f"hello from {self.name}"))

    async def send(self, msg: Outbound) -> None:
        self.sent.append(msg)

    async def stop(self) -> None:
        self.stopped = True


def test_manager_routes_by_chat_key_prefix(caplog: pytest.LogCaptureFixture) -> None:
    hub = ChannelManager()
    terminal, web, slack = Channel("local"), Channel("local"), Channel("slack")
    for channel in (terminal, web, slack):
        hub.add(channel)
    received: list[Inbound] = []

    async def on_message(msg: Inbound) -> None:
        received.append(msg)

    async def main() -> None:
        await asyncio.gather(*await hub.start(on_message))
        await hub.send(Outbound("local:me", "to both"))
        await hub.send(Outbound("slack:C1:thread", "to slack"))
        with caplog.at_level(logging.WARNING):
            await hub.send(Outbound("discord:x", "lost"))  # logged, not raised
        await hub.stop()

    asyncio.run(main())
    assert [m.text for m in terminal.sent] == [m.text for m in web.sent] == ["to both"]
    assert [m.text for m in slack.sent] == ["to slack"]
    assert len(received) == 3 and all(c.started and c.stopped for c in (terminal, web, slack))
    assert "discord:x" in caplog.text


def test_terminal_channel(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("hello\n\n/quit\nnever read\n"))
    term, received = TerminalChannel("Alex", "http://127.0.0.1:8765/#t=abc"), []

    async def on_message(msg: Inbound) -> None:
        received.append(msg)
        await term.send(Outbound(msg.chat_key, "hi Alex", ["C:/files/a.pdf"]))

    asyncio.run(asyncio.wait_for(term.start(on_message), 5))
    assert term.done.is_set() and term.name == "local"
    assert [(m.chat_key, m.sender_id, m.sender_name, m.text) for m in received] == [
        ("local:me", "owner", "Alex", "hello"),
        ("local:me", "owner", "Alex", ""),  # the router ignores empty messages
    ]
    out = capsys.readouterr().out
    assert (
        "http://127.0.0.1:8765/#t=abc" in out and "attaché> hi Alex\n  file: C:/files/a.pdf" in out
    )


def test_terminal_channel_ends_on_eof(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    term = TerminalChannel("Alex", "http://x")
    asyncio.run(asyncio.wait_for(term.start(lambda msg: asyncio.sleep(0)), 5))
    assert term.done.is_set()


def test_stop_ends_a_terminal_that_waits_for_input(monkeypatch: pytest.MonkeyPatch) -> None:
    class Waiting:  # stdin that blocks for good, as a person who never types
        def readline(self) -> str:
            threading.Event().wait()
            return ""

    monkeypatch.setattr(sys, "stdin", Waiting())
    term = TerminalChannel("Alex", "http://x")

    async def main() -> Any:
        task = asyncio.create_task(term.start(lambda msg: asyncio.sleep(0)))
        await asyncio.sleep(0.1)
        await term.stop()
        await asyncio.wait_for(task, 5)  # the reader thread is a daemon: nothing waits for it

    asyncio.run(main())
    assert term.done.is_set()
