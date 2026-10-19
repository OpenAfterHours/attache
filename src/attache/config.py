"""Load <home>/attache.toml into dataclasses; create a fresh home. Owner: foundation. ~80 lines."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from attache.types import Endpoint, Person


@dataclass
class ChatConfig:
    key: str  # "<channel>:<chat_id>"
    name: str = ""
    engage: str = "mention"  # mention | always | a regular expression matched against the text
    members: list[str] = field(default_factory=list)  # person ids from [people]
    guests: str = "ignore"  # ignore | public
    endpoint: str | None = None  # overrides [routing] for this chat


@dataclass
class Config:
    home: Path
    owner: Person  # id "owner", role "owner"
    people: dict[str, Person]
    chats: dict[str, ChatConfig]
    endpoints: dict[str, Endpoint]
    own_chats: str = "claude"  # endpoint for your own chats (terminal, web, your DMs)
    group_chats: str = "claude-api"  # endpoint for group chats
    host: str = "127.0.0.1"
    port: int = 8765
    name: str = "Attaché"  # how the assistant names itself; also its default trigger word

    def person(self, channel: str, sender_id: str) -> Person | None:
        """Who sent this? `terminal` and `web` senders are always the owner."""
        raise NotImplementedError

    def chat(self, chat_key: str) -> ChatConfig | None:
        raise NotImplementedError

    def endpoint_for(self, chat_key: str, is_group: bool) -> Endpoint:
        """The chat's own endpoint if set, else [routing] group_chats or own_chats."""
        raise NotImplementedError


def home_dir() -> Path:
    """$ATTACHE_HOME if set, else ~/.attache."""
    raise NotImplementedError


def load(home: Path | None = None) -> Config:
    """Read <home>/attache.toml. A missing file gives the defaults from bundled/attache.toml."""
    raise NotImplementedError


def init_home(home: Path, owner_name: str, endpoint: str = "claude") -> list[Path]:
    """Create a home: attache.toml, memory/, skills/ (bundled copies), artifacts/home.html,
    .secret (32 random bytes, hex) and a git repo with a first commit.

    Never overwrites an existing file. Returns the paths it created.
    """
    raise NotImplementedError
