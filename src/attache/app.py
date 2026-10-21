"""Wire the services together and run them."""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import secrets
import signal
from dataclasses import dataclass, field
from pathlib import Path
from socket import AF_INET, AF_INET6, create_server, socket

from attache import channels, config, router, scheduler, store, tools, web
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
    snapshots: dict[str, tuple[str, float]] = field(default_factory=dict)  # web.snapshot_url's

    @property
    def home(self) -> Path:
        return self.config.home


def listen(host: str, port: int) -> socket:
    """Bind host:port. Attaché serves this machine only, so a host that is not a loopback
    address ([web] host) is a SystemExit; share links reach it through Tailscale Funnel, which
    connects from 127.0.0.1. A port in use raises OSError."""
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit(f"[web] host = {host!r}: use 127.0.0.1, localhost or ::1 for now")
    return create_server((host, port), family=AF_INET6 if host == "::1" else AF_INET)


def build(home: Path) -> Services:
    """Load config and open the store. No network, no tasks started."""
    cfg = config.load(home)
    db, hub = store.Store(home / "attache.db"), channels.ChannelManager()
    url = f"http://{'[::1]' if cfg.host == '::1' else cfg.host}:{cfg.port}"
    return Services(cfg, db, scheduler.Scheduler(db), hub, {}, url)


async def run(home: Path, *, terminal: bool) -> None:
    """Start the web server, scheduler and channels (plus the terminal chat when asked), and
    with [web] public_url set a second web server, for share links only, on 127.0.0.1:[web]
    public_port. Then ask the owner again about members' requests still open.

    Returns when the terminal session ends (EOF or /quit) or on Ctrl+C. A channel that stops,
    a sidecar included, never ends the run; a web server or the scheduler stopping does, as
    a SystemExit. A port in use or a host that is not this machine is a SystemExit too."""
    cfg = config.load(home)
    try:
        sock = listen(cfg.host, cfg.port)
        public = listen("127.0.0.1", cfg.public_port) if cfg.public_url else None
    except OSError:
        ports = f"{cfg.port} or {cfg.public_port}" if cfg.public_url else cfg.port
        raise SystemExit(f"Port {ports} is in use. Is `attache run` already running?") from None
    services = build(home)
    os.environ["ATTACHE_WEB_URL"] = services.web_url  # for skill scripts and sidecars
    os.environ["ATTACHE_HOME"] = str(home.resolve())  # for them too, wherever they run
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
    servers = [asyncio.create_task(web.serve(web.create_app(services, page, rt), sock))]
    if public:  # the share links' app, for `tailscale funnel` (docs/configuration.md)
        shared = web.create_app(services, page, public=True)
        servers.append(asyncio.create_task(web.serve(shared, public)))
    beat = asyncio.create_task(services.scheduler.run())
    sdks = {SDKS[e.api] for e in cfg.endpoints.values() if e.api in SDKS}  # so turn 1 won't wait
    warmups = [asyncio.create_task(asyncio.to_thread(importlib.import_module, m)) for m in sdks]
    channel_loops = await services.channels.start(rt.handle)
    services.channels.spawn(tools.resume_requests(services))
    vital = [*servers, beat]  # if one of these stops, so does Attaché
    quitting = [asyncio.create_task(term.done.wait())] if terminal else []
    loop, previous = asyncio.get_running_loop(), signal.getsignal(signal.SIGINT)
    if hasattr(signal, "SIGBREAK"):  # Windows' Ctrl+Break, how the tui skill stops Attaché:
        signal.signal(signal.SIGBREAK, previous)  # the same quiet shutdown as Ctrl+C gives

    def interrupt(signum: int, frame: object) -> None:
        """Ctrl+C in `attache chat`: stop your turn if one is running (its footer then says
        "stopped"), else quit as before."""
        if not services.channels.stop_turns("local:me"):
            previous(signum, frame)  # asyncio.run's own handler: the usual quiet shutdown

    if terminal:  # from the signal handler to the loop, which is not safe to touch from there
        signal.signal(signal.SIGINT, lambda *sig: loop.call_soon_threadsafe(interrupt, *sig))
    try:
        done, _ = await asyncio.wait([*vital, *quitting], return_when=asyncio.FIRST_COMPLETED)
        if done & set(vital):
            raise SystemExit("A web server or the scheduler stopped; see the log above.")
        if services.channels.tasks:  # the terminal quit, maybe at the end of piped input:
            # let its turns reply first, for as long as the slowest endpoint may take
            timeouts = [float(e.options.get("timeout", 900)) for e in cfg.endpoints.values()]
            print("Waiting for the last replies; Ctrl+C stops.", flush=True)
            await asyncio.wait(services.channels.tasks, timeout=max(timeouts, default=900))
    finally:  # in this order:
        signal.signal(signal.SIGINT, previous)  # Ctrl+C as it was (for `attache run`, it still is)
        await services.channels.stop()  # the turns still running, then every channel, which
        # ends the page's event streams, so the web server's own shutdown is quick and quiet
        everything = [*vital, *warmups, *channel_loops, *quitting]
        for task in everything:
            task.cancel()
        for result in await asyncio.gather(*everything, return_exceptions=True):
            if isinstance(result, Exception):  # it had failed: say so
                logging.getLogger(__name__).error("a task failed", exc_info=result)
        services.store.close()  # last, once nothing can write to it
