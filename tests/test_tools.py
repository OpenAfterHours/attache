from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from attache import app, config, tools
from attache.scheduler import Scheduler
from attache.store import Store
from attache.types import Outbound, Person, TurnContext

GROUP = "slack:CFAM"
SKILL = """---
name: {name}
description: A test skill.
{meta}---
Body.
"""


class Channels:
    def __init__(self) -> None:
        self.sent: list[Outbound] = []

    async def send(self, msg: Outbound) -> None:
        self.sent.append(msg)


@pytest.fixture
def services(home: Path) -> Iterator[app.Services]:
    db = Store(home / "attache.db")
    yield app.Services(config.load(home), db, Scheduler(db), Channels(), None, "http://test", b"k")
    db.close()


def ctx(services: app.Services, role: str = "owner", chat: str = "terminal:local") -> TurnContext:
    who = Person("owner", "Alex", "owner") if role == "owner" else Person("sam", "Sam", role)
    return TurnContext(chat, who, chat.startswith("slack:"), services)


def call(context: TurnContext, name: str, /, **args: Any) -> str:
    tool = next(t for t in tools.build(context) if t.name == name)
    return asyncio.run(tool.fn(args, context))


def add_skill(home: Path, name: str, meta: str = "") -> Path:
    (scripts := home / "skills" / name / "scripts").mkdir(parents=True)
    (home / "skills" / name / "SKILL.md").write_text(SKILL.format(name=name, meta=meta), "utf-8")
    (scripts / "hello.py").write_text("print('hi')\n", encoding="utf-8")
    return scripts / "hello.py"


def test_slug() -> None:
    assert tools.slug("Liste de Noël!") == "liste-de-noel"
    assert tools.slug("slack:C0123") == "slack-c0123"
    assert tools.slug("--") == "untitled"
    assert len(tools.slug("x" * 100)) == 60


def test_paths_stay_inside_home(services: app.Services, tmp_path: Path) -> None:
    c = ctx(services)
    home = services.home.resolve()
    assert tools.resolve(c, "notes/a.txt") == home / "notes" / "a.txt"
    assert tools.resolve(c, str(home / "b.txt")) == home / "b.txt"
    for bad in ["../outside.txt", "notes/../../x", str(tmp_path / "elsewhere.txt")]:
        with pytest.raises(PermissionError):
            tools.resolve(c, bad)
    assert tools.chat_files(ctx(services, chat=GROUP)) == services.home / "files" / "slack-cfam"
    assert (services.home / "files" / "slack-cfam").is_dir()


def test_read_write_edit(services: app.Services) -> None:
    c = ctx(services)
    assert "11 characters" in call(c, "write_file", path="notes/a.txt", content="hello world")
    assert call(c, "read_file", path="notes/a.txt") == "hello world"
    call(c, "edit_file", path="notes/a.txt", old="world", new="there")
    assert call(c, "read_file", path="notes/a.txt") == "hello there"
    with pytest.raises(ValueError, match="occurs 2 times"):
        call(c, "edit_file", path="notes/a.txt", old="l", new="L")
    with pytest.raises(PermissionError):
        call(c, "write_file", path="../escape.txt", content="x")


def test_run(services: app.Services) -> None:
    out = json.loads(call(ctx(services), "run", command=f'"{sys.executable}" -c "print(\'hi\')"'))
    assert out["exit_code"] == 0 and out["output"].strip() == "hi"
    failed = json.loads(call(ctx(services), "run", command=f'"{sys.executable}" -c "exit(3)"'))
    assert failed["exit_code"] == 3


def test_exec_kills_on_timeout_and_caps_output(home: Path) -> None:
    # An argument list, not the shell: a killed shell can leave its own children running.
    slow = [sys.executable, "-c", "import time; time.sleep(20)"]

    async def timed_out() -> None:
        with pytest.raises(TimeoutError, match=r"stopped after 0\.5 seconds"):
            await tools._exec(asyncio.create_subprocess_exec, slow, home, 0.5)
        await asyncio.sleep(0.5)  # the killed process's pipes close while the loop still runs

    start = time.time()
    asyncio.run(timed_out())
    assert time.time() - start < 5
    loud = [sys.executable, "-c", "print('x' * 30000 + 'end')"]
    out = json.loads(asyncio.run(tools._exec(asyncio.create_subprocess_exec, loud, home, 30)))
    assert len(out["output"]) == 20_000 and out["output"].rstrip().endswith("end")


def test_script_member_gating(services: app.Services, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[list[str], Path]] = []

    async def fake_exec(spawn: Any, argv: list[str], cwd: Path, timeout: float) -> str:
        seen.append((argv, cwd))
        return "{}"

    monkeypatch.setattr(tools, "_exec", fake_exec)
    shared = add_skill(services.home, "shared", "metadata:\n  attache-roles: owner member\n")
    add_skill(services.home, "private")
    member = ctx(services, "member", GROUP)
    call(member, "script", skill="shared", script="hello.py", args=["a", 1])
    assert seen[-1][0][1:] == ["run", str(shared), "a", "1"]
    assert seen[-1][1] == services.home / "files" / "slack-cfam"
    with pytest.raises(PermissionError):
        call(member, "script", skill="private", script="hello.py")
    call(ctx(services), "script", skill="private", script="hello.py")
    for skill, script in [("private", "../SKILL.md"), ("..", "x.py"), ("nope", "hello.py")]:
        with pytest.raises(FileNotFoundError):
            call(ctx(services), "script", skill=skill, script=script)
    monkeypatch.setattr(tools.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="uv is not installed"):
        call(ctx(services), "script", skill="shared", script="hello.py")


def test_send_file_rules(services: app.Services) -> None:
    (services.home / "notes.txt").write_text("x", encoding="utf-8")
    member = ctx(services, "member", GROUP)
    (folder := tools.chat_files(member)).joinpath("list.pdf").write_bytes(b"%PDF")
    call(ctx(services), "send", text="from home", files=["notes.txt"])
    call(member, "send", text="here", files=["files/slack-cfam/list.pdf"])
    sent = services.channels.sent
    assert sent[0] == Outbound(
        "terminal:local", "from home", [str(services.home.resolve() / "notes.txt")]
    )
    assert sent[1].chat_key == GROUP and sent[1].files == [str(folder.resolve() / "list.pdf")]
    with pytest.raises(FileNotFoundError, match=r"notes\.txt"):
        call(member, "send", text="no", files=["notes.txt"])
    with pytest.raises(FileNotFoundError):
        call(ctx(services), "send", text="no", files=["missing.txt"])
    with pytest.raises(PermissionError):
        call(member, "send", text="no", files=["../../secret"])
    assert len(sent) == 2


def test_db_member_rules(services: app.Services) -> None:
    owner, member = ctx(services), ctx(services, "member", GROUP)
    call(owner, "db", op="put", collection="lists", id="trip", data={"x": 1, "_shared": [GROUP]})
    call(owner, "db", op="put", collection="lists", id="secret", data={"x": 2})
    assert [d["id"] for d in json.loads(call(member, "db", op="list", collection="lists"))] == [
        "trip"
    ]
    with pytest.raises(PermissionError):
        call(member, "db", op="get", collection="lists", id="secret")
    with pytest.raises(PermissionError):
        call(member, "db", op="delete", collection="lists", id="trip")
    new = json.loads(call(member, "db", op="put", collection="lists", data={"item": "ferry"}))
    assert new["_shared"] == [GROUP] and new["_by"] == "sam"
    assert len(json.loads(call(owner, "db", op="list", collection="lists"))) == 3


def test_artifact(services: app.Services) -> None:
    html = "<html><head><title> Trip list </title></head><body></body></html>"
    out = call(
        ctx(services, chat=GROUP), "artifact", name="Trip List", html=html, collections=["trip"]
    )
    assert json.loads(out) == {"url": "http://test/a/trip-list"}
    assert (services.home / "artifacts" / "trip-list.html").read_text(encoding="utf-8") == html
    doc = services.store.doc_get("_artifacts", "trip-list")
    assert doc is not None
    assert (doc["name"], doc["title"], doc["collections"]) == ("Trip List", "Trip list", ["trip"])
    assert (doc["chat_key"], doc["by"]) == (GROUP, "owner")
    call(ctx(services), "artifact", name="Mine", html="<p>no title</p>")
    mine = services.store.doc_get("_artifacts", "mine")
    assert mine is not None and mine["chat_key"] is None and mine["title"] == "Mine"
    member = ctx(services, "member", GROUP)
    call(member, "artifact", name="trip list", html="<p>v2</p>")  # same chat: may update
    with pytest.raises(PermissionError):
        call(member, "artifact", name="mine", html="<p>taken over</p>")


def test_schedule(services: app.Services) -> None:
    owner, member = ctx(services), ctx(services, "member", GROUP)
    soon = json.loads(call(owner, "schedule", op="add", text="tea", in_minutes=10))
    assert abs(soon["due"] - (time.time() + 600)) < 5 and soon["kind"] == "message"
    at = json.loads(
        call(owner, "schedule", op="add", text="call", at="2030-01-02T08:30", kind="turn")
    )
    assert at["due"] == datetime(2030, 1, 2, 8, 30).timestamp() and at["kind"] == "turn"
    utc = json.loads(call(owner, "schedule", op="add", text="utc", at="2030-01-02T08:30+00:00"))
    assert utc["due"] == datetime.fromisoformat("2030-01-02T08:30+00:00").timestamp()
    daily = json.loads(call(member, "schedule", op="add", text="bins", cron="0 8 * * *"))
    assert (daily["chat_key"], daily["by"], daily["cron"]) == (GROUP, "sam", "0 8 * * *")
    with pytest.raises(ValueError):
        call(member, "schedule", op="add", text="triage", in_minutes=1, kind="turn")
    assert [t["text"] for t in json.loads(call(member, "schedule", op="list"))] == ["bins"]
    assert len(json.loads(call(owner, "schedule", op="list"))) == 3
    assert len(json.loads(call(owner, "schedule", op="list", all=True))) == 4
    with pytest.raises(KeyError):
        call(member, "schedule", op="cancel", id=soon["id"])
    assert "cancelled" in call(member, "schedule", op="cancel", id=str(daily["id"]))
    assert "cancelled" in call(owner, "schedule", op="cancel", id=soon["id"])
    assert len(json.loads(call(owner, "schedule", op="list", all=True))) == 2


def test_build_filters_by_role(services: app.Services) -> None:
    member = ["script", "send", "db", "artifact", "schedule"]
    owner = ["read_file", "write_file", "edit_file", "run", *member]
    assert [t.name for t in tools.build(ctx(services))] == owner
    assert [t.name for t in tools.build(ctx(services, "member", GROUP))] == member
    assert tools.build(ctx(services, "guest", GROUP)) == []
    for tool in tools.build(ctx(services)):
        assert tool.description.strip() and tool.schema["type"] == "object"
        assert set(tool.schema["required"]) <= set(tool.schema["properties"])
    schema = next(t for t in tools.build(ctx(services)) if t.name == "schedule").schema
    assert schema["required"] == ["op"] and schema["properties"]["in_minutes"] == {"type": "number"}
    assert next(t for t in tools.build(ctx(services)) if t.name == "send").schema == {
        "type": "object",
        "properties": {"text": {"type": "string"}, "files": tools.ARRAY},
        "required": ["text"],
    }
