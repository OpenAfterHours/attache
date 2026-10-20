from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import shutil
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from attache import channels, config
from attache.channels import ChannelManager, SidecarChannel, TerminalChannel
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


class AskingChannel(Channel):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.asked: list[tuple[str, str, str, list[str]]] = []

    async def ask(self, chat_id: str, ask_id: str, text: str, options: list[str]) -> None:
        self.asked.append((chat_id, ask_id, text, options))


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


def test_ask_uses_native_buttons_or_numbered_text() -> None:
    hub = ChannelManager()
    plain, native = Channel("slack"), AskingChannel("slack")
    hub.add(plain)
    hub.add(native)
    asyncio.run(hub.ask("slack:D0ALEX", "a19", "Send it?", ["Approve", "Decline"]))
    assert native.asked == [("D0ALEX", "a19", "Send it?", ["Approve", "Decline"])]
    assert native.sent == []
    assert [(m.chat_key, m.text) for m in plain.sent] == [
        ("slack:D0ALEX", "Send it?\nReply 1 for Approve, 2 for Decline.")
    ]


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
    assert [(m.chat_key, m.sender_id, m.sender_name, m.text, m.origin) for m in received] == [
        ("local:me", "owner", "Alex", "hello", "terminal"),
        ("local:me", "owner", "Alex", "", "terminal"),  # the router ignores empty messages
    ]
    out = capsys.readouterr().out
    assert "http://127.0.0.1:8765/#t=abc" in out
    assert "attaché> hi Alex\n  file: C:/files/a.pdf" in out


def test_terminal_mirrors_web_messages_but_not_its_own(capsys: pytest.CaptureFixture[str]) -> None:
    term, hub = TerminalChannel("Alex", "http://x"), ChannelManager()
    hub.add(term)

    async def main() -> None:
        await term.send(Outbound("local:me", "typed on the web", author="Alex", origin="web"))
        await term.send(Outbound("local:me", "typed here", author="Alex", origin="terminal"))
        await term.send(Outbound("local:me", "a reply", origin="terminal"))
        await hub.ask("local:me", "a1", "Send it?", ["Approve", "Decline"])  # no native ask

    asyncio.run(main())
    assert capsys.readouterr().out.splitlines() == [
        "Alex (web)> typed on the web",
        "attaché> a reply",
        "attaché> Send it?",
        "Reply 1 for Approve, 2 for Decline.",
    ]


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


# sidecars: small Python programs run with this interpreter

ECHO = """
import json, sys

def emit(**event):
    print(json.dumps(event), flush=True)

emit(type="ready", self_id="B0T", name="Bot")
print("not json", flush=True)
emit(type="unknown")
emit(type="message", chat_id="C1", sender_id="U0SAM", sender_name="Sam", text="hi",
     is_group=True, mentioned=True, message_id="m1", files=[], extra="ignored")
for line in sys.stdin:
    event = json.loads(line)
    if event["type"] == "send":
        emit(type="message", chat_id="C1", sender_id="U0SAM", sender_name="Echo", text=line)
    elif event["type"] == "ask":
        emit(type="answer", ask_id=event["ask_id"], sender_id="U0ALEX", choice=event["options"][0])
    elif event["type"] == "stop":
        break
"""


def sidecar(tmp_path: Path, code: str, **kwargs: Any) -> SidecarChannel:
    script = tmp_path / "sidecar.py"
    script.write_text(code, encoding="utf-8")
    on_answer = kwargs.pop("on_answer", lambda ask_id, sender_id, choice: True)
    return SidecarChannel(
        "fake", [sys.executable, str(script)], tmp_path, dict(os.environ), on_answer, **kwargs
    )


async def until(condition: Callable[[], bool], timeout: float = 10.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not condition():
        assert loop.time() < end, "timed out"
        await asyncio.sleep(0.01)


def test_sidecar_speaks_the_protocol(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    answers: list[tuple[str, str, str]] = []
    channel = sidecar(tmp_path, ECHO, on_answer=lambda *answer: answers.append(answer) or True)
    received: list[Inbound] = []

    async def on_message(msg: Inbound) -> None:
        received.append(msg)

    async def main() -> None:
        task = asyncio.create_task(channel.start(on_message))
        await until(lambda: len(received) == 1)
        await channel.send(Outbound("fake:C1", "09:40 from Portsmouth", ["notes.txt"], "m1"))
        await until(lambda: len(received) == 2)
        await channel.ask("D0ALEX", "a19", "Send it?", ["Approve", "Decline"])
        await until(lambda: bool(answers))
        await channel.stop()
        await asyncio.wait_for(task, 10)  # start() returns once stopped

    with caplog.at_level(logging.INFO, logger="attache.channels"):
        asyncio.run(main())
    first = received[0]
    assert (first.channel, first.chat_key, first.sender_id, first.sender_name) == (
        "fake",
        "fake:C1",
        "U0SAM",
        "Sam",
    )
    assert (first.text, first.is_group, first.mentioned, first.message_id) == (
        "hi",
        True,
        True,
        "m1",
    )
    sent = json.loads(received[1].text)
    assert sent == {
        "type": "send",
        "chat_id": "C1",
        "text": "09:40 from Portsmouth",
        "files": [str((Path.cwd() / "notes.txt").resolve())],  # absolute
        "reply_to": "m1",
    }
    assert answers == [("a19", "U0ALEX", "Approve")]
    assert "ready" in caplog.text and "B0T" in caplog.text
    assert "ignored b'not json" in caplog.text
    assert channel.proc is not None and channel.proc.returncode is not None


EXITS = """
import json
print(json.dumps({"type": "message", "chat_id": "C1", "sender_id": "U1", "sender_name": "S",
                  "text": "started"}), flush=True)
"""


def test_sidecar_restarts_with_growing_delays(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    channel = sidecar(tmp_path, EXITS, backoff=(0.01, 0.04))
    starts: list[Inbound] = []

    async def on_message(msg: Inbound) -> None:
        starts.append(msg)

    async def main() -> None:
        task = asyncio.create_task(channel.start(on_message))
        await until(lambda: len(starts) >= 5)  # five starts: four delays logged before them
        await channel.stop()
        await asyncio.wait_for(task, 10)

    with caplog.at_level(logging.WARNING, logger="attache.channels"):
        asyncio.run(main())
    delays = [r.args[1] for r in caplog.records if "restarting" in r.getMessage()]
    assert delays[:4] == [0.01, 0.02, 0.04, 0.04]


def test_sidecar_delay_resets_after_a_steady_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(channels, "STEADY_SECONDS", 0.0)  # every run counts as steady
    channel = sidecar(tmp_path, EXITS, backoff=(0.01, 0.04))
    starts: list[Inbound] = []

    async def on_message(msg: Inbound) -> None:
        starts.append(msg)

    async def main() -> None:
        task = asyncio.create_task(channel.start(on_message))
        await until(lambda: len(starts) >= 3)
        await channel.stop()
        await asyncio.wait_for(task, 10)

    with caplog.at_level(logging.WARNING, logger="attache.channels"):
        asyncio.run(main())
    delays = [r.args[1] for r in caplog.records if "restarting" in r.getMessage()]
    assert delays[:2] == [0.01, 0.01]


def test_a_sidecar_that_cannot_start_never_raises(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    channel = SidecarChannel(
        "fake",
        [str(tmp_path / "missing.exe")],
        tmp_path,
        dict(os.environ),
        lambda *a: True,
        backoff=(0.01, 0.01),
    )

    async def main() -> None:
        task = asyncio.create_task(channel.start(lambda msg: asyncio.sleep(0)))
        await until(lambda: caplog.text.count("channel fake failed") >= 2)
        await channel.send(Outbound("fake:C1", "dropped"))  # not running: logged, not raised
        await channel.stop()
        await asyncio.wait_for(task, 10)

    with caplog.at_level(logging.WARNING, logger="attache.channels"):
        asyncio.run(main())
    assert "channel fake is not running; dropped a send" in caplog.text


SKILL = """---
name: {name}
description: The {name} channel
{metadata}---
"""


def test_sidecars_from_config(
    home: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    for name, metadata in [("slack", "metadata:\n  attache-channel: channel.py\n"), ("signal", "")]:
        (home / "skills" / name).mkdir(parents=True)
        text = SKILL.format(name=name, metadata=metadata)
        (home / "skills" / name / "SKILL.md").write_text(text, encoding="utf-8")
    (home / "attache.toml").write_text(
        '[channels.slack]\nenabled = true\nteam = "T0FAMILY"\nhome = "not the home"\n'
        "[channels.signal]\nenabled = true\n"
        "[channels.discord]\nenabled = true\n"
        "[channels.matrix]\nenabled = false\n",
        encoding="utf-8",
    )
    cfg = config.load(home)
    monkeypatch.setattr(shutil, "which", lambda name: "C:/tools/uv.exe" if name == "uv" else None)
    asked: list[str] = []

    def on_answer_for(name: str) -> Any:
        asked.append(name)
        return lambda ask_id, sender_id, choice: True

    with caplog.at_level(logging.WARNING, logger="attache.channels"):
        made = channels.sidecars(cfg, on_answer_for)
    assert [c.name for c in made] == ["slack"] and asked == ["slack"]
    slack = made[0]
    folder = home / "skills" / "slack"
    assert slack.argv == ["C:/tools/uv.exe", "run", str(folder / "channel.py")]
    assert slack.cwd == folder
    inbox = home / "files" / "inbox" / "slack"
    assert inbox.is_dir()
    assert slack.env["ATTACHE_HOME"] == str(home)  # a setting cannot override it
    assert (slack.env["ATTACHE_CHANNEL"], slack.env["ATTACHE_FILES"]) == ("slack", str(inbox))
    assert slack.env["ATTACHE_TEAM"] == "T0FAMILY"
    assert slack.env["PATH"] == os.environ["PATH"]
    assert "signal skipped" in caplog.text and "discord skipped" in caplog.text
    assert "matrix" not in caplog.text  # disabled: not even mentioned

    caplog.clear()
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with caplog.at_level(logging.WARNING, logger="attache.channels"):
        assert channels.sidecars(cfg, on_answer_for) == []
    assert "uv is not installed" in caplog.text
