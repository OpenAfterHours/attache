from __future__ import annotations

import asyncio
import builtins
import logging
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
    hub, slack, signal = ChannelManager(), Channel("slack"), Channel("signal")
    hub.add(slack)
    hub.add(signal)
    received: list[Inbound] = []

    async def on_message(msg: Inbound) -> None:
        received.append(msg)

    async def main() -> None:
        tasks = await hub.start(on_message)
        await asyncio.gather(*tasks)
        await hub.send(Outbound("slack:C1:thread", "to slack"))
        await hub.send(Outbound("signal:group", "to signal"))
        with caplog.at_level(logging.WARNING):
            await hub.send(Outbound("discord:x", "lost"))  # logged, not raised
        await hub.stop()

    asyncio.run(main())
    assert [m.text for m in slack.sent] == ["to slack"] and [m.text for m in signal.sent] == [
        "to signal"
    ]
    assert sorted(m.text for m in received) == ["hello from signal", "hello from slack"]
    assert slack.started and slack.stopped and signal.stopped
    assert "discord:x" in caplog.text


def test_terminal_channel(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    lines = iter(["hello", "/quit", "never read"])
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(lines))
    term, received = TerminalChannel("Alex", "http://127.0.0.1:8765"), []

    async def on_message(msg: Inbound) -> None:
        received.append(msg)
        await term.send(Outbound(msg.chat_key, "hi Alex", ["C:/files/a.pdf"]))

    asyncio.run(term.start(on_message))
    assert term.done.is_set() and next(lines) == "never read"
    assert received == [Inbound("terminal", "local", "owner", "Alex", "hello", ts=received[0].ts)]
    out = capsys.readouterr().out
    assert "http://127.0.0.1:8765" in out and "attaché> hi Alex\n  file: C:/files/a.pdf" in out


def test_terminal_channel_ends_on_eof(monkeypatch: pytest.MonkeyPatch) -> None:
    def eof(prompt: str = "") -> Any:
        raise EOFError

    monkeypatch.setattr(builtins, "input", eof)
    term = TerminalChannel("Alex", "http://x")
    asyncio.run(term.start(lambda msg: asyncio.sleep(0)))
    assert term.done.is_set()
    term.done.clear()
    asyncio.run(term.stop())
    assert term.done.is_set()
