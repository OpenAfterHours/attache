from __future__ import annotations

import asyncio
import io
import logging
import os
import shutil
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
    """app.run builds the web app as create_app(services, page); these tests fake the server."""
    monkeypatch.setattr(web, "create_app", lambda services, page: None)
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


def test_listen_refuses_other_hosts(home: Path) -> None:
    for host in ("0.0.0.0", "192.168.1.5", "example.com"):
        with pytest.raises(SystemExit, match=r"use 127\.0\.0\.1, localhost or ::1"):
            app.listen(config.Config(home, config.load(home).owner, {}, {}, {}, host=host))


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


def free_home(home: Path, extra: str = "") -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    (home / "attache.toml").write_text(f"[web]\nport = {port}\n{extra}", encoding="utf-8")


async def serving(app_: Any, sock: socket.socket) -> None:
    await asyncio.Event().wait()


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
