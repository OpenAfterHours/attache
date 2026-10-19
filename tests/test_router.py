from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from attache import app, config, router
from attache.channels import ChannelManager
from attache.config import ChatConfig
from attache.router import APOLOGY, Router
from attache.scheduler import Scheduler
from attache.store import Store
from attache.types import Endpoint, Inbound, Outbound, Reply, Turn

TOML = """
[assistant]
name = "Attaché"

[owner]
name = "Alex"
[owner.ids]
slack = "UALEX"

[routing]
own_chats = "own"
group_chats = "group"

[endpoints.own]
api = "fake"
model = "m-own"

[endpoints.group]
api = "fake"
model = "m-group"

[people.sam]
name = "Sam"
slack = "USAM"

[people.kim]
name = "Kim"
role = "guest"
slack = "UKIM"

[chats."slack:CFAM"]
name = "Family"
members = ["sam"]

[chats."slack:CPUB"]
name = "Public"
engage = "always"
guests = "public"

[chats."slack:CRX"]
engage = "(?i)hey bot"
members = ["sam"]
"""
SKILL = "---\nname: {name}\ndescription: The {name} skill.\n{meta}---\n"


class Backend:
    def __init__(self) -> None:
        self.turns: list[Turn] = []
        self.error: Exception | None = None

    async def run(self, turn: Turn) -> Reply:
        self.turns.append(turn)
        if self.error:
            raise self.error
        return Reply(f"reply {len(self.turns)}", {"n": len(self.turns)})


class Channel:
    def __init__(self, name: str) -> None:
        self.name, self.sent = name, []

    async def start(self, on_message: Any) -> None: ...

    async def send(self, msg: Outbound) -> None:
        self.sent.append(msg)

    async def stop(self) -> None: ...


class World:
    def __init__(self, home: Path) -> None:
        (home / "attache.toml").write_text(TOML, encoding="utf-8")
        self.store = Store(home / "attache.db")
        hub = ChannelManager()
        self.terminal, self.slack = Channel("terminal"), Channel("slack")
        hub.add(self.terminal)
        hub.add(self.slack)
        cfg, scheduler = config.load(home), Scheduler(self.store)
        self.services = app.Services(cfg, self.store, scheduler, hub, None, "http://t", b"k")
        self.backend, self.endpoints, self.commits = Backend(), [], []
        self.router = Router(self.services, self.make_backend)

    def make_backend(self, endpoint: Endpoint) -> Backend:
        self.endpoints.append(endpoint)
        return self.backend

    def say(self, text: str, sender: str = "USAM", chat: str = "CFAM", **kw: Any) -> Reply | None:
        group = kw.pop("is_group", not chat.startswith("D"))
        msg = Inbound("slack", chat, sender, kw.pop("name", "Someone"), text, group, **kw)
        return asyncio.run(self.router.handle(msg))


@pytest.fixture
def world(home: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    w = World(home)
    monkeypatch.setattr(router.skills, "commit", lambda home, message: w.commits.append(message))
    yield w
    w.store.close()


def test_owner_terminal_chat(world: World) -> None:
    reply = asyncio.run(
        world.router.handle(Inbound("terminal", "local", "owner", "Alex", "hi\nthere"))
    )
    assert reply is not None and reply.text == "reply 1"
    assert world.terminal.sent == [Outbound("terminal:local", "reply 1")]
    turn = world.backend.turns[0]
    assert turn.endpoint.name == "own" and turn.ctx.role == "owner" and turn.history == []
    assert turn.text.startswith("[Alex, owner, terminal, ") and turn.text.endswith("]\nhi\nthere")
    assert "read_file" in [t.name for t in turn.tools]
    assert world.commits == ["terminal:local: hi there"]
    assert world.store.session_get("terminal:local", "own") == {"n": 1}
    asyncio.run(world.router.handle(Inbound("terminal", "local", "owner", "Alex", "again")))
    assert world.backend.turns[1].session == {"n": 1}
    assert world.backend.turns[1].history == [
        {"role": "user", "content": "hi\nthere"},
        {"role": "assistant", "content": "reply 1"},
    ]
    assert len(world.endpoints) == 1  # the backend is cached per endpoint


def test_unlisted_group_and_unknown_direct_sender_are_ignored(world: World) -> None:
    assert world.say("@Attaché hi", chat="CNOPE", mentioned=True) is None
    assert world.say("hi", sender="UNKNOWN", chat="DM1") is None
    assert world.store.history("slack:CNOPE") == [] and world.store.history("slack:DM1") == []
    assert world.backend.turns == []


def test_member_mention_is_answered(world: World) -> None:
    reply = world.say("what time is the ferry?", name="Sam", mentioned=True, message_id="m1")
    assert reply is not None
    turn = world.backend.turns[0]
    assert (
        turn.ctx.role == "member" and turn.ctx.sender.id == "sam" and turn.endpoint.name == "group"
    )
    assert [t.name for t in turn.tools] == ["script", "send", "db", "artifact", "schedule"]
    assert world.slack.sent == [Outbound("slack:CFAM", "reply 1", reply_to="m1")]
    assert world.commits == []


def test_unmentioned_group_message_is_stored_not_answered(world: World) -> None:
    assert world.say("packing tonight", name="Sam") is None
    assert world.backend.turns == [] and world.slack.sent == []
    assert world.store.history("slack:CFAM")[0]["text"] == "packing tonight"
    world.say("did you see that?", name="Sam", mentioned=True)
    assert world.backend.turns[0].history == [{"role": "user", "content": "Sam: packing tonight"}]


def test_guests(world: World) -> None:
    (world.services.home / "memory").mkdir()
    (world.services.home / "memory" / "USER.md").write_text("Alex's private notes", "utf-8")
    assert world.say("hello", sender="UKIM", name="Kim", mentioned=True) is None
    assert world.say("hello", sender="UNKNOWN", name="Kim", mentioned=True) is None
    assert len(world.store.history("slack:CFAM")) == 2 and world.backend.turns == []
    assert world.say("hello", sender="UNKNOWN", chat="CPUB", name="Pat") is not None
    turn = world.backend.turns[0]
    assert turn.ctx.role == "guest" and turn.tools == []
    assert "private notes" not in turn.system and "## Skills" not in turn.system


def test_direct_chat_keeps_configured_role(world: World) -> None:
    world.say("hi", sender="USAM", chat="DSAM", name="Sam")
    assert (
        world.backend.turns[0].ctx.role == "member"
        and world.backend.turns[0].endpoint.name == "own"
    )
    assert world.say("hi", sender="UKIM", chat="DKIM", name="Kim") is None  # a guest, not public
    world.say("hi", sender="UALEX", chat="CFAM", name="Alex")  # the owner, unmentioned in a group
    assert len(world.backend.turns) == 1


def test_trigger_word(world: World) -> None:
    family = world.services.config.chat("slack:CFAM")

    def said(text: str) -> bool:
        return world.router.triggered(Inbound("slack", "CFAM", "USAM", "Sam", text, True), family)

    for text in ["Attaché, are you there?", "attache are you there", "hey @ATTACHE", "ATTACHÉ!"]:
        assert said(text), text
    for text in ["the attachment", "attachés", "nothing here"]:
        assert not said(text), text
    regex = ChatConfig("slack:CRX", engage="(?i)hey bot")
    assert world.router.triggered(Inbound("slack", "CRX", "U", "U", "Hey Bot, hi", True), regex)
    assert not world.router.triggered(Inbound("slack", "CRX", "U", "U", "Attaché?", True), regex)
    assert world.router.triggered(Inbound("slack", "CRX", "U", "U", "x", True, True), regex)
    assert world.router.triggered(Inbound("slack", "DM", "U", "U", "x"), None)


def test_system_prompt_filters_skills_for_members(world: World) -> None:
    home = world.services.home
    for name, meta in [
        ("shared", "metadata:\n  attache-roles: [owner, member]\n"),
        ("private", ""),
    ]:
        (home / "skills" / name).mkdir(parents=True)
        (home / "skills" / name / "SKILL.md").write_text(
            SKILL.format(name=name, meta=meta), "utf-8"
        )
    (home / "memory" / "chats").mkdir(parents=True)
    (home / "memory" / "ATTACHE.md").write_text("You are Attaché.", encoding="utf-8")
    (home / "memory" / "chats" / "slack-cfam.md").write_text("Family notes.", encoding="utf-8")
    family = world.services.config.chat("slack:CFAM")
    owner, member = world.services.config.owner, world.services.config.people["sam"]
    msg = Inbound("slack", "CFAM", "USAM", "Sam", "hi", True)
    full = world.router.system_prompt(owner, msg, family)
    assert "**shared**" in full and "**private**" in full
    shared = world.router.system_prompt(member, msg, family)
    assert "**shared**" in shared and "**private**" not in shared
    assert shared.startswith("You are Attaché.") and "Family notes." in shared
    assert "Sam (member) in Family" in shared and "files/slack-cfam/" in shared


def test_errors(world: World) -> None:
    world.backend.error = RuntimeError("boom")
    reply = asyncio.run(world.router.handle(Inbound("terminal", "local", "owner", "Alex", "go")))
    assert reply is not None and "RuntimeError: boom" in reply.text
    assert world.terminal.sent[0].text == reply.text
    reply = world.say("@Attaché go", name="Sam")
    assert reply is not None and reply.text == APOLOGY == world.slack.sent[0].text
    assert world.store.session_get("terminal:local", "own") == {}  # not saved on failure


def test_bad_regex_and_empty_messages_never_raise(world: World) -> None:
    world.services.config.chats["slack:CRX"].engage = "("
    assert world.say("anything", chat="CRX", name="Sam") is None
    assert (
        asyncio.run(world.router.handle(Inbound("terminal", "local", "owner", "Alex", "  ")))
        is None
    )
    assert world.backend.turns == []


def test_fire(world: World) -> None:
    asyncio.run(world.router.fire({"chat_key": "slack:CFAM", "kind": "message", "text": "bins"}))
    assert world.slack.sent == [Outbound("slack:CFAM", "Reminder: bins")]
    assert world.store.history("slack:CFAM")[-1]["text"] == "Reminder: bins"
    asyncio.run(world.router.fire({"chat_key": "terminal:local", "kind": "turn", "text": "triage"}))
    asyncio.run(world.router.fire({"chat_key": "slack:CFAM", "kind": "turn", "text": "plan"}))
    first, second = world.backend.turns
    assert first.ctx.role == "owner" and first.text.endswith("\ntriage")
    assert second.ctx.role == "owner" and second.ctx.is_group and second.endpoint.name == "group"
    assert world.terminal.sent == [Outbound("terminal:local", "reply 1")]


def test_endpoint_names_reach_the_backend_factory(world: World) -> None:
    world.say("@Attaché hi", name="Sam")
    world.say("@Attaché hi again", name="Sam")
    assert [e.name for e in world.endpoints] == ["group"]
    assert isinstance(world.endpoints[0], Endpoint)
