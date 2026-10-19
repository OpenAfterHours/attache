"""Wire the services together and run them."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from socket import AF_INET, AF_INET6, create_server, socket

from attache import channels, config, router, scheduler, store, web
from attache.types import Turn


@dataclass
class Services:
    config: config.Config
    store: store.Store
    scheduler: scheduler.Scheduler
    channels: channels.ChannelManager
    gateway: dict[str, Turn]  # MCP token -> the turn it serves (the claude-cli backend's tools)
    web_url: str  # e.g. http://127.0.0.1:8765
    secret: bytes  # HMAC key for artifact tokens and signed links, from <home>/.secret
    ui_token: str = field(default_factory=lambda: secrets.token_urlsafe(24))  # the web page's

    home = property(lambda self: self.config.home)  # <home>, the folder all state lives in


def listen(cfg: config.Config) -> socket:
    """Bind [web] host:port. Phase 1 serves this machine only: another host is a SystemExit.
    A port in use raises OSError."""
    if cfg.host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit(f"[web] host = {cfg.host!r}: use 127.0.0.1, localhost or ::1 for now")
    return create_server((cfg.host, cfg.port), family=AF_INET6 if cfg.host == "::1" else AF_INET)


def build(home: Path) -> Services:
    """Load config and open the store, creating <home>/.secret, readable by this user only,
    if it is missing. No network, no tasks started."""
    key = home / ".secret"
    if not key.exists():
        key.write_text(secrets.token_hex(32), encoding="utf-8")
        key.chmod(0o600)  # enough on POSIX; on Windows, also drop what the folder grants
        if os.name == "nt":
            acl = [str(key), "/inheritance:r", "/grant:r", f"{os.environ.get('USERNAME')}:F"]
            subprocess.run(["icacls", *acl], capture_output=True)
    cfg, db, secret = config.load(home), store.Store(home / "attache.db"), key.read_bytes().strip()
    url = f"http://{'[::1]' if cfg.host == '::1' else cfg.host}:{cfg.port}"
    return Services(cfg, db, scheduler.Scheduler(db), channels.ChannelManager(), {}, url, secret)


async def run(home: Path, *, terminal: bool) -> None:
    """Start the web server, scheduler and channels (plus the terminal chat when asked).

    Returns when any task ends: the terminal chat's, at EOF or /quit, is the normal one; the
    others only stop by failing. Ctrl+C also returns. A port in use or a host that is not this
    machine is a SystemExit saying so; so is the web server stopping. On the way out every
    task is cancelled and awaited (any that failed is logged), the channels are stopped, and
    only then is the store closed. fire is set before the scheduler starts: no wake() needed."""
    try:
        sock = listen(cfg := config.load(home))
    except OSError:
        raise SystemExit(f"Port {cfg.port} is in use. Is `attache run` already running?") from None
    services = build(home)
    rt, page = router.Router(services), web.WebChannel()  # rt sets itself as scheduler.fire
    url = f"{services.web_url}/#t={services.ui_token}"
    print(f"Open {url}", flush=True)
    services.channels.add(page)
    if terminal:
        services.channels.add(channels.TerminalChannel(cfg.owner.name, url))
    server = asyncio.create_task(web.serve(web.create_app(services, rt, page), sock))
    tasks = [server, asyncio.create_task(services.scheduler.run())]
    tasks += await services.channels.start(rt.handle)
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if server in done:
            raise SystemExit("The web server stopped.")
    finally:
        everything = [*tasks, *rt.background]  # background: scheduled turns still running
        for task in everything:
            task.cancel()
        for result in await asyncio.gather(*everything, return_exceptions=True):
            if isinstance(result, Exception):
                logging.getLogger(__name__).error("a task failed", exc_info=result)
        await services.channels.stop()
        services.store.close()
