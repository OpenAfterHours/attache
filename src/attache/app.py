"""Wire the services together and run them. Owner: runtime. Budget: ~60 lines."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from attache.channels import ChannelManager
from attache.config import Config
from attache.scheduler import Scheduler
from attache.store import Store
from attache.web import ToolGateway


@dataclass
class Services:
    config: Config
    store: Store
    scheduler: Scheduler
    channels: ChannelManager
    gateway: ToolGateway  # lets CLI backends call our tools over HTTP (see mcp.py)
    web_url: str  # e.g. http://127.0.0.1:8765
    secret: bytes  # HMAC key for artifact tokens and signed links, from <home>/.secret

    @property
    def home(self) -> Path:
        return self.config.home


def build(home: Path) -> Services:
    """Load config and open the store. No network, no tasks started."""
    raise NotImplementedError


async def run(home: Path, *, terminal: bool) -> None:
    """Start the web server, scheduler and channels (plus the terminal REPL when asked).

    Returns when the terminal session ends or on Ctrl+C. If the web port is taken, raise
    SystemExit with a message naming the port and suggesting `attache run` is already up.
    """
    raise NotImplementedError
