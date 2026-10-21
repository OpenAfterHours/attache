"""The tui skill's logic: the link it reads, the child it starts and stops, the event stream,
and the state events build. Textual and httpx are never imported, and no child is started."""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from collections import deque
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from attache import skills, tools

FOLDER = skills.BUNDLED / "tui"
SCRIPT = FOLDER / "scripts" / "tui.py"
LINK = "Open http://127.0.0.1:8765/#t=Ab-c_9\n"


def load(path: Path = SCRIPT) -> ModuleType:
    spec = importlib.util.spec_from_file_location("attache_tui", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up while the file runs
    spec.loader.exec_module(module)
    return module


tui = load()


def test_owner_only_with_textual_and_httpx_in_its_header() -> None:
    meta, body = skills.parse((FOLDER / "SKILL.md").read_text(encoding="utf-8"))
    assert meta["name"] == "tui" and "metadata" not in meta
    assert not tools.for_members(skills.Skill("tui", meta["description"], FOLDER, meta))
    assert "attache skill tui" in body and "Never run it with the `script` tool" in body
    header = re.search(r"# /// script\n(.*?)# ///", SCRIPT.read_text(encoding="utf-8"), re.S)
    assert header and '"textual>=' in header[1] and '"httpx>=' in header[1]
    assert "textual" not in sys.modules or "attache_tui" in sys.modules  # loading needs neither


# Starting Attaché


def test_the_child_is_this_attache(tmp_path: Path) -> None:
    cli = [sys.executable, "-m", "attache"]
    env = {"ATTACHE_CLI": json.dumps(cli), "ATTACHE_HOME": str(tmp_path)}
    assert tui.child_argv(env) == [*cli, "run", "--home", str(tmp_path)]
    plain = tui.child_argv({"ATTACHE_HOME": str(tmp_path)})
    assert plain == ["attache", "run", "--home", str(tmp_path)]


def test_without_attache_home_the_home_is_where_the_skill_is(home: Path) -> None:
    skills.install_bundled(home)
    installed = load(home / "skills" / "tui" / "scripts" / "tui.py")
    assert installed.child_argv({})[-1] == str(home.resolve())


def test_the_link_gives_the_address_and_the_token() -> None:
    lines = iter(["Starting\n", "warning: no git\n", LINK, "INFO later\n"])
    seen: deque[str] = deque(maxlen=200)
    assert tui.read_link(lines, seen) == ("http://127.0.0.1:8765", "Ab-c_9")
    assert list(seen) == ["Starting", "warning: no git"]  # the link, with the token, is not kept
    assert next(lines) == "INFO later\n"  # the rest is left for the log pane
    other = tui.read_link(["Open http://localhost:9000/#t=x\n"], [])
    assert other == ("http://localhost:9000", "x")


def test_a_child_that_stops_first_explains_itself() -> None:
    seen: list[str] = []
    lines = ["", "Port 8765 is in use. Is `attache run` already running?\n"]
    assert tui.read_link(lines, seen) is None
    said = tui.early_exit(seen, 1)
    assert said.splitlines() == [
        "Attaché stopped before it was ready (exit code 1).",
        "Port 8765 is in use. Is `attache run` already running?",
        "Attaché may already be running; stop it or use its web page.",
    ]


def test_start_puts_the_child_in_its_own_group() -> None:
    calls = []
    tui.start(["attache", "run"], popen=lambda argv, **options: calls.append((argv, options)))
    (argv, options), *_ = calls
    assert argv == ["attache", "run"]
    assert options["stdout"] == subprocess.PIPE and options["stderr"] == subprocess.STDOUT
    assert options["stdin"] == subprocess.DEVNULL and options["encoding"] == "utf-8"
    if sys.platform == "win32":  # CTRL_BREAK reaches it; the terminal's Ctrl+C does not
        assert options["creationflags"] == subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert options["start_new_session"] is True


class Child:
    """A fake child process: it exits on the signal, or never (stubborn)."""

    def __init__(self, stubborn: bool = False, exited: bool = False) -> None:
        self.pid, self.signals, self.stubborn, self.code = 4242, [], stubborn, 0 if exited else None
        self.waits: list[float | None] = []

    def poll(self) -> int | None:
        return self.code

    def send_signal(self, number: int) -> None:
        self.signals.append(number)

    def wait(self, timeout: float | None = None) -> int:
        self.waits.append(timeout)
        if self.stubborn and timeout is not None:
            raise subprocess.TimeoutExpired("attache", timeout)
        self.code = 0
        return 0


@pytest.mark.parametrize("windows", [True, False])
def test_stop_asks_first_then_kills_the_tree(
    windows: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tui.signal, "CTRL_BREAK_EVENT", "break", raising=False)  # Windows'
    monkeypatch.setattr(tui.signal, "SIGKILL", 9, raising=False)  # POSIX's, on any system
    killed, ran = [], []
    monkeypatch.setattr(tui.os, "killpg", lambda pid, number: killed.append(pid), raising=False)
    gentle = Child()
    tui.stop(gentle, windows=windows, run=lambda argv, **o: ran.append(argv))
    assert gentle.signals == ["break" if windows else tui.signal.SIGINT]
    assert gentle.waits == [10.0] and ran == killed == []
    stubborn = Child(stubborn=True)
    tui.stop(stubborn, windows=windows, run=lambda argv, **o: ran.append(argv))
    if windows:
        assert ran == [["taskkill", "/T", "/F", "/PID", "4242"]] and killed == []
    else:
        assert killed == [4242] and ran == []
    assert stubborn.waits == [10.0, None]


def test_main_says_why_when_attache_stops_first(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    child = Child(exited=True)
    child.stdout = iter(["Port 8765 is in use. Is `attache run` already running?\n"])
    child.wait = lambda timeout=None: 1
    monkeypatch.setattr(tui, "start", lambda argv: child)
    monkeypatch.setattr(tui, "make_app", lambda *a: pytest.fail("no app without a link"))
    assert tui.main() == 1
    err = capsys.readouterr().err
    assert "exit code 1" in err and "Port 8765 is in use" in err and tui.HINT in err
    assert child.signals == []  # it had exited: nothing to stop


def test_main_runs_the_app_then_stops_the_child(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    child, made, stopped = Child(), [], []
    child.stdout = iter(["hello\n", LINK])
    app = SimpleNamespace(run=lambda: made.append("ran"), goodbye="", code=0)
    monkeypatch.setattr(tui, "start", lambda argv: child)
    monkeypatch.setattr(tui, "make_app", lambda *args: made.append(args[1:3]) or app)
    monkeypatch.setattr(tui, "stop", lambda proc: stopped.append(proc))
    assert tui.main() == 0
    assert made == [("http://127.0.0.1:8765", "Ab-c_9"), "ran"] and stopped == [child]
    assert "Ab-c_9" not in capsys.readouterr().out  # the token is never shown


# The event stream


def test_server_sent_events() -> None:
    """The lines web.WebChannel.stream sends: "event: <kind>" then one "data: <json>"."""
    activity = {"kind": "step", "label": "script mail inbox", "seconds": 1.1}
    stream = [": connected", "", "event: activity", f"data: {json.dumps(activity)}", ""]
    stream += [": ping", "", "event: message", 'data: {"text": "hi"}', "", "event: bye"]
    kind, got = "", []
    for line in stream:
        kind, data = tui.sse(line, kind)
        if data is not None:
            got.append((kind, data))
    assert got == [("activity", activity), ("message", {"text": "hi"})] and kind == "bye"


# The state events build


def test_history_loads_like_the_web_page() -> None:
    state = tui.State()
    question = {"ask_id": "a1", "text": "Send the draft to Dan?", "options": ["Send", "Not now"]}
    state.load(
        {
            "messages": [
                {"role": "user", "text": "hi", "sender_name": "Alex"},
                {"role": "user", "text": "Reminder: call Sam", "sender_name": "Reminder"},
                {"role": "assistant", "text": "Hello!\nHow can I help?", "sender_name": ""},
                {"role": "assistant", "text": "Send the draft to Dan?", "sender_name": ""},
            ],
            "asks": [question],
        }
    )
    assert state.lines() == [
        ("you", "you      hi"),
        ("bot", "attaché  Reminder: call Sam"),  # reminders are Attaché's
        ("bot", "attaché  Hello!"),
        ("bot", "         How can I help?"),
        ("ask", "Send the draft to Dan?"),  # once, with its options, last
        ("ask", "1 Send   2 Not now   (a number answers)"),
    ]
    assert state.open_ask() is question
    state.answered("a1", "Send")
    assert state.open_ask() is None and state.lines()[-1] == ("footer", "→ Send")


def test_a_turn_from_start_to_finish() -> None:
    state = tui.State()
    state.activity({"kind": "started", "route": "claude"}, now=100.0)
    assert state.route == "claude"  # for the header
    assert state.status(now=101.0, frame=0) == "⠋ Thinking · 1s · esc to stop"
    state.activity({"kind": "thinking", "text": "Calendar first. Then mail from Dan."}, 101.0)
    state.activity({"kind": "step", "label": "script mail agenda", "seconds": 1.1}, 102.0)
    state.activity({"kind": "step", "label": "Bash ls -la", "seconds": None}, 103.0)
    assert state.status(now=105.4, frame=3) == "⠸ Bash ls -la · 5s · esc to stop"
    state.message({"chat_key": "local:me", "text": "Tomorrow: the dentist at 9.", "files": []})
    assert state.turn is not None  # the reply comes before "finished"
    state.activity({"kind": "finished", "seconds": 14.2, "tokens": 3120, "route": "claude"}, 114)
    assert state.turn is None
    assert state.status(200, 0) == "✓ done · 14.2s · 3.1k tokens · claude"
    assert state.lines() == [
        ("think", "▸ Thinking · Calendar first.   ctrl+o"),
        ("step", "● script mail agenda  ✓ 1.1s"),
        ("step", "● Bash ls -la"),  # Claude Code's own tools have no time
        ("bot", "attaché  Tomorrow: the dentist at 9."),
        ("footer", "14.2s · 3.1k tokens · claude"),
    ]
    state.expanded = True  # ctrl+o
    assert state.lines()[:3] == [
        ("think", "▾ Thinking"),
        ("think", "  Calendar first. Then mail from Dan."),
        ("step", "● script mail agenda  ✓ 1.1s"),
    ]


def test_every_turn_keeps_its_steps_and_folds_its_thinking() -> None:
    state = tui.State()
    for question in ("first", "second"):
        state.entries.append(("you", question))
        state.activity({"kind": "started", "route": "claude"}, 0)
        state.activity({"kind": "thinking", "text": f"About the {question} one. More."}, 0)
        state.activity({"kind": "step", "label": f"db list {question}", "seconds": 0.2}, 0)
        state.activity({"kind": "finished", "seconds": 2.5, "tokens": 800, "route": "claude"}, 0)
    lines = state.lines()
    for question in ("first", "second"):
        assert ("think", f"▸ Thinking · About the {question} one.   ctrl+o") in lines
        assert ("step", f"● db list {question}  ✓ 0.2s") in lines
    assert lines.count(("footer", "2.5s · 800 tokens · claude")) == 2


def test_a_stopped_turn() -> None:
    state = tui.State()
    state.activity({"kind": "started", "route": "claude"}, 0)
    state.activity({"kind": "finished", "seconds": 3.24, "tokens": None, "stopped": True}, 3)
    assert state.status(5, 0) == "■ stopped · 3.2s"
    assert state.lines()[-1] == ("footer", "stopped · 3.2s") and state.turn is None


def test_activity_while_the_app_was_away_starts_a_turn() -> None:
    state = tui.State()
    state.activity({"kind": "step", "label": "read_file memory/USER.md", "seconds": 0.1}, 50)
    assert state.turn is not None
    assert state.lines() == [("step", "● read_file memory/USER.md  ✓ 0.1s")]
    state.activity({"kind": "finished", "seconds": 1, "tokens": None, "route": ""}, 51)
    assert state.lines()[-1] == ("footer", "1.0s")  # no tokens or route given: left out


def test_messages_from_elsewhere() -> None:
    state = tui.State()
    state.message({"chat_key": "slack:D1", "text": "not this chat"})
    state.message({"chat_key": "local:me", "text": "typed in the terminal", "author": "Alex"})
    state.message({"chat_key": "local:me", "text": "Here it is", "files": ["files/x/plan.docx"]})
    assert state.lines() == [
        ("you", "you      typed in the terminal"),
        ("bot", "attaché  Here it is"),
        ("bot", "         file: files/x/plan.docx"),
    ]


def test_questions_from_the_stream() -> None:
    state = tui.State()
    state.ask({"ask_id": "q1", "text": "Approve?", "options": ["Approve", "Decline"]})
    state.ask({"ask_id": "q2", "text": "Send?", "options": ["Send", "Not now"]})
    assert state.open_ask()["ask_id"] == "q2"  # the latest is the one number keys answer
    state.answered("q2", "(closed)")
    assert state.open_ask()["ask_id"] == "q1"
    assert state.lines() == [
        ("ask", "Approve?"),
        ("ask", "1 Approve   2 Decline   (a number answers)"),
        ("ask", "Send?"),
        ("footer", "→ (closed)"),  # answered elsewhere, or timed out
    ]


def test_small_formats() -> None:
    assert tui.summary("Calendar first, then mail from Dan. After that, the reply.") == (
        "Calendar first, then mail from Dan."
    )
    long = tui.summary("word " * 60)
    assert len(long) == 100 and long.endswith("…")
    assert tui.tokens_text(3120) == "3.1k tokens" and tui.tokens_text(980) == "980 tokens"
    assert tui.tokens_text(None) == ""
