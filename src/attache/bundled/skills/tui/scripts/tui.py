# /// script
# requires-python = ">=3.11"
# dependencies = ["textual>=1.0", "httpx>=0.27"]
# ///
"""Attaché full screen: the owner's chat (local:me) as a terminal app, like Claude Code.

    attache skill tui

It starts Attaché itself, as a child process: $ATTACHE_CLI (a JSON argv, which `attache skill`
sets) plus `run --home $ATTACHE_HOME`, or `attache run ...` without it. The child prints
"Open http://127.0.0.1:<port>/#t=<token>"; the app takes the address and the UI token from
that line and keeps the token in memory only, never in a file. Everything else the child
prints goes to the log pane (ctrl+l). From then on the app is a client of the web API, as the
chat page is: GET /api/messages, the /api/events stream (message, ask, activity, bye), and
POST /api/chat, /api/answer and /api/stop. The child's socket listens before it prints the
link, so the stream connects once; it ends with "bye" when Attaché stops.

Quitting (ctrl+d, or Textual's own ctrl+q) stops the child as Ctrl+C would: CTRL_BREAK on
Windows, where it runs in its own process group, and SIGINT elsewhere. After 10 seconds its
tree is killed.

The logic is in plain functions and State, which the tests import without Textual. Textual
and httpx are imported in make_app().
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping, MutableSequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LINK = re.compile(r"Open (https?://[^\s/]+)/#t=([\w-]+)")  # what `attache run` prints
HINT = "Attaché may already be running; stop it or use its web page."
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
# Its own process group (Windows; start_new_session is ignored there) or session (POSIX), so
# the terminal's keys never reach it, and a CTRL_BREAK or killpg reaches it alone.
NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)  # 0 off Windows
GROUP = {"start_new_session": True, "creationflags": NEW_GROUP}


def clean(text: str) -> str:
    """Text with its control characters taken out (C0 and C1, ESC among them, but not
    newline and tab) before it reaches the screen: Rich's Text would keep them, and a
    message could then move the cursor or retitle the terminal."""
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", text)


def child_argv(env: Mapping[str, str]) -> list[str]:
    """How to start this same Attaché: $ATTACHE_CLI, else `attache`, then `run --home` with
    $ATTACHE_HOME, else the home this copy of the skill is in (<home>/skills/tui/scripts)."""
    cli = json.loads(env["ATTACHE_CLI"]) if env.get("ATTACHE_CLI") else ["attache"]
    home = env.get("ATTACHE_HOME") or str(Path(__file__).resolve().parents[3])
    return [*cli, "run", "--home", home]


def read_link(lines: Iterable[str], seen: MutableSequence[str]) -> tuple[str, str] | None:
    """Read the child's output up to its "Open <url>/#t=<token>" line: (base URL, token), or
    None when the output ends first. The other lines go into `seen`; the link line, which
    holds the token, goes nowhere."""
    for line in lines:
        if match := LINK.search(line):
            return match[1], match[2]
        seen.append(clean(line.rstrip("\n")))
    return None


def early_exit(seen: Iterable[str], code: int | None) -> str:
    """What to say when Attaché stops before it is ready: its last lines, then the hint."""
    said = [line for line in seen if line.strip()][-8:]
    return "\n".join([f"Attaché stopped before it was ready (exit code {code}).", *said, HINT])


def summary(text: str) -> str:
    """A thought's first sentence, at most 100 characters."""
    sentence = re.split(r"(?<=[.!?])\s", " ".join(text.split()), maxsplit=1)[0]
    return sentence if len(sentence) <= 100 else sentence[:99].rstrip() + "…"


def tokens_text(count: int) -> str:
    """3.1k tokens for thousands."""
    return f"{count / 1000:.1f}k tokens" if count >= 1000 else f"{count} tokens"


def sse(line: str, kind: str) -> tuple[str, dict[str, Any] | None]:
    """One line of the event stream, after the event named `kind`: (the name, the data once
    it comes). Attaché sends "event: <name>" then one "data: <json>" line; comments such as
    ": ping" and blank lines carry nothing."""
    if line.startswith("event:"):
        return line[6:].strip(), None
    return kind, json.loads(line[5:]) if line.startswith("data:") else None


@dataclass
class Turn:
    """One turn's activity: when it began (time.monotonic), its steps as (label, seconds),
    and its thinking."""

    since: float
    steps: list[tuple[str, float | None]] = field(default_factory=list)
    thoughts: list[str] = field(default_factory=list)


@dataclass
class State:
    """What the app shows, changed by events. Entries are (kind, item): "you" and "bot" with
    text, "turn" with a Turn, "ask" with the question, and "footer" or "error" with text."""

    entries: list[tuple[str, Any]] = field(default_factory=list)
    asks: dict[str, dict[str, Any]] = field(default_factory=dict)  # the open ones, by ask_id
    turn: Turn | None = None  # the running turn, if one is
    route: str = ""
    done: str = ""  # the status line once a turn is over
    expanded: bool = False  # ctrl+o: every turn's steps and thinking in full

    def load(self, chat: dict[str, Any]) -> None:
        """Start again from GET /api/messages. A stored question that is still open shows
        once, last, with its options."""
        self.entries, self.asks = [], {}
        open_texts = {ask["text"] for ask in chat["asks"]}
        for message in chat["messages"]:
            mine = message["role"] == "user" and message.get("sender_name") != "Reminder"
            if message["text"] not in open_texts:
                self.entries.append(("you" if mine else "bot", message["text"]))
        for ask in chat["asks"]:
            self.ask(ask)

    def message(self, msg: dict[str, Any]) -> None:
        """A "message" event: a reply, or with author set a message typed elsewhere."""
        if msg.get("chat_key") == "local:me":
            files = [f"file: {name}" for name in msg.get("files") or []]
            text = "\n".join([msg.get("text") or "", *files]).strip()
            self.entries.append(("you" if msg.get("author") else "bot", text))

    def ask(self, ask: dict[str, Any]) -> None:
        self.asks[ask["ask_id"]] = ask
        self.entries.append(("ask", ask))

    def answered(self, ask_id: str, choice: str) -> None:
        if ask := self.asks.pop(ask_id, None):
            ask["choice"] = choice

    def open_ask(self) -> dict[str, Any] | None:
        """The latest question still waiting, which number keys answer."""
        return next(reversed(self.asks.values()), None)

    def choice(self, key: str) -> tuple[dict[str, Any], str] | None:
        """(the open question, its option) for a number key such as "2"; else None."""
        ask = self.open_ask()
        if ask and key.isdecimal() and 1 <= int(key) <= len(ask["options"]):  # not "²"
            return ask, ask["options"][int(key) - 1]
        return None

    def activity(self, event: dict[str, Any], now: float) -> None:
        """An "activity" event: {kind: started | step | thinking | finished, ...data}."""
        kind = event.get("kind")
        if kind == "started" or (kind in ("step", "thinking") and self.turn is None):
            self.turn = Turn(since=now)  # also when the app connected while a turn was running
            self.entries.append(("turn", self.turn))
            self.done = ""
        self.route = event.get("route") or self.route
        if kind == "step":
            self.turn.steps.append((str(event.get("label") or "a step"), event.get("seconds")))
        elif kind == "thinking":
            self.turn.thoughts.append(event.get("text") or "")
        elif kind == "finished":
            took = f"{event.get('seconds') or 0.0:.1f}s"
            count = event.get("tokens")  # None when the route did not say
            used = " · ".join(filter(None, [took, count and tokens_text(count), self.route]))
            footer = f"stopped · {took}" if event.get("stopped") else used
            self.done = ("■ " if event.get("stopped") else "✓ done · ") + footer
            self.entries.append(("footer", footer))
            self.turn = None

    def status(self, now: float) -> str:
        """The live line above the input box: the running turn's latest step, or Thinking,
        after a spinner that moves on every tenth of a second."""
        if self.turn is None:
            return self.done
        doing = self.turn.steps[-1][0] if self.turn.steps else "Thinking"
        took = now - self.turn.since
        return f"{SPINNER[int(now * 10) % len(SPINNER)]} {doing} · {took:.0f}s · esc to stop"

    def lines(self) -> list[tuple[str, str]]:
        """The transcript as (style, text) lines, each turn's steps and thinking under it."""
        out: list[tuple[str, str]] = []
        for kind, item in self.entries:
            if kind == "turn":
                out += turn_lines(item, self.expanded)
            elif kind == "ask":
                out += [("ask", line) for line in item["text"].splitlines()]
                if "choice" in item:
                    out.append(("footer", f"→ {item['choice']}"))
                else:
                    options = (f"{n} {option}" for n, option in enumerate(item["options"], 1))
                    out.append(("ask", "   ".join(options) + "   (a number answers)"))
            elif kind in ("you", "bot"):
                first, *rest = item.splitlines() or [""]
                out.append((kind, f"{'you' if kind == 'you' else 'attaché':<9}{first}"))
                out += [(kind, " " * 9 + line) for line in rest]
            else:
                out.append((kind, item))
        return out


def turn_lines(turn: Turn, expanded: bool) -> list[tuple[str, str]]:
    """A turn's thinking, folded to its first sentence unless ctrl+o opened it, then a line
    for each step, as in the proposal's mock-up."""
    thought = "\n".join(text for text in turn.thoughts if text.strip())
    if thought and expanded:
        out = [("think", "▾ Thinking"), *(("think", "  " + line) for line in thought.splitlines())]
    else:
        out = [("think", f"▸ Thinking · {summary(thought)}   ctrl+o")] if thought else []
    for label, seconds in turn.steps:
        done = f"  ✓ {seconds:.1f}s" if seconds is not None else ""
        out.append(("step", f"● {label}{done}"))
    return out


def start(argv: list[str], popen: Callable[..., Any] = subprocess.Popen) -> Any:
    """Start Attaché in its own process group (GROUP), with stdout and stderr on one pipe."""
    pipes = {"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE, "stderr": subprocess.STDOUT}
    text = {"text": True, "encoding": "utf-8", "errors": "replace", "bufsize": 1}
    return popen(argv, **pipes, **text, **GROUP)


def stop(proc: Any, windows: bool = os.name == "nt", run: Callable[..., Any] = subprocess.run):
    """Stop the child as Ctrl+C would, wait 10 seconds, then kill its whole tree. (Popen's
    send_signal does nothing once the process has exited.)"""
    proc.send_signal(signal.CTRL_BREAK_EVENT if windows else signal.SIGINT)
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        if windows:
            run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
        else:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)  # its session: start_new_session
        proc.wait()


def make_app(proc: Any, base: str, token: str, seen: MutableSequence[str]) -> Any:
    """The Textual app, talking to the Attaché at `base` with the UI token. Quitting is
    Textual's own action_quit; main() then stops the child."""
    import httpx
    from rich.text import Text
    from textual import events
    from textual.app import App
    from textual.binding import Binding
    from textual.containers import VerticalScroll
    from textual.widgets import Input, RichLog, Static

    styles = {"bot": "", "you": "bold", "think": "dim italic", "ask": "yellow", "error": "red"}

    class Prompt(Input):
        """The input box. It takes printable keys before any App binding sees them, so the
        number keys that answer a question are caught here."""

        async def on_key(self, event: events.Key) -> None:
            if not self.value and (picked := self.app.state.choice(event.character or "")):
                event.prevent_default()  # so Input's own handler does not type it
                await self.app.answer(*picked)

    class Tui(App):
        CSS = """
        #chat { height: 1fr; padding: 0 1; }
        #log { height: 12; display: none; }
        """
        BINDINGS = [  # noqa: RUF012 - Textual reads it from the class; ctrl+q quits already
            Binding("ctrl+o", "steps", "steps"),
            Binding("escape", "stop", "stop", priority=True),
            Binding("ctrl+l", "log", "log"),
            Binding("ctrl+d", "quit", "quit", priority=True),  # Input's delete-right otherwise
        ]

        def __init__(self) -> None:
            super().__init__()
            self.state = State()
            self.goodbye, self.code = "", 0  # set if Attaché stops by itself
            headers = {"X-Attache-Token": token}  # trust_env off: never through a proxy
            self.client = httpx.AsyncClient(base_url=base, headers=headers, trust_env=False)

        def compose(self) -> Any:
            yield Static(id="head")
            with VerticalScroll(id="chat"):
                yield Static(id="transcript")
            yield RichLog(id="log", max_lines=2000)
            yield Static(id="status")
            yield Prompt(placeholder="Message Attaché", id="input")
            yield Static("ctrl+o steps · esc stop · ctrl+l log · ctrl+d quit", id="hints")

        def on_mount(self) -> None:
            log = self.query_one(RichLog)
            log.write("\n".join(seen))  # what Attaché printed before its link
            threading.Thread(target=self.pump, args=[log], daemon=True).start()
            self.run_worker(self.listen())  # which draws the chat once it has loaded
            self.set_interval(0.1, self.tick)

        def pump(self, log: RichLog) -> None:
            """A thread: the child's output into the log, then its exit. It reads to the end
            even once the app has closed, so the pipe never fills while Attaché stops."""
            for line in proc.stdout:
                seen.append(clean(line.rstrip("\n")))  # a deque: safe from this thread
                self.later(log.write, seen[-1])
            self.later(self.gone, proc.wait())

        def later(self, *call: Any) -> None:
            """call_from_thread while the app runs; nothing once it has ended."""
            with contextlib.suppress(RuntimeError):  # it ended just now
                if self.is_running:  # else the call would be left waiting, never run
                    self.call_from_thread(*call)

        def gone(self, code: int) -> None:
            """Attaché stopped by itself: say so, with its last lines, once the app is closed."""
            last = [line for line in seen if line.strip()][-5:]
            self.goodbye = "\n".join([f"Attaché stopped (exit code {code}).", *last])
            self.code = 1
            self.exit()

        async def listen(self) -> None:
            """The event stream, then the chat so far, so nothing is missed in between."""
            stream = self.client.stream("GET", "/api/events", params={"token": token}, timeout=None)
            try:
                async with stream as response:
                    response.raise_for_status()
                    chat = await self.client.get("/api/messages")
                    self.state.load(chat.raise_for_status().json())
                    self.show()
                    kind = ""
                    async for line in response.aiter_lines():
                        kind, data = sse(line, kind)
                        if kind == "bye":
                            return
                        if data is not None:
                            self.handle(kind, data)
            except httpx.HTTPError as error:
                self.state.entries.append(("error", f"lost Attaché's events: {error}"))
                self.show()

        def handle(self, kind: str, data: dict[str, Any]) -> None:
            if kind == "message":
                self.state.message(data)
            elif kind == "ask":
                self.state.ask(data)
            elif kind == "activity":
                self.state.activity(data, time.monotonic())
            self.show()

        def show(self) -> None:
            """Draw everything again. Every string from Attaché goes through clean()."""
            text = Text()
            for style, line in self.state.lines():
                text.append(clean(line) + "\n", style=styles.get(style, "dim"))  # steps, footers
            self.query_one("#transcript", Static).update(text)
            self.query_one("#chat").scroll_end(animate=False)
            route = f" · {self.state.route}" if self.state.route else ""
            self.query_one("#head", Static).update(Text(clean(f"Attaché · local:me{route}")))
            self.tick()

        def tick(self) -> None:
            status = self.state.status(time.monotonic())
            self.query_one("#status", Static).update(Text(clean(status)))

        async def post(self, path: str, data: dict[str, Any]) -> dict[str, Any] | None:
            """POST to Attaché: its JSON reply, or None after showing the error."""
            try:
                reply = await self.client.post(path, json=data)
                return reply.raise_for_status().json()
            except httpx.HTTPError as error:
                self.state.entries.append(("error", f"{path}: {error}"))
                self.show()

        async def on_input_submitted(self, event: Input.Submitted) -> None:
            """Send a message. Its turn shows from the "started" event, at once."""
            text, event.input.value = event.value.strip(), ""
            if text:
                self.state.entries.append(("you", text))
                self.show()
                await self.post("/api/chat", {"text": text})

        async def answer(self, ask: dict[str, Any], choice: str) -> None:
            reply = await self.post("/api/answer", {"ask_id": ask["ask_id"], "choice": choice})
            if reply is not None:
                self.state.answered(ask["ask_id"], choice if reply.get("ok") else "(closed)")
                self.show()

        async def action_stop(self) -> None:
            await self.post("/api/stop", {})

        def action_steps(self) -> None:
            self.state.expanded = not self.state.expanded
            self.show()

        def action_log(self) -> None:
            log = self.query_one(RichLog)
            log.display = not log.display

    return Tui()


def main() -> int:
    print("Starting Attaché…", flush=True)
    proc = start(child_argv(os.environ))
    seen: deque[str] = deque(maxlen=200)  # the child's output, for the log pane
    try:
        link = read_link(proc.stdout, seen)
        if link is None:
            print(early_exit(seen, proc.wait()), file=sys.stderr)
            return 1
        app = make_app(proc, *link, seen)
        app.run()
    except KeyboardInterrupt:  # while it was starting
        return 130
    finally:
        if proc.poll() is None:
            print("Stopping Attaché…", flush=True)
            stop(proc)
    if app.goodbye:  # it stopped by itself: say why
        print(app.goodbye)
    return app.code


if __name__ == "__main__":
    sys.exit(main())
