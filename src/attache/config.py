"""Load <home>/attache.toml into dataclasses; create a fresh home. Owner: foundation. ~80 lines."""

from __future__ import annotations

import os
import secrets
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from attache import skills
from attache.types import Endpoint, Person

BUNDLED = Path(__file__).parent / "bundled"


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
        if channel in ("terminal", "web"):
            return self.owner
        known = [self.owner, *self.people.values()]  # empty ids were dropped by load()
        return next((p for p in known if p.ids.get(channel) == sender_id), None)

    def chat(self, chat_key: str) -> ChatConfig | None:
        return self.chats.get(chat_key)

    def endpoint_for(self, chat_key: str, is_group: bool) -> Endpoint:
        """The chat's own endpoint if set, else [routing] group_chats or own_chats."""
        chat = self.chats.get(chat_key)
        name = (chat and chat.endpoint) or (self.group_chats if is_group else self.own_chats)
        if name not in self.endpoints:
            raise ValueError(f"endpoint {name!r} (for {chat_key}) is not in attache.toml")
        return self.endpoints[name]


def home_dir() -> Path:
    """$ATTACHE_HOME if set, else ~/.attache."""
    return Path(os.environ.get("ATTACHE_HOME") or Path.home() / ".attache").expanduser()


def _template(owner: str, endpoint: str) -> str:
    text = (BUNDLED / "attache.toml").read_text(encoding="utf-8")
    for key, value in (("{owner}", owner), ("{endpoint}", endpoint)):
        text = text.replace(key, value.replace("\\", "\\\\").replace('"', '\\"'))
    return text


def _ids(table: dict[str, Any]) -> dict[str, str]:
    return {k: str(v) for k, v in table.items() if k not in ("name", "role") and v}


def _endpoint(name: str, table: dict[str, Any]) -> Endpoint:
    known = {k: table.pop(k) for k in ("api", "model", "base_url", "key_env") if k in table}
    return Endpoint(name, **{"model": "", **known}, options=table)


def load(home: Path | None = None) -> Config:
    """Read <home>/attache.toml. A missing file gives the defaults from bundled/attache.toml."""
    home = home or home_dir()
    path = home / "attache.toml"
    text = path.read_text(encoding="utf-8") if path.exists() else _template("you", "claude")
    raw = tomllib.loads(text)
    own = raw.get("owner", {})
    owner = Person("owner", own.get("name", "you"), "owner", _ids(own.get("ids", {})))
    people = {
        pid: Person(pid, p.get("name", pid), p.get("role", "member"), _ids(p))
        for pid, p in raw.get("people", {}).items()
    }
    chats = {key: ChatConfig(key, **c) for key, c in raw.get("chats", {}).items()}
    endpoints = {name: _endpoint(name, e) for name, e in raw.get("endpoints", {}).items()}
    # [routing], [web] and [assistant] keys are Config fields; an unknown key raises TypeError.
    tables = {**raw.get("routing", {}), **raw.get("web", {}), **raw.get("assistant", {})}
    return Config(home, owner, people, chats, endpoints, **tables)


def init_home(home: Path, owner_name: str, endpoint: str = "claude") -> list[Path]:
    """Create a home: attache.toml, memory/, skills/ (bundled copies), artifacts/home.html,
    .secret (32 random bytes, hex) and a git repo with a first commit.

    Never overwrites an existing file. Returns the paths it created.
    """
    files = {
        "attache.toml": _template(owner_name, endpoint),
        ".secret": secrets.token_hex(32),
        ".gitignore": "attache.db*\n.secret\nfiles/\nlogs/\n",
    }
    for name in ("USER.md", "ATTACHE.md"):
        text = (BUNDLED / "memory" / name).read_text(encoding="utf-8")
        files[f"memory/{name}"] = text.replace("{owner}", owner_name)
    page = skills.BUNDLED / "artifact-kit" / "templates" / "home.html"
    if page.exists():  # the artifact-kit skill provides it; skip until it does
        files["artifacts/home.html"] = page.read_text(encoding="utf-8")
    created = []
    for rel, text in files.items():
        if not (home / rel).exists():
            (home / rel).parent.mkdir(parents=True, exist_ok=True)
            (home / rel).write_text(text, encoding="utf-8")
            created.append(home / rel)
    (home / ".secret").chmod(0o600)  # the HMAC key for artifact and UI tokens
    for rel in ("memory/chats", "artifacts", "files", "logs"):
        (home / rel).mkdir(parents=True, exist_ok=True)
    created += skills.install_bundled(home)
    skills.git(home, "init", "-b", "main")
    skills.commit(home, "attache init")
    return created
