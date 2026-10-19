"""Wire the services together and run them. Owner: runtime."""

from __future__ import annotations

import asyncio
import secrets
import socket
from dataclasses import dataclass
from pathlib import Path

from attache import channels, config, router, scheduler, store, web


@dataclass
class Services:
    config: config.Config
    store: store.Store
    scheduler: scheduler.Scheduler
    channels: channels.ChannelManager
    gateway: web.ToolGateway  # lets CLI backends call our tools over HTTP (see mcp.py)
    web_url: str  # e.g. http://127.0.0.1:8765
    secret: bytes  # HMAC key for artifact tokens and signed links, from <home>/.secret

    @property
    def home(self) -> Path:
        return self.config.home


def build(home: Path) -> Services:
    """Load config and open the store. No network, no tasks started."""
    (key := home / ".secret").exists() or key.write_text(secrets.token_hex(32), encoding="utf-8")
    cfg, db, hub = config.load(home), store.Store(home / "attache.db"), channels.ChannelManager()
    url, secret = f"http://{cfg.host}:{cfg.port}", key.read_bytes().strip()
    return Services(cfg, db, scheduler.Scheduler(db), hub, web.ToolGateway(), url, secret)


async def run(home: Path, *, terminal: bool) -> None:
    """Run web, scheduler and channels (and the REPL) until the REPL ends or Ctrl+C."""
    cfg = config.load(home)
    try:  # a quick bind; create_server sets SO_REUSEADDR on POSIX only, as uvicorn does
        socket.create_server((cfg.host, cfg.port)).close()
    except OSError:
        raise SystemExit(f"Port {cfg.port} is in use. Is `attache run` already running?") from None
    rt, page = router.Router(sv := build(home)), web.WebChannel()
    term = channels.TerminalChannel(cfg.owner.name, sv.web_url)
    sv.scheduler.fire = rt.fire  # before scheduler.run() starts, so no wake() is needed
    for channel in [page, term] if terminal else [page]:
        sv.channels.add(channel)
    coros = [web.serve(web.create_app(sv, rt, page), cfg.host, cfg.port), sv.scheduler.run()]
    tasks = [*map(asyncio.create_task, coros), *await sv.channels.start(rt.handle)]
    try:
        await (term.done.wait() if terminal else asyncio.Event().wait())
    finally:
        asyncio.gather(*tasks).cancel()
        sv.store.close()
