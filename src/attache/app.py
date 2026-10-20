"""Wire the services together and run them."""

from __future__ import annotations

import asyncio
import importlib
import logging
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from socket import AF_INET, AF_INET6, create_server, socket

from attache import channels, config, router, scheduler, store, web
from attache.approvals import Approvals
from attache.types import OnAnswer, Turn

SDKS = {"anthropic": "anthropic", "openai-chat": "openai", "openai-responses": "openai"}


@dataclass
class Services:
    config: config.Config
    store: store.Store
    scheduler: scheduler.Scheduler
    channels: channels.ChannelManager
    approvals: Approvals
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
    return Services(cfg, db, scheduler.Scheduler(db), hub, Approvals(hub), {}, url)


async def run(home: Path, *, terminal: bool) -> None:
    """Start the web server, scheduler and channels (plus the terminal chat when asked).

    Returns when the terminal session ends (EOF or /quit) or on Ctrl+C. A channel that stops,
    a sidecar included, never ends the run; the web server or the scheduler stopping does, as
    a SystemExit. A port in use or a host that is not this machine is a SystemExit too. On the
    way out the web server is stopped first, so requests in flight finish; then every other
    task (any that failed is logged); then the channels; and only then is the store closed."""
    cfg = config.load(home)
    try:
        sock = listen(cfg)
    except OSError:
        raise SystemExit(f"Port {cfg.port} is in use. Is `attache run` already running?") from None
    services = build(home)
    rt, page = router.Router(services), web.WebChannel()  # rt sets itself as scheduler.fire
    url = f"{services.web_url}/#t={services.ui_token}"
    print(f"Open {url}", flush=True)

    def on_answer_for(name: str) -> OnAnswer:
        """A sidecar's answers carry its platform sender id; approvals want the person id."""

        def on_answer(ask_id: str, sender_id: str, choice: str) -> bool:
            person = cfg.person(name, sender_id)
            return services.approvals.answer(ask_id, person.id if person else sender_id, choice)

        return on_answer

    term = channels.TerminalChannel(cfg.owner.name, url)
    for channel in [page, *([term] if terminal else []), *channels.sidecars(cfg, on_answer_for)]:
        services.channels.add(channel)
    server = asyncio.create_task(web.serve(web.create_app(services, rt, page), sock))
    beat = asyncio.create_task(services.scheduler.run())
    sdks = {SDKS[e.api] for e in cfg.endpoints.values() if e.api in SDKS}  # so turn 1 won't wait
    rest = [
        beat,
        *(asyncio.create_task(asyncio.to_thread(importlib.import_module, m)) for m in sdks),
    ]
    rest += await services.channels.start(rt.handle)
    ending = [server, beat, *([asyncio.create_task(term.done.wait())] if terminal else [])]
    try:
        done, _ = await asyncio.wait(ending, return_when=asyncio.FIRST_COMPLETED)
        if server in done or beat in done:
            raise SystemExit("The web server or the scheduler stopped; see the log above.")
    finally:
        for group in ([server], [*rest, *ending[2:], *rt.background.values()]):  # web first
            for task in group:
                task.cancel()
            for result in await asyncio.gather(*group, return_exceptions=True):
                if isinstance(result, Exception):
                    logging.getLogger(__name__).error("a task failed", exc_info=result)
        await services.channels.stop()
        services.store.close()
