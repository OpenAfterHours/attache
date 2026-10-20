"""Wire the services together and run them."""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from socket import AF_INET, AF_INET6, create_server, socket
from typing import Any

from attache import channels, config, router, scheduler, store, web
from attache.types import Turn

SDKS = {"anthropic": "anthropic", "openai-chat": "openai", "openai-responses": "openai"}


@dataclass
class Services:
    config: config.Config
    store: store.Store
    scheduler: scheduler.Scheduler
    channels: channels.ChannelManager  # also asks questions and takes their answers
    gateway: dict[str, Turn]  # MCP token -> the turn it serves (the claude-cli backend's tools)
    web_url: str  # e.g. http://127.0.0.1:8765
    ui_token: str = field(default_factory=lambda: secrets.token_urlsafe(24))  # new every run

    @property
    def home(self) -> Path:
        return self.config.home


def listen(cfg: config.Config) -> socket:
    """Bind [web] host:port. Phase 1 serves this machine only: another host is a SystemExit.
    A port in use raises OSError."""
    if cfg.host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit(f"[web] host = {cfg.host!r}: use 127.0.0.1, localhost or ::1 for now")
    return create_server((cfg.host, cfg.port), family=AF_INET6 if cfg.host == "::1" else AF_INET)


def build(home: Path) -> Services:
    """Load config and open the store. No network, no tasks started."""
    cfg = config.load(home)
    db, hub = store.Store(home / "attache.db"), channels.ChannelManager()
    url = f"http://{'[::1]' if cfg.host == '::1' else cfg.host}:{cfg.port}"
    return Services(cfg, db, scheduler.Scheduler(db), hub, {}, url)


async def _cancel(tasks: list[asyncio.Task[Any]]) -> None:
    """Cancel tasks and wait for them to finish; one that had failed is logged."""
    for task in tasks:
        task.cancel()
    for result in await asyncio.gather(*tasks, return_exceptions=True):
        if isinstance(result, Exception):
            logging.getLogger(__name__).error("a task failed", exc_info=result)


async def run(home: Path, *, terminal: bool) -> None:
    """Start the web server, scheduler and channels (plus the terminal chat when asked).

    Returns when the terminal session ends (EOF or /quit) or on Ctrl+C. A channel that stops,
    a sidecar included, never ends the run; the web server or the scheduler stopping does, as
    a SystemExit. A port in use or a host that is not this machine is a SystemExit too."""
    cfg = config.load(home)
    try:
        sock = listen(cfg)
    except OSError:
        raise SystemExit(f"Port {cfg.port} is in use. Is `attache run` already running?") from None
    services = build(home)
    os.environ["ATTACHE_WEB_URL"] = services.web_url  # for skill scripts and sidecars
    rt, page = router.Router(services), web.WebChannel()  # rt sets itself as scheduler.fire
    url = f"{services.web_url}/#t={services.ui_token}"
    print(f"Open {url}", flush=True)

    def on_answer(channel: str, ask_id: str, sender_id: str, choice: str) -> bool:
        """A sidecar's answer names a platform sender; the question waits for a person id."""
        person = cfg.person(channel, sender_id)
        return person is not None and services.channels.answer(ask_id, person.id, choice)

    term = channels.TerminalChannel(cfg.owner.name, url)
    for channel in [page, *([term] if terminal else []), *channels.sidecars(cfg, on_answer)]:
        services.channels.add(channel)
    server = asyncio.create_task(web.serve(web.create_app(services, page), sock))
    beat = asyncio.create_task(services.scheduler.run())
    sdks = {SDKS[e.api] for e in cfg.endpoints.values() if e.api in SDKS}  # so turn 1 won't wait
    warmups = [asyncio.create_task(asyncio.to_thread(importlib.import_module, m)) for m in sdks]
    rest = [beat, *warmups, *await services.channels.start(rt.handle)]
    quitting = [asyncio.create_task(term.done.wait())] if terminal else []
    try:
        done, _ = await asyncio.wait([server, beat, *quitting], return_when=asyncio.FIRST_COMPLETED)
        if server in done or beat in done:
            raise SystemExit("The web server or the scheduler stopped; see the log above.")
    finally:  # in this order:
        await services.channels.stop()  # the turns still running, then every channel, which
        # ends the page's event streams, so the web server's own shutdown is quick and quiet
        await _cancel([server, *rest, *quitting, *rt.background.values()])
        services.store.close()  # last, once nothing can write to it
