from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import shutil
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from attache import channels, config, skills
from attache.channels import ChannelManager, SidecarChannel, TerminalChannel, choose
from attache.types import Inbound, OnMessage, Outbound

OPTIONS = ["Approve", "Decline"]


class Channel:
    """A fake channel. Every channel has ask(): this one records the question and says
    whether anyone saw it (sees), or fails as a broken sidecar would."""

    def __init__(
        self, name: str, log: list[str] | None = None, sees: bool = True, fails: bool = False
    ) -> None:
        self.name, self.sent, self.started, self.stopped = name, [], False, False
        self.log = log if log is not None else []
        self.asked: list[tuple[str, str, str, list[str]]] = []
        self.sees, self.fails = sees, fails

    async def start(self, on_message: OnMessage) -> None:
        self.started = True
        await on_message(Inbound(self.name, "c1", "u1", "U", f"hello from {self.name}"))

    async def send(self, msg: Outbound) -> None:
        self.sent.append(msg)

    async def ask(self, chat_id: str, ask_id: str, text: str, options: list[str]) -> bool:
        if self.fails:
            raise ConnectionError("the sidecar is down")
        self.asked.append((chat_id, ask_id, text, options))
        return self.sees

    async def stop(self) -> None:
        self.stopped = True
        self.log.append(f"{self.name} stopped")


async def until(condition: Callable[[], Any], timeout: float = 10.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not condition():
        assert loop.time() < end, "timed out"
        await asyncio.sleep(0.01)


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
        await until(lambda: len(received) == 3)
        await hub.send(Outbound("local:me", "to both"))
        await hub.send(Outbound("slack:C1:thread", "to slack"))
        with caplog.at_level(logging.WARNING):
            await hub.send(Outbound("discord:x", "lost"))  # logged, not raised
        await hub.stop()

    asyncio.run(main())
    assert [m.text for m in terminal.sent] == [m.text for m in web.sent] == ["to both"]
    assert [m.text for m in slack.sent] == ["to slack"]
    assert all(c.started and c.stopped for c in (terminal, web, slack))
    assert "discord:x" in caplog.text


def test_delivery_returns_at_once_and_stop_cancels_the_turns_first() -> None:
    log: list[str] = []
    hub = ChannelManager()
    hub.add(Channel("local", log))
    hub.add(Channel("slack", log))

    async def turn(msg: Inbound) -> None:
        try:
            await asyncio.Event().wait()  # a turn that never ends by itself
        except asyncio.CancelledError:
            log.append(f"turn in {msg.channel} cancelled")
            raise

    async def main() -> None:
        # Each channel's start() awaits its callback, and still returns at once.
        await asyncio.wait_for(asyncio.gather(*await hub.start(turn)), 1)
        await until(lambda: len(hub.tasks) == 2)
        await hub.stop()
        assert hub.tasks == set()

    asyncio.run(main())
    assert sorted(log[:2]) == ["turn in local cancelled", "turn in slack cancelled"]
    assert sorted(log[2:]) == ["local stopped", "slack stopped"]  # channels after the turns


def test_choose() -> None:
    assert choose(OPTIONS, "approve") == choose(OPTIONS, " 1 ") == choose(OPTIONS, "APPROVE!")
    assert choose(OPTIONS, "approve") == "Approve"
    assert choose(OPTIONS, "2") == choose(OPTIONS, "decline.") == "Decline"
    assert choose(OPTIONS, "3") is choose(OPTIONS, "maybe") is choose(OPTIONS, "0") is None


async def asked(hub: ChannelManager, *question: Any) -> str | None:
    """ask(), then wait for its answer, as the tools do."""
    waiting = await hub.ask(*question)
    assert waiting is not None, "nobody saw the question"
    return await waiting


def test_ask_shows_the_question_and_takes_one_persons_answer() -> None:
    hub = ChannelManager()
    down, up = Channel("slack", sees=False), Channel("slack")
    hub.add(down)
    hub.add(up)

    async def main() -> None:
        answer = await hub.ask("slack:D0ALEX", "owner", "Send it?", OPTIONS)  # once shown
        assert isinstance(answer, asyncio.Task) and not answer.done()
        ((chat_id, ask_id, text, options),) = up.asked
        assert (chat_id, text, options) == ("D0ALEX", "Send it?", OPTIONS)
        assert down.asked == up.asked  # every channel of the chat is asked; one seeing it is enough
        assert len(ask_id) == 16 and list(hub.pending) == [ask_id]  # 64 random bits
        assert hub.pending[ask_id].text == "Send it?"  # for the page to rebuild after a reload
        assert down.sent == up.sent == []  # each channel shows it its own way, through ask()
        assert not hub.answer(ask_id, "sam", "Approve")  # only the person asked
        assert not hub.answer("unknown", "owner", "Approve")
        assert not hub.answer(ask_id, "owner", "Maybe")  # not one of the options
        assert hub.answer(ask_id, "owner", "approve")
        assert await answer == "Approve"
        assert hub.pending == {}
        assert not hub.answer(ask_id, "owner", "Approve")  # answered already: gone

    asyncio.run(main())


def test_ask_gives_none_at_once_when_nobody_can_see_it() -> None:
    hub = ChannelManager()
    began = time.monotonic()
    assert asyncio.run(hub.ask("slack:D1", "owner", "Hello?", OPTIONS)) is None  # no channel
    hub.add(Channel("slack", sees=False))  # e.g. its sidecar is down
    assert asyncio.run(hub.ask("slack:D1", "owner", "Hello?", OPTIONS)) is None
    assert time.monotonic() - began < 2  # not the 600 s timeout
    assert hub.pending == {} and hub.tasks == set()  # never shown: nothing waits for it


def test_ask_gives_none_once_stop_has_begun() -> None:
    hub = ChannelManager()
    hub.add(Channel("local"))

    async def main() -> None:
        await hub.stop()
        assert await hub.ask("local:me", "owner", "Still there?", OPTIONS) is None
        assert hub.pending == {} and hub.tasks == set()

    asyncio.run(main())


def test_ask_gives_none_on_timeout() -> None:
    hub = ChannelManager()
    hub.add(Channel("local"))
    hub.ask_seconds = 0.05
    assert asyncio.run(asked(hub, "local:me", "owner", "Hello?", ["Yes"])) is None
    assert hub.pending == {}


def test_a_failing_channel_leaves_nothing_pending() -> None:
    hub = ChannelManager()
    hub.add(Channel("slack", fails=True))
    with pytest.raises(ConnectionError):
        asyncio.run(hub.ask("slack:D1", "owner", "Ship it?", OPTIONS))
    assert hub.pending == {}


def test_a_typed_reply_answers_only_the_one_question_waiting_for_its_sender() -> None:
    hub = ChannelManager()
    hub.add(Channel("local"))
    hub.add(Channel("slack"))

    async def main() -> None:
        tea = asyncio.create_task(asked(hub, "local:me", "owner", "Tea?", ["Yes", "No"]))
        colour = asyncio.create_task(asked(hub, "slack:C1", "sam", "Which?", ["Red", "Blue"]))
        theirs = asyncio.create_task(asked(hub, "local:me", "sam", "Sam's?", ["Yes", "No"]))
        await until(lambda: len(hub.pending) == 3)
        assert not hub.match("slack:C1", "sam", "green")  # not an option
        assert not hub.match("slack:C2", "sam", "red")  # another chat
        assert hub.match("slack:C1", "sam", " 2 ")
        assert hub.match("local:me", "owner", "YES.")  # sam's question here is not the owner's
        assert await tea == "Yes" and await colour == "Blue"
        assert not hub.match("local:me", "owner", "yes")  # nothing waits for the owner now
        theirs.cancel()

    asyncio.run(main())


def test_with_two_questions_open_a_typed_reply_is_just_a_message() -> None:
    hub = ChannelManager()
    hub.add(Channel("local"))

    async def main() -> None:
        # A member's request and the owner's own question, both waiting for the owner: a
        # typed "Approve" must not be taken for either, or a member could aim it at theirs.
        mine = asyncio.create_task(asked(hub, "local:me", "owner", "Send it?", OPTIONS))
        request = asyncio.create_task(asked(hub, "local:me", "owner", "Sam asks…", OPTIONS))
        await until(lambda: len(hub.pending) == 2)
        assert not hub.match("local:me", "owner", "Approve")
        assert not mine.done() and not request.done()
        ask_id = next(i for i, p in hub.pending.items() if p.text == "Sam asks…")
        assert hub.answer(ask_id, "owner", "Decline")  # a button still answers
        assert await request == "Decline"
        assert hub.match("local:me", "owner", "1")  # now only one is open
        assert await mine == "Approve"

    asyncio.run(main())


def test_spawn_refuses_work_once_stop_has_begun() -> None:
    hub, ran = ChannelManager(), []

    async def follow_up() -> None:
        ran.append("follow-up")

    async def turn() -> None:
        try:
            await asyncio.Event().wait()
        finally:  # cancelled by stop(), it tries to start more work
            assert hub.spawn(follow_up()) is None

    async def main() -> None:
        hub.spawn(turn())
        await asyncio.sleep(0)
        await hub.stop()
        late = follow_up()
        assert hub.spawn(late) is None and late.cr_frame is None  # closed, never run
        await asyncio.sleep(0.05)

    asyncio.run(main())
    assert ran == [] and hub.tasks == set()


class Showing(Channel):
    """A fake channel that shows activity, as the terminal and the web page do."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.shown: list[tuple[str, str, dict[str, Any]]] = []

    async def activity(self, chat_id: str, kind: str, data: dict[str, Any]) -> None:
        self.shown.append((chat_id, kind, data))


def test_activity_reaches_only_the_chats_channels_that_show_it() -> None:
    hub = ChannelManager()
    plain, terminal, web, slack = (
        Channel("local"),
        Showing("local"),
        Showing("local"),
        Showing("slack"),
    )
    for channel in (plain, terminal, web, slack):
        hub.add(channel)

    async def main() -> None:
        await hub.activity("local:me", "step", label="search mail", seconds=1.2)
        await hub.activity("local:me", "finished", seconds=3.0, tokens=None, route="claude")

    asyncio.run(main())
    assert (
        terminal.shown
        == web.shown
        == [
            ("me", "step", {"label": "search mail", "seconds": 1.2}),
            ("me", "finished", {"seconds": 3.0, "tokens": None, "route": "claude"}),
        ]
    )
    assert slack.shown == [] and plain.sent == []  # another chat; one without activity() skipped


def test_stop_turns_cancels_the_turns_of_that_chat_only() -> None:
    hub, log = ChannelManager(), []
    hub.add(Channel("local"))
    hub.add(Channel("slack"))

    async def turn(msg: Inbound) -> None:
        try:
            await asyncio.Event().wait()  # a long turn
        except asyncio.CancelledError:
            log.append(f"{msg.channel} stopped")
            raise

    def running() -> list[str]:
        return sorted(task.get_name() for task in hub.tasks)

    async def main() -> None:
        other = hub.spawn(asyncio.Event().wait())  # other work, such as a question's wait
        await asyncio.gather(*await hub.start(turn))
        await until(lambda: len(hub.tasks) == 3)
        assert {"local:c1", "slack:c1"} < set(running())  # each message's task: its chat key
        assert hub.stop_turns("local:c1") is True
        await until(lambda: log == ["local stopped"])
        await until(lambda: len(hub.tasks) == 2)  # a done one is forgotten
        assert hub.stop_turns("local:c1") is False  # nothing is running there now
        assert hub.stop_turns("local:me") is False
        assert other is not None and not other.done()  # not a chat's: left alone
        await hub.stop()

    asyncio.run(main())
    assert log == ["local stopped", "slack stopped"] and hub.tasks == set()


def test_summary_and_footer() -> None:
    assert channels.summary("I should check  the\ninbox first. Then draft.") == (
        "I should check the inbox first."
    )
    long = channels.summary("word " * 40)
    assert len(long) == 100 and long.endswith("…")
    assert channels.summary("No full stop") == "No full stop"
    finished = {"seconds": 4.25, "route": "claude"}
    assert channels.footer({**finished, "tokens": 3140}) == "4.2s · 3.1k tokens · claude"
    assert channels.footer({**finished, "tokens": 812}) == "4.2s · 812 tokens · claude"
    assert channels.footer({**finished, "tokens": None}) == "4.2s · claude"  # not known
    assert channels.footer({**finished, "tokens": 9, "stopped": True}) == "stopped · 4.2s"


def test_terminal_trail(capsys: pytest.CaptureFixture[str]) -> None:
    term = TerminalChannel("Alex", "http://x")
    thinking = "The user wants the boiler email. I'll search for it.\nThen read it."
    events = [
        ("started", {"route": "claude"}),
        ("thinking", {"text": thinking}),
        ("step", {"label": 'mail search "boiler"', "seconds": 1.23}),
        ("step", {"label": "Bash ls -la", "seconds": None}),  # Claude Code's own: not timed
        ("finished", {"seconds": 4.2, "tokens": 3100, "route": "claude", "stopped": False}),
        ("started", {"route": "claude"}),
        ("finished", {"seconds": 1, "tokens": None, "route": "claude", "stopped": True}),
        ("later", {}),  # a kind it doesn't know: nothing
    ]

    async def main() -> None:
        for kind, data in events:
            await term.activity("me", kind, data)

    asyncio.run(main())
    assert capsys.readouterr().out.splitlines() == [
        "  working…",
        "  ∴ The user wants the boiler email.",  # one line: the first sentence
        '  · mail search "boiler"  1.2s',
        "  · Bash ls -la",
        "  4.2s · 3.1k tokens · claude",
        "  working…",
        "  stopped · 1.0s",  # Ctrl+C, or the page's Stop
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


def test_a_terminal_turn_can_wait_for_an_answer_typed_in_the_terminal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("please ask\n1\n/quit\n"))
    hub, term, results = ChannelManager(), TerminalChannel("Alex", "http://x"), []
    hub.add(term)

    async def handle(msg: Inbound) -> None:  # a router in miniature
        if not hub.match(msg.chat_key, "owner", msg.text):
            results.append(await asked(hub, msg.chat_key, "owner", "Sure?", ["Yes", "No"]))

    async def main() -> None:
        await asyncio.wait_for(asyncio.gather(*await hub.start(handle)), 5)
        await until(lambda: results)
        await hub.stop()

    asyncio.run(main())
    assert results == ["Yes"]  # the answer reached the turn that was waiting for it
    assert "attaché> Sure?\nReply 1 for Yes, 2 for No." in capsys.readouterr().out


def test_terminal_mirrors_web_messages_but_not_its_own(capsys: pytest.CaptureFixture[str]) -> None:
    term = TerminalChannel("Alex", "http://x")

    async def main() -> None:
        await term.send(Outbound("local:me", "typed on the web", author="Alex", origin="web"))
        await term.send(Outbound("local:me", "typed here", author="Alex", origin="terminal"))
        await term.send(Outbound("local:me", "a reply", origin="terminal"))

    asyncio.run(main())
    assert capsys.readouterr().out.splitlines() == [
        "Alex (web)> typed on the web",
        "attaché> a reply",
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
import json, os, sys

def emit(**event):
    print(json.dumps(event), flush=True)

inbox = os.environ["ATTACHE_FILES"]
emit(type="ready", self_id="B0T", name="Bot")
print("not json", flush=True)
emit(type="unknown")
files = [os.path.join(inbox, "photo.jpg"), os.path.join(inbox, "..", "elsewhere.txt"), __file__]
emit(type="message", chat_id="C1", sender_id="U0SAM", sender_name="Sam", text="hi",
     is_group=True, mentioned=True, message_id="m1", files=files, extra="ignored")
emit(type="message", chat_id="C1", sender_id="U0SAM", sender_name="Sam", text="x" * 1_000_000)
for line in sys.stdin:
    event = json.loads(line)
    if event["type"] in ("send", "answered"):
        emit(type="message", chat_id="C1", sender_id="U0SAM", sender_name="Echo", text=line)
    elif event["type"] == "ask":
        emit(type="answer", ask_id=event["ask_id"], sender_id="U0ALEX", choice=event["options"][0])
    elif event["type"] == "stop":
        break
"""


def sidecar(tmp_path: Path, code: str, **kwargs: Any) -> SidecarChannel:
    script = tmp_path / "sidecar.py"
    script.write_text(code, encoding="utf-8")
    (tmp_path / "inbox").mkdir(exist_ok=True)
    env = {**os.environ, "ATTACHE_FILES": str(tmp_path / "inbox")}
    on_answer = kwargs.pop("on_answer", lambda channel, ask_id, sender_id, choice: True)
    return SidecarChannel("fake", [sys.executable, str(script)], tmp_path, env, on_answer, **kwargs)


def test_sidecar_speaks_the_protocol(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    answers: list[tuple[str, ...]] = []

    def on_answer(*answer: str) -> bool:
        answers.append(answer)
        return len(answers) == 1  # the first answer is accepted, the second refused

    channel = sidecar(tmp_path, ECHO, on_answer=on_answer)
    received: list[Inbound] = []

    async def on_message(msg: Inbound) -> None:
        received.append(msg)

    async def main() -> None:
        task = asyncio.create_task(channel.start(on_message))
        await until(lambda: len(received) == 2)
        await channel.send(Outbound("fake:C1", "09:40 from Portsmouth", ["notes.txt"], "m1"))
        await until(lambda: len(received) == 3)
        assert await channel.ask("D0ALEX", "a19", "Send it?", OPTIONS) is True
        await until(lambda: len(received) == 4)
        await channel.ask("D0ALEX", "a20", "And this?", OPTIONS)
        await until(lambda: len(received) == 5)
        await channel.stop()
        await asyncio.wait_for(task, 10)  # start() returns once stopped

    with caplog.at_level(logging.INFO, logger="attache.channels"):
        asyncio.run(main())
    first, long = received[0], received[1]
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
    assert first.files == [str(tmp_path / "inbox" / "photo.jpg")]  # the rest is outside the inbox
    assert long.text == "x" * 1_000_000  # longer than asyncio's default 64 KiB line limit
    assert json.loads(received[2].text) == {
        "type": "send",
        "chat_id": "C1",
        "text": "09:40 from Portsmouth",
        "files": [str((Path.cwd() / "notes.txt").resolve())],  # absolute
        "reply_to": "m1",
    }
    assert answers == [("fake", "a19", "U0ALEX", "Approve"), ("fake", "a20", "U0ALEX", "Approve")]
    replies = [json.loads(m.text) for m in received[3:]]
    assert replies == [
        {"type": "answered", "ask_id": "a19", "sender_id": "U0ALEX", "accepted": True},
        {"type": "answered", "ask_id": "a20", "sender_id": "U0ALEX", "accepted": False},
    ]
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


TOO_LONG = """
import json, time
print(json.dumps({"type": "log", "text": "x" * 5000}), flush=True)
time.sleep(60)  # still running after the bad line
"""


def test_a_reader_error_kills_the_sidecar_before_the_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setitem(channels.SPAWN, "limit", 1000)  # so the 5,000-byte line breaks reading
    channel = sidecar(tmp_path, TOO_LONG, backoff=(0.01, 0.01))
    runs: list[Any] = []

    async def main() -> None:
        task = asyncio.create_task(channel.start(lambda msg: asyncio.sleep(0)))
        await until(lambda: channel.proc is not None)
        runs.append(channel.proc)
        await until(lambda: "restarting" in caplog.text)
        await channel.stop()
        await asyncio.wait_for(task, 10)

    with caplog.at_level(logging.WARNING, logger="attache.channels"):
        asyncio.run(main())
    assert "channel fake failed" in caplog.text
    assert runs[0].returncode is not None  # killed, not left sleeping for a minute


TREE = """
import subprocess, sys, time
subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])  # shares our stdout
print("ready", flush=True)
time.sleep(60)
"""


def test_kill_tree_kills_the_children_too(tmp_path: Path) -> None:
    script = tmp_path / "tree.py"
    script.write_text(TREE, encoding="utf-8")

    async def main() -> None:
        proc = await asyncio.create_subprocess_exec(sys.executable, str(script), **channels.SPAWN)
        assert (await proc.stdout.readline()).strip() == b"ready"
        await channels.kill_tree(proc)
        assert proc.returncode is not None
        # The pipe ends only when every process holding it, the grandchild too, has died.
        assert await asyncio.wait_for(proc.stdout.read(), 10) == b""

    asyncio.run(main())


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
        assert await channel.ask("D1", "a1", "Seen?", OPTIONS) is False  # so ask fails fast
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


def channel_skill(home: Path, name: str, script: str | None, file: str | None = None) -> None:
    folder = home / "skills" / name
    folder.mkdir(parents=True)
    metadata = f"metadata:\n  attache-channel: {json.dumps(script)}\n" if script else ""
    (folder / "SKILL.md").write_text(SKILL.format(name=name, metadata=metadata), encoding="utf-8")
    if file:
        (folder / file).write_text("", encoding="utf-8")


def test_channel_script_is_a_plain_file_name_in_the_skill_folder(home: Path) -> None:
    channel_skill(home, "good", "channel.py", file="channel.py")
    for name, script in [("dots", ".."), ("up", "../channel.py"), ("deep", "sub/channel.py")]:
        channel_skill(home, name, script, file="channel.py")
    channel_skill(home, "gone", "channel.py")  # names a file that is not there
    channel_skill(home, "none", None)
    (home / "skills" / "odd").mkdir()
    (home / "skills" / "odd" / "SKILL.md").write_text(
        "---\nname: odd\ndescription: d\nmetadata: just text\n---\n", encoding="utf-8"
    )
    found = {skill.name: channels.channel_script(skill) for skill in skills.discover(home)}
    assert found.pop("good") == home / "skills" / "good" / "channel.py"
    assert set(found.values()) == {None}
    assert set(found) == {"dots", "up", "deep", "gone", "none", "odd"}


def test_sidecars_from_config(
    home: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    channel_skill(home, "slack", "channel.py", file="channel.py")
    channel_skill(home, "signal", None)
    for name, script in [("dots", ".."), ("up", "../channel.py"), ("deep", "sub/channel.py")]:
        channel_skill(home, name, script)
    channel_skill(home, "gone", "channel.py")  # names a file that is not there
    enabled = "".join(f"[channels.{n}]\nenabled = true\n" for n in ("dots", "up", "deep", "gone"))
    (home / "attache.toml").write_text(
        '[endpoints.mine]\napi = "anthropic"\nmodel = "m"\nkey_env = "MY_MODEL_KEY"\n'
        '[channels.slack]\nenabled = true\nteam = "T0FAMILY"\nhome = "not the home"\n'
        "[channels.signal]\nenabled = true\n"
        "[channels.discord]\nenabled = true\n"
        "[channels.matrix]\nenabled = false\n" + enabled,
        encoding="utf-8",
    )
    for secret in ("MY_MODEL_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.setenv(secret, "sk-secret")
    monkeypatch.setenv("ATTACHE_WEB_URL", "http://127.0.0.1:8765")
    cfg = config.load(home)
    monkeypatch.setattr(shutil, "which", lambda name: "C:/tools/uv.exe" if name == "uv" else None)

    def on_answer(channel: str, ask_id: str, sender_id: str, choice: str) -> bool:
        return True

    with caplog.at_level(logging.WARNING, logger="attache.channels"):
        made = channels.sidecars(cfg, on_answer)
    assert [c.name for c in made] == ["slack"]
    slack = made[0]
    folder = home / "skills" / "slack"
    assert slack.argv == ["C:/tools/uv.exe", "run", str(folder / "channel.py")]
    assert slack.cwd == folder and slack.on_answer is on_answer
    inbox = home / "files" / "inbox" / "slack"
    assert inbox.is_dir()
    assert slack.env["ATTACHE_HOME"] == str(home)  # a setting cannot override it
    assert (slack.env["ATTACHE_CHANNEL"], slack.env["ATTACHE_FILES"]) == ("slack", str(inbox))
    assert (slack.env["ATTACHE_TEAM"], slack.env["ATTACHE_ENABLED"]) == ("T0FAMILY", "True")
    assert slack.env["ATTACHE_WEB_URL"] == "http://127.0.0.1:8765"
    assert slack.env["PATH"] == os.environ["PATH"]
    assert not {"MY_MODEL_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"} & set(slack.env)
    for skipped in ("signal", "discord", "dots", "up", "deep", "gone"):
        assert f"channel {skipped} skipped" in caplog.text
    assert "matrix" not in caplog.text  # disabled: not even mentioned

    caplog.clear()
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with caplog.at_level(logging.WARNING, logger="attache.channels"):
        assert channels.sidecars(cfg, on_answer) == []
    assert "uv is not installed" in caplog.text
