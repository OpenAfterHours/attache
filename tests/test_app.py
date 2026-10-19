from __future__ import annotations

import asyncio
import io
import logging
import shutil
import socket
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

from attache import app, config, web


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_build_on_an_initialised_home(home: Path) -> None:
    config.init_home(home, "Alex")
    services, again = app.build(home), app.build(home)
    try:
        assert services.home == home and services.config.owner.name == "Alex"
        assert services.web_url == "http://127.0.0.1:8765" and services.gateway == {}
        assert len(services.ui_token) >= 32 and services.ui_token != again.ui_token  # per run
        assert not hasattr(services, "secret") and not (home / ".secret").exists()
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


def free_home(home: Path) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    (home / "attache.toml").write_text(f"[web]\nport = {port}\n", encoding="utf-8")


def test_run_stops_when_the_web_server_dies(
    home: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def broken(app: Any, sock: socket.socket) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(web, "serve", broken)
    free_home(home)
    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit, match="web server stopped"):
        asyncio.run(app.run(home, terminal=False))
    assert "boom" in caplog.text  # the failure is logged
    assert "_GatheringFuture" not in caplog.text


def test_run_shuts_down_in_order(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    stores: list[Any] = []
    real_build, real_stop = app.build, app.channels.ChannelManager.stop

    async def serving(app_: Any, sock: socket.socket) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            events.append("web cancelled")

    def build(where: Path) -> app.Services:
        stores.append((services := real_build(where)).store)
        return services

    async def stop(self: Any) -> None:
        events.append("channels stopped")
        stores[0].add_message("local:me", "user", "the store is still open")
        await real_stop(self)

    monkeypatch.setattr(web, "serve", serving)
    monkeypatch.setattr(app, "build", build)
    monkeypatch.setattr(app.channels.ChannelManager, "stop", stop)
    monkeypatch.setattr(sys, "stdin", io.StringIO("/quit\n"))
    free_home(home)
    asyncio.run(asyncio.wait_for(app.run(home, terminal=True), 10))
    assert events == ["web cancelled", "channels stopped"]
    with pytest.raises(sqlite3.ProgrammingError):  # closed last
        stores[0].add_message("local:me", "user", "too late")
