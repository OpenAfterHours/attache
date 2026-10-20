"""Load <home>/attache.toml into dataclasses, and create a fresh home."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from attache import skills
from attache.types import Endpoint, Person

BUNDLED = Path(__file__).parent / "bundled"
PERSON_KEYS = ("name", "role", "ids", "approvals")  # others are channel ids, like slack = "U01"
# The [routing], [web] and [assistant] keys, which are all Config fields.
SECTIONS = {"routing": ["own_chats", "group_chats"], "web": ["host", "port"], "assistant": ["name"]}
LIMITS = {"per_sender_hour": 20, "per_chat_hour": 60, "member_daily": 200}  # turns, non-owners


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
    own_chats: str = "claude"  # endpoint for your own chats (local, your direct messages)
    group_chats: str = "claude-api"  # endpoint for group chats and anyone but you
    host: str = "127.0.0.1"
    port: int = 8765
    name: str = "Attaché"  # how the assistant names itself; also its default trigger word
    channels: dict[str, dict[str, Any]] = field(default_factory=dict)  # [channels.<name>]
    approvals: str = "local:me"  # the chat where approval requests reach the owner
    limits: dict[str, int] = field(default_factory=lambda: dict(LIMITS))

    def person(self, channel: str, sender_id: str) -> Person | None:
        """Who sent this? Senders on the `local` channel (terminal and web) are the owner."""
        if channel == "local":
            return self.owner
        for person in [self.owner, *self.people.values()]:
            if person.ids.get(channel) == sender_id:  # load() drops empty ids
                return person
        return None

    def endpoint_for(self, chat_key: str, is_group: bool) -> Endpoint:
        """The chat's own endpoint if set, else [routing] group_chats or own_chats."""
        chat = self.chats.get(chat_key)
        default = self.group_chats if is_group else self.own_chats
        name = chat.endpoint if chat and chat.endpoint else default
        if name not in self.endpoints:
            raise ValueError(f"endpoint {name!r} (for {chat_key}) is not in attache.toml")
        return self.endpoints[name]


def home_dir() -> Path:
    """$ATTACHE_HOME if set, else ~/.attache."""
    env = os.environ.get("ATTACHE_HOME")
    return Path(env).expanduser() if env else Path.home() / ".attache"


def load(home: Path | None = None) -> Config:
    """Read <home>/attache.toml. A missing file gives the defaults from bundled/attache.toml.

    Raises ValueError, naming the file, for bad TOML, an unknown key or a bad port.
    """
    home = home or home_dir()
    path = home / "attache.toml"
    # utf-8-sig: PowerShell writes a byte order mark.
    text = path.read_text(encoding="utf-8-sig") if path.exists() else _template("you", "claude")
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"{path}: {e}") from None

    own = raw.get("owner", {})
    owner = Person("owner", own.get("name", "you"), "owner", _ids(own))
    people = {}
    for pid, table in raw.get("people", {}).items():
        people[pid] = Person(pid, table.get("name", pid), table.get("role", "member"), _ids(table))
    chat_keys = [f.name for f in fields(ChatConfig) if f.name != "key"]
    chats = {}
    for key, table in raw.get("chats", {}).items():
        chats[key] = ChatConfig(key, **_check(path, f'chats."{key}"', table, chat_keys))
    endpoints = {}
    for name, table in raw.get("endpoints", {}).items():
        api, model = table.pop("api", ""), table.pop("model", "")
        base_url, key_env = table.pop("base_url", None), table.pop("key_env", None)
        endpoints[name] = Endpoint(name, api, model, base_url, key_env, options=table)
    channels = raw.get("channels", {})
    for name, table in channels.items():
        if name == "local":
            raise ValueError(f"{path}: [channels.local] is taken by the terminal and web page")
        if not isinstance(table.setdefault("enabled", False), bool):
            raise ValueError(f"{path}: [channels.{name}] enabled must be true or false")
        for key, value in table.items():
            if key != "enabled" and not isinstance(value, str):  # the sidecar gets them as env
                raise ValueError(f"{path}: [channels.{name}] {key} must be a string")
    limits = {**LIMITS, **_check(path, "limits", raw.get("limits", {}), list(LIMITS))}
    for key, value in limits.items():
        if type(value) is not int or value < 1:  # type(): true and false are not numbers here
            raise ValueError(f"{path}: [limits] {key} must be a whole number of at least 1")
    approvals = str(own.get("approvals", "local:me"))
    channel, _, chat_id = approvals.partition(":")
    direct = approvals == "local:me" or (channels.get(channel, {}).get("enabled") and chat_id)
    if not direct or approvals in chats:  # questions go to your own direct chat, never a group
        raise ValueError(f'{path}: [owner] approvals must be "local:me" or "<channel>:<your DM>"')
    settings: dict[str, Any] = {"channels": channels, "limits": limits, "approvals": approvals}
    for section, keys in SECTIONS.items():
        settings.update(_check(path, section, raw.get(section, {}), keys))
    try:
        settings["port"] = int(settings.get("port", 8765))
    except (TypeError, ValueError):
        raise ValueError(f"{path}: [web] port must be a number, not {settings['port']!r}") from None
    return Config(home, owner, people, chats, endpoints, **settings)


def _template(owner: str, endpoint: str) -> str:
    """The bundled attache.toml with {owner} and {endpoint} filled in as TOML strings."""
    text = (BUNDLED / "attache.toml").read_text(encoding="utf-8")
    for placeholder, value in (("{owner}", owner), ("{endpoint}", endpoint)):
        text = text.replace(placeholder, value.replace("\\", "\\\\").replace('"', '\\"'))
    return text


def _check(path: Path, section: str, table: dict[str, Any], known: list[str]) -> dict[str, Any]:
    """Return `table`, or raise ValueError naming the keys it has that are not `known`."""
    unknown = ", ".join(sorted(set(table) - set(known)))
    if unknown:
        raise ValueError(f"{path}: unknown key {unknown} in [{section}]; known: {', '.join(known)}")
    return table


def _ids(table: dict[str, Any]) -> dict[str, str]:
    """Channel ids from an `ids` subtable and from plain `<channel> = "<id>"` keys."""
    ids = {key: value for key, value in table.items() if key not in PERSON_KEYS}
    ids.update(table.get("ids", {}))
    return {channel: str(sender) for channel, sender in ids.items() if sender}


def init_home(home: Path, owner_name: str, endpoint: str = "claude") -> list[Path]:
    """Create a home: attache.toml, memory/, skills/ (bundled copies), artifacts/home.html and
    a git repo with a first commit. app.build makes .secret.

    Never overwrites an existing file. Returns the paths it created.
    """
    home_page = skills.BUNDLED / "artifact-kit" / "templates" / "home.html"
    files = {
        "attache.toml": _template(owner_name, endpoint),
        ".gitignore": "attache.db*\n.secret\nfiles/\nlogs/\n",
        "artifacts/home.html": home_page.read_text(encoding="utf-8"),
    }
    for name in ("USER.md", "ATTACHE.md"):
        text = (BUNDLED / "memory" / name).read_text(encoding="utf-8")
        files[f"memory/{name}"] = text.replace("{owner}", owner_name)

    created = []
    for relative, text in files.items():
        path = home / relative
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            created.append(path)
    for folder in ("memory/chats", "files", "logs"):
        (home / folder).mkdir(parents=True, exist_ok=True)
    created += skills.install_bundled(home)
    skills.git(home, "init", "-b", "main")
    skills.commit(home, "attache init")
    return created
