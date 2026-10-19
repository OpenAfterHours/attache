"""Shared types. Modules talk to each other through these, so keep them small."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from attache.app import Services


@dataclass
class Person:
    """Someone Attaché knows. `id` is the key under [people] in attache.toml, "owner" for you."""

    id: str
    name: str
    role: str = "guest"  # owner | member | guest
    ids: dict[str, str] = field(default_factory=dict)  # channel name -> platform sender id


@dataclass
class Endpoint:
    """One [endpoints.<name>] block: which protocol, which model, where."""

    name: str
    api: str  # anthropic | openai-chat | openai-responses | claude-cli
    model: str
    base_url: str | None = None
    key_env: str | None = None
    options: dict[str, Any] = field(default_factory=dict)  # e.g. max_tokens, bin


@dataclass
class Inbound:
    """A message arriving from any channel, already normalised by its adapter."""

    channel: str
    chat_id: str
    sender_id: str
    sender_name: str
    text: str
    is_group: bool = False
    mentioned: bool = False  # set by the channel: a native mention or a reply to the bot
    message_id: str = ""
    files: list[str] = field(default_factory=list)  # local paths of downloaded attachments
    ts: float = field(default_factory=time.time)

    @property
    def chat_key(self) -> str:
        return f"{self.channel}:{self.chat_id}"


@dataclass
class Outbound:
    """Something to deliver to a chat. `chat_key` is "<channel>:<chat_id>"."""

    chat_key: str
    text: str = ""
    files: list[str] = field(default_factory=list)
    reply_to: str | None = None


OnMessage = Callable[[Inbound], Awaitable[None]]


class Channel(Protocol):
    name: str

    async def start(self, on_message: OnMessage) -> None: ...  # long-running

    async def send(self, msg: Outbound) -> None: ...

    async def stop(self) -> None: ...


@dataclass
class TurnContext:
    """What tools can reach during one turn."""

    chat_key: str
    sender: Person
    is_group: bool
    services: Services

    @property
    def role(self) -> str:
        return self.sender.role


ToolFn = Callable[[dict[str, Any], TurnContext], Awaitable[str]]


@dataclass
class Tool:
    name: str
    description: str
    schema: dict[str, Any]  # JSON Schema for the arguments object
    fn: ToolFn
    roles: frozenset[str] = frozenset({"owner"})


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass
class Turn:
    """Everything a backend needs to answer one message."""

    ctx: TurnContext
    endpoint: Endpoint
    system: str
    history: list[dict[str, str]]  # earlier messages: {"role": "user"|"assistant", "content": ...}
    text: str  # the new message, sender header already applied
    tools: list[Tool]  # already filtered to what this sender's role may use
    session: dict[str, Any] = field(default_factory=dict)  # backend-owned, kept per chat+endpoint


@dataclass
class Reply:
    text: str
    session: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)


class Backend(Protocol):
    async def run(self, turn: Turn) -> Reply: ...
