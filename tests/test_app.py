from __future__ import annotations

import asyncio
import shutil
import socket
from pathlib import Path
from typing import Any

import pytest

from attache import app, config, web


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_build_on_an_initialised_home(home: Path) -> None:
    config.init_home(home, "Alex")
    services = app.build(home)
    try:
        assert services.home == home and services.config.owner.name == "Alex"
        assert services.web_url == "http://127.0.0.1:8765"
        assert (
            services.secret == (home / ".secret").read_bytes().strip()
            and len(services.secret) == 64
        )
        assert services.scheduler.store is services.store and services.channels.channels == {}
        assert isinstance(services.gateway, web.ToolGateway)
        services.store.add_message("terminal:local", "user", "hi")
    finally:
        services.store.close()
    (home / ".secret").unlink()
    again = app.build(home)  # a missing .secret is created
    again.store.close()
    assert len((home / ".secret").read_bytes().strip()) == 64


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
