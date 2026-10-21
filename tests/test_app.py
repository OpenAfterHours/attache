from __future__ import annotations

import asyncio
import io
import logging
import os
import shutil
import signal
import socket
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from attache import app, config, web
from attache.channels import ChannelManager
from attache.scheduler import Scheduler


@pytest.fixture(autouse=True)
def fake_web_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """app.run builds the web apps as create_app(services, page, router) and, for share links,
    create_app(services, page, public=True); these tests fake them as the names "local" (with
    a router: page turns need one) and "public", and fake the servers."""

    def create_app(services: Any, page: Any, router: Any = None, public: bool = False) -> str:
        assert public is (router is None)  # only the local app takes page turns
        return "public" if public else "local"

    monkeypatch.setattr(web, "create_app", create_app)
    monkeypatch.delenv("ATTACHE_WEB_URL", raising=False)  # restored after each test


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_build_on_an_initialised_home(home: Path) -> None:
    config.init_home(home, "Alex")
    services, again = app.build(home), app.build(home)
    try:
        assert services.home == home and services.config.owner.name == "Alex"
        assert services.web_url == "http://127.0.0.1:8765" and services.gateway == {}
        assert len(services.ui_token) >= 32 and services.ui_token != again.ui_token  # per run
        assert not hasattr(services, "secret") and not (home / ".secret").exists()
        assert isinstance(services.channels, ChannelManager)  # it asks the questions too
        assert not hasattr(services, "approvals")
        services.store.add_message("local:me", "user", "hi")
    finally:
        services.store.close()
        again.store.close()


def test_ipv6_loopback_url(home: Path) -> None:
    (home / "attache.toml").write_text('[web]\nhost = "::1"\nport = 9001\n', encoding="utf-8")
    services = app.build(home)
    services.store.close()
    assert services.web_url == "http://[::1]:9001"


def test_listen_refuses_other_hosts() -> None:
    for host in ("0.0.0.0", "192.168.1.5", "example.com"):
        with pytest.raises(SystemExit, match=r"use 127\.0\.0\.1, localhost or ::1"):
            app.listen(host, 8765)


def test_run_reports_a_taken_port(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def never(*args: Any) -> None:
        raise AssertionError("must not start the server")

    monkeypatch.setattr(web, "serve", never)
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        (home / "attache.toml").write_text(f"[web]\nport = {port}\n", encoding="utf-8")
        with pytest.raises(SystemExit) as exit:
            asyncio.run(app.run(home, terminal=False))
    assert str(port) in str(exit.value) and "attache run" in str(exit.value)
    assert not (home / "attache.db").exists()  # checked before anything was opened


def free_ports(count: int) -> list[int]:
    """Ports free on 127.0.0.1 now: all bound at once, so no two are the same."""
    sockets = [socket.socket() for _ in range(count)]
    for s in sockets:
        s.bind(("127.0.0.1", 0))
    ports = [s.getsockname()[1] for s in sockets]
    for s in sockets:
        s.close()
    return ports


def free_home(home: Path, extra: str = "") -> None:
    (home / "attache.toml").write_text(f"[web]\nport = {free_ports(1)[0]}\n{extra}", "utf-8")


async def serving(app_: Any, sock: socket.socket) -> None:
    await asyncio.Event().wait()


SHARING = '[web]\nport = {}\npublic_url = "https://pc.tailnet.ts.net"\npublic_port = {}\n'


def test_run_reports_a_taken_public_port(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web, "serve", serving)
    port, public = free_ports(2)
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", public))
        busy.listen()
        (home / "attache.toml").write_text(SHARING.format(port, public), encoding="utf-8")
        with pytest.raises(SystemExit) as exit:
            asyncio.run(app.run(home, terminal=False))
    assert f"Port {port} or {public} is in use" in str(exit.value)
    assert not (home / "attache.db").exists()


@pytest.mark.parametrize("sharing", [True, False])
def test_run_serves_share_links_on_a_second_loopback_port(
    sharing: bool, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    served: list[tuple[str, Any]] = []
    resumed: list[Any] = []

    async def serve(app_: str, sock: socket.socket) -> None:
        served.append((app_, sock.getsockname()))
        await asyncio.Event().wait()

    async def resume_requests(services: app.Services) -> None:  # the requests still open
        resumed.append((services, os.environ["ATTACHE_HOME"]))

    monkeypatch.setattr(web, "serve", serve)
    monkeypatch.setattr(app.tools, "resume_requests", resume_requests)
    monkeypatch.setattr(sys, "stdin", io.StringIO("/quit\n"))
    port, public = free_ports(2)
    text = SHARING.format(port, public) if sharing else f"[web]\nport = {port}\n"
    (home / "attache.toml").write_text(text, encoding="utf-8")
    asyncio.run(asyncio.wait_for(app.run(home, terminal=True), 10))
    local = ("local", ("127.0.0.1", port))
    assert served == ([local, ("public", ("127.0.0.1", public))] if sharing else [local])
    ((services, env_home),) = resumed  # once, after the channels started
    assert isinstance(services, app.Services) and env_home == str(home.resolve())  # for scripts
    assert not (home / "skills").exists()  # bundled skills arrive by `attache init` only


@pytest.mark.parametrize("dies", ["web", "scheduler"])
def test_run_stops_when_the_web_server_or_scheduler_dies(
    dies: str, home: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def broken(*args: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(web, "serve", broken if dies == "web" else serving)
    if dies == "scheduler":
        monkeypatch.setattr(Scheduler, "run", broken)
    free_home(home)
    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit, match="scheduler stopped"):
        asyncio.run(app.run(home, terminal=False))
    assert "boom" in caplog.text  # the failure is logged
    assert "_GatheringFuture" not in caplog.text


def test_run_wires_sidecars_and_outlives_a_channel(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = '[owner]\nname = "Alex"\n[owner.ids]\nslack = "U0ALEX"\n'
    endpoints = '[endpoints.api]\napi = "anthropic"\nmodel = "m"\n'
    free_home(home, owner + endpoints)
    events: list[Any] = []

    class Sidecar:
        name = "slack"

        async def start(self, on_message: Any) -> None:
            events.append("sidecar ended")  # at once, as a crashed sidecar would

        async def send(self, msg: Any) -> None: ...

        async def stop(self) -> None:
            events.append("sidecar stopped")

    def sidecars(cfg: config.Config, on_answer: Any) -> list[Sidecar]:
        events.append(on_answer)
        return [Sidecar()]

    answers: list[tuple[str, str, str]] = []
    monkeypatch.setattr(ChannelManager, "answer", lambda self, *args: answers.append(args) or True)
    monkeypatch.setattr(app.importlib, "import_module", lambda name: events.append(name))
    monkeypatch.setattr(app.channels, "sidecars", sidecars)
    monkeypatch.setattr(web, "serve", serving)
    go = threading.Event()

    class Stdin:  # the terminal chat quits only when told
        def readline(self) -> str:
            go.wait()
            return "/quit\n"

    monkeypatch.setattr(sys, "stdin", Stdin())

    async def main() -> None:
        task = asyncio.create_task(app.run(home, terminal=True))
        await asyncio.sleep(0.5)
        assert "sidecar ended" in events and not task.done()  # Attaché runs on
        on_answer = events[0]
        assert on_answer("slack", "a1", "U0ALEX", "Approve")
        assert not on_answer("slack", "a2", "U0WHO", "Decline")  # nobody we know
        assert os.environ["ATTACHE_WEB_URL"].startswith("http://127.0.0.1:")  # for sidecars
        go.set()
        await asyncio.wait_for(task, 10)

    asyncio.run(main())
    assert answers == [("a1", "owner", "Approve")]  # a person id; the stranger never got there
    assert "anthropic" in events and "sidecar stopped" in events  # warmed, then stopped


def slow_router(seconds: float, replies: list[str]) -> type:
    class Router:  # turns that take a while, as a model's do
        def __init__(self, services: app.Services) -> None:
            self.background: dict[int, Any] = {}
            services.scheduler.fire = self.fire

        async def fire(self, task: Any) -> None: ...

        async def handle(self, msg: Any) -> None:
            await asyncio.sleep(seconds)
            replies.append(msg.text)

    return Router


async def until(condition: Any, timeout: float = 10.0) -> None:
    end = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < end, "timed out"
        await asyncio.sleep(0.01)


def test_ctrl_c_in_chat_stops_a_running_turn_then_quits(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    replies: list[str] = []
    built: list[app.Services] = []
    real_build = app.build

    class Router:  # a long turn that, as the real router does, reports how it ended
        def __init__(self, services: app.Services) -> None:
            self.services = services
            services.scheduler.fire = self.fire

        async def fire(self, task: Any) -> None: ...

        async def handle(self, msg: Any) -> None:
            try:
                await asyncio.sleep(60)
                replies.append(msg.text)
            finally:
                done = {"seconds": 0.5, "tokens": None, "route": "claude"}
                stopped = msg.text not in replies
                await self.services.channels.activity(
                    msg.chat_key, "finished", **done, stopped=stopped
                )

    monkeypatch.setattr(app, "build", lambda where: built.append(real_build(where)) or built[0])
    monkeypatch.setattr(app.router, "Router", Router)
    monkeypatch.setattr(web, "serve", serving)

    class Stdin:  # one message, then someone who never types again (a daemon thread waits)
        def __init__(self) -> None:
            self.lines = ["hello\n"]

        def readline(self) -> str:
            if self.lines:
                return self.lines.pop()
            threading.Event().wait()
            return ""

    monkeypatch.setattr(sys, "stdin", Stdin())
    free_home(home)

    def turns() -> list[Any]:  # what is running for the terminal's chat
        return [t for t in built[0].channels.tasks if t.get_name() == "local:me"] if built else []

    printed: list[str] = []

    def footer_shown() -> bool:
        printed.append(capsys.readouterr().out)
        return "  stopped · 0.5s\n" in "".join(printed)

    async def main() -> None:
        theirs = signal.getsignal(signal.SIGINT)  # asyncio.run's own handler
        run = asyncio.create_task(app.run(home, terminal=True))
        await until(turns)  # "hello" is being answered
        ctrl_c = signal.getsignal(signal.SIGINT)
        assert ctrl_c is not theirs
        ctrl_c(signal.SIGINT, None)  # as Python calls it for a real Ctrl+C, on any system
        await until(footer_shown)  # the stopped turn's footer, in the terminal
        await until(lambda: not turns())
        assert not run.done()  # Attaché runs on
        ctrl_c(signal.SIGINT, None)  # nothing running: quit, through asyncio.run's handler
        await run

    with pytest.raises(KeyboardInterrupt):  # as for Ctrl+C before; cli.main hides it
        asyncio.run(main())
    assert replies == []  # the stopped turn never replied
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler  # put back


def test_ctrl_c_in_attache_run_is_left_as_it_was(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Any] = []

    async def serve(app_: Any, sock: socket.socket) -> None:
        seen.append(signal.getsignal(signal.SIGINT))
        raise RuntimeError("done looking")

    async def main() -> None:
        seen.append(signal.getsignal(signal.SIGINT))
        await app.run(home, terminal=False)

    monkeypatch.setattr(web, "serve", serve)
    free_home(home)
    with pytest.raises(SystemExit):
        asyncio.run(main())
    assert seen[0] is seen[1]  # still asyncio.run's own


class FakeSignal:
    """The signal module as app.run sees it, on Windows (with SIGBREAK) or not. Nothing real:
    it only records the handlers."""

    SIGINT = 2
    default_int_handler = staticmethod(signal.default_int_handler)  # it raises KeyboardInterrupt

    def __init__(self, windows: bool) -> None:
        self.handlers: dict[int, Any] = {self.SIGINT: "asyncio.run's"}
        if windows:
            self.SIGBREAK = 21
            self.handlers[21] = "Windows' own"

    def getsignal(self, number: int) -> Any:
        return self.handlers.get(number)

    def signal(self, number: int, handler: Any) -> Any:
        self.handlers[number], previous = handler, self.handlers.get(number)
        return previous


class Waiting:  # stdin for a terminal that waits for good
    def readline(self) -> str:
        threading.Event().wait()
        return ""


def test_ctrl_c_quits_even_if_it_was_ignored_at_start(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeSignal(windows=False)
    fake.handlers[2] = signal.SIG_IGN  # as for a job started in the background

    async def serve(app_: Any, sock: socket.socket) -> None:
        fake.handlers[2](2, None)  # Ctrl+C in `attache chat`, with no turn running
        await asyncio.Event().wait()

    monkeypatch.setattr(app, "signal", fake)
    monkeypatch.setattr(web, "serve", serve)
    monkeypatch.setattr(sys, "stdin", Waiting())
    free_home(home)
    with pytest.raises(KeyboardInterrupt):  # not a TypeError from calling SIG_IGN
        asyncio.run(app.run(home, terminal=True))
    assert fake.handlers[2] is signal.SIG_IGN  # put back


@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize("windows", [True, False])
def test_ctrl_break_quits_quietly_as_ctrl_c_does(
    windows: bool, terminal: bool, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake, during = FakeSignal(windows), []

    async def serve(app_: Any, sock: socket.socket) -> None:
        during.append(dict(fake.handlers))
        raise RuntimeError("done looking")

    monkeypatch.setattr(app, "signal", fake)
    monkeypatch.setattr(web, "serve", serve)
    monkeypatch.setattr(sys, "stdin", Waiting())  # the terminal, if any: the server ends the run
    free_home(home)
    with pytest.raises(SystemExit):
        asyncio.run(app.run(home, terminal=terminal))
    ((handlers,),) = [during]
    # Ctrl+Break (how the tui skill stops `attache run` on Windows) gets asyncio.run's own
    # SIGINT handler, so it quits as quietly as Ctrl+C, in either mode.
    assert handlers.get(21) == ("asyncio.run's" if windows else None)
    assert (handlers[2] == "asyncio.run's") is not terminal  # chat: Ctrl+C stops turns first
    assert getattr(handlers[2], "__name__", None) == ("on_ctrl_c" if terminal else None)
    # Afterwards, Ctrl+C and Ctrl+Break are as they were.
    assert fake.handlers == (
        {2: "asyncio.run's", 21: "Windows' own"} if windows else {2: "asyncio.run's"}
    )


def test_piped_chat_waits_for_its_replies(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    replies: list[str] = []
    monkeypatch.setattr(app.router, "Router", slow_router(0.3, replies))
    monkeypatch.setattr(web, "serve", serving)
    monkeypatch.setattr(sys, "stdin", io.StringIO("hello\nhow are you?\n"))  # then EOF at once
    free_home(home)
    asyncio.run(asyncio.wait_for(app.run(home, terminal=True), 10))
    assert replies == ["hello", "how are you?"]  # not cancelled by the terminal quitting


def test_piped_chat_waits_no_longer_than_the_slowest_endpoint(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    replies: list[str] = []
    monkeypatch.setattr(app.router, "Router", slow_router(60, replies))
    monkeypatch.setattr(web, "serve", serving)
    monkeypatch.setattr(app.importlib, "import_module", lambda name: None)  # no warm-up
    monkeypatch.setattr(sys, "stdin", io.StringIO("hello\n"))
    endpoints = '[endpoints.quick]\napi = "anthropic"\nmodel = "m"\ntimeout = 0.2\n'
    free_home(home, endpoints + '[routing]\nown_chats = "quick"\n')
    began = time.monotonic()
    asyncio.run(asyncio.wait_for(app.run(home, terminal=True), 10))
    assert time.monotonic() - began < 5 and replies == []  # given up after 0.2 s


def test_run_shuts_down_in_order(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    stores: list[Any] = []
    real_build, real_stop = app.build, app.channels.ChannelManager.stop

    async def serving(app_: Any, sock: socket.socket) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            events.append("web cancelled")

    async def ticking(self: Any) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            events.append("scheduler cancelled")

    def build(where: Path) -> app.Services:
        stores.append((services := real_build(where)).store)
        return services

    async def stop(self: Any) -> None:
        events.append("channels stopped")
        stores[0].add_message("local:me", "user", "the store is still open")
        await real_stop(self)

    monkeypatch.setattr(web, "serve", serving)
    monkeypatch.setattr(Scheduler, "run", ticking)
    monkeypatch.setattr(app, "build", build)
    monkeypatch.setattr(app.channels.ChannelManager, "stop", stop)
    monkeypatch.setattr(sys, "stdin", io.StringIO("/quit\n"))
    free_home(home)
    asyncio.run(asyncio.wait_for(app.run(home, terminal=True), 10))
    # The channels first (that ends the page's event streams), then the web server and the rest.
    assert events[0] == "channels stopped"
    assert sorted(events[1:]) == ["scheduler cancelled", "web cancelled"]
    with pytest.raises(sqlite3.ProgrammingError):  # closed last
        stores[0].add_message("local:me", "user", "too late")
