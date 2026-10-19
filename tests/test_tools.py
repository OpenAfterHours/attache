from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from attache import app, config, skills, tools
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
    yield app.Services(config.load(home), db, Scheduler(db), Channels(), {}, "http://test")
    db.close()


def ctx(services: app.Services, role: str = "owner", chat: str = "local:me") -> TurnContext:
    who = Person("owner", "Alex", "owner") if role == "owner" else Person("sam", "Sam", role)
    return TurnContext(chat, who, chat.startswith("slack:"), services)


def call(context: TurnContext, name: str, /, **args: Any) -> str:
    tool = next(t for t in tools.build(context) if t.name == name)
    return asyncio.run(tool.fn(args, context))


def add_skill(home: Path, name: str, meta: str = "") -> Path:
    scripts = home / "skills" / name / "scripts"
    scripts.mkdir(parents=True)
    (home / "skills" / name / "SKILL.md").write_text(SKILL.format(name=name, meta=meta), "utf-8")
    (scripts / "hello.py").write_text("print('hi')\n", encoding="utf-8")
    return scripts / "hello.py"


def test_slug_and_chat_slug() -> None:
    assert tools.slug("Liste de Noël!") == "liste-de-noel"
    assert tools.slug("--") == "untitled"
    assert len(tools.slug("x" * 100)) == 60
    one, two = tools.chat_slug("slack:C0123"), tools.chat_slug("slack:c0123")
    assert one.startswith("slack-c0123-") and len(one.rsplit("-", 1)[1]) == 8
    assert one != two  # the same slug, told apart by the hash
    assert tools.chat_slug("a:b") != tools.chat_slug("a-b")
    assert len(tools.chat_slug("signal:" + "x" * 100)) == 40 + 1 + 8


@pytest.mark.parametrize(
    "bad",
    [
        "/etc/passwd",
        "\\Windows\\win.ini",
        "C:\\Windows\\win.ini",
        "C:/Windows",
        "C:foo.txt",  # drive-relative
        "\\\\server\\share\\x",
        "//server/share/x",
        "notes.txt:stream",
        "",
        "../outside.txt",
        "notes/../../outside.txt",
    ],
)
def test_resolve_rejects_paths_outside_home(services: app.Services, bad: str) -> None:
    with pytest.raises(PermissionError):
        tools.resolve(ctx(services), bad)


def test_resolve_allows_plain_relative_paths(services: app.Services) -> None:
    home = services.home.resolve()
    assert tools.resolve(ctx(services), "notes/a.txt") == home / "notes" / "a.txt"
    assert tools.resolve(ctx(services), "notes/../b.txt") == home / "b.txt"
    with pytest.raises(PermissionError):
        tools.resolve(ctx(services), str(home / "b.txt"))  # absolute, even inside home


@pytest.mark.parametrize(
    "private",
    [
        ".secret",
        ".SECRET",
        ".secret.",
        "attache.db",
        "ATTACHE.DB-wal",
        ".git/config",
        "skills/x/.git/HEAD",
        ".claude/settings.json",
        "CLAUDE.md",
        "skills/x/claude.md",
    ],
)
def test_resolve_denies_private_files(services: app.Services, private: str) -> None:
    with pytest.raises(PermissionError, match="keeps that file"):
        tools.resolve(ctx(services), private)


def test_resolve_follows_links_out_of_home(services: app.Services, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.txt").write_text("x", encoding="utf-8")
    link = services.home / "link"
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(outside), str(link))  # no admin rights needed
    else:
        link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(PermissionError, match="outside the home folder"):
        tools.resolve(ctx(services), "link/x.txt")


def test_read_write_edit(services: app.Services) -> None:
    c = ctx(services)
    assert "11 characters" in call(c, "write_file", path="notes/a.txt", content="hello world")
    assert call(c, "read_file", path="notes/a.txt") == "hello world"
    call(c, "edit_file", path="notes/a.txt", old="world", new="there")
    assert call(c, "read_file", path="notes/a.txt") == "hello there"
    with pytest.raises(ValueError, match="exactly once"):
        call(c, "edit_file", path="notes/a.txt", old="l", new="L")
    with pytest.raises(PermissionError):
        call(c, "write_file", path="../escape.txt", content="x")


def test_run(services: app.Services) -> None:
    out = json.loads(call(ctx(services), "run", command=f'"{sys.executable}" -c "print(\'hi\')"'))
    assert out["exit_code"] == 0 and out["output"].strip() == "hi"
    failed = json.loads(call(ctx(services), "run", command=f'"{sys.executable}" -c "exit(3)"'))
    assert failed["exit_code"] == 3
    description = next(t for t in tools.build(ctx(services)) if t.name == "run").description
    assert tools.SHELL in description and "600" in description


def test_run_caps_its_timeout(services: app.Services, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[float] = []

    async def fake_exec(cmd: Any, cwd: Path, timeout: float) -> str:
        seen.append(timeout)
        return "{}"

    monkeypatch.setattr(tools, "_exec", fake_exec)
    call(ctx(services), "run", command="x", timeout=5000)
    call(ctx(services), "run", command="x")
    assert seen == [600, 60]


@pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning")
def test_run_timeout_kills_the_whole_tree(services: app.Services) -> None:
    # The shell starts python, which starts another python: all three hold the output pipe,
    # so the call only returns this fast if the whole tree was killed.
    child = "import subprocess, sys, time; "
    child += "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
    child += "time.sleep(60)"
    start = time.time()
    with pytest.raises(TimeoutError, match=r"stopped after 1 seconds"):
        call(ctx(services), "run", command=f'"{sys.executable}" -c "{child}"', timeout=1)
    assert time.time() - start < 20


def test_exec_caps_output(home: Path) -> None:
    loud = [sys.executable, "-c", "print('x' * 30000 + 'end')"]
    out = json.loads(asyncio.run(tools._exec(loud, home, 30)))
    assert len(out["output"]) == tools.OUTPUT_CAP and out["output"].rstrip().endswith("end")


def test_for_members() -> None:
    def skill(meta: Any) -> skills.Skill:
        return skills.Skill("s", "d", Path("."), {"metadata": meta})

    assert tools.for_members(skill({"attache-roles": "owner member"}))
    assert tools.for_members(skill({"attache-roles": "owner,member"}))
    assert tools.for_members(skill({"attache-roles": ["owner", "member"]}))
    assert not tools.for_members(skill({"attache-roles": "nonmember"}))
    assert not tools.for_members(skill({"attache-roles": ["members"]}))
    assert not tools.for_members(skill("attache-roles: member"))  # metadata must be a mapping
    assert not tools.for_members(skill(None))


def test_script_member_gating(services: app.Services, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[list[str], Path]] = []

    async def fake_exec(cmd: list[str], cwd: Path, timeout: float) -> str:
        seen.append((cmd, cwd))
        return "{}"

    monkeypatch.setattr(tools, "_exec", fake_exec)
    shared = add_skill(services.home, "shared", "metadata:\n  attache-roles: owner member\n")
    add_skill(services.home, "private")
    member = ctx(services, "member", GROUP)
    call(member, "script", skill="shared", script="hello.py", args=["a", 1])
    assert seen[-1][0][1:] == ["run", str(shared), "a", "1"]
    assert seen[-1][1] == tools.chat_files(member)
    with pytest.raises(PermissionError):
        call(member, "script", skill="private", script="hello.py")
    with pytest.raises(PermissionError):  # the owner in a group acts with member rights
        call(ctx(services, chat=GROUP), "script", skill="private", script="hello.py")
    call(ctx(services), "script", skill="private", script="hello.py")
    for script in ["../SKILL.md", "sub/x.py", "sub\\x.py", "C:x.py", "/x.py", ".."]:
        with pytest.raises(PermissionError):
            call(ctx(services), "script", skill="private", script=script)
    with pytest.raises(FileNotFoundError):
        call(ctx(services), "script", skill="nope", script="hello.py")
    with pytest.raises(FileNotFoundError):
        call(ctx(services), "script", skill="private", script="missing.py")
    monkeypatch.setattr(tools.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="uv is not installed"):
        call(ctx(services), "script", skill="shared", script="hello.py")


def test_send(services: app.Services) -> None:
    (services.home / "notes.txt").write_text("x", encoding="utf-8")
    member = ctx(services, "member", GROUP)
    folder = tools.chat_files(member)
    (folder / "list.pdf").write_bytes(b"%PDF")
    call(ctx(services), "send", text="from home", files=["notes.txt"])
    call(member, "send", text="here", files=[f"files/{folder.name}/list.pdf"])
    sent = services.channels.sent
    home_file = str(services.home.resolve() / "notes.txt")
    assert sent[0] == Outbound("local:me", "from home", [home_file])
    assert sent[1].chat_key == GROUP and sent[1].files == [str(folder.resolve() / "list.pdf")]
    with pytest.raises(PermissionError, match=r"notes\.txt"):
        call(member, "send", text="no", files=["notes.txt"])
    with pytest.raises(PermissionError, match="not a file"):
        call(ctx(services), "send", text="no", files=["missing.txt"])
    with pytest.raises(PermissionError):
        call(member, "send", text="no", files=["../../x"])
    assert len(sent) == 2
    assert [m["text"] for m in services.store.history(GROUP)] == ["here"]  # in the history
    assert services.store.history(GROUP)[0]["role"] == "assistant"


def test_db_member_rules(services: app.Services) -> None:
    owner, member = ctx(services), ctx(services, "member", GROUP)
    call(owner, "db", op="put", collection="lists", id="trip", data={"x": 1, "_shared": [GROUP]})
    call(owner, "db", op="put", collection="lists", id="secret", data={"x": 2})
    listed = json.loads(call(member, "db", op="list", collection="lists"))
    assert [d["id"] for d in listed] == ["trip"]
    with pytest.raises(PermissionError):
        call(member, "db", op="get", collection="lists", id="secret")
    with pytest.raises(PermissionError):
        call(member, "db", op="delete", collection="lists", id="trip")
    with pytest.raises(PermissionError):  # the owner in a group sees only the group's docs
        call(ctx(services, chat=GROUP), "db", op="get", collection="lists", id="secret")
    new = json.loads(call(member, "db", op="put", collection="lists", data={"item": "ferry"}))
    assert new["_shared"] == [GROUP] and new["_by"] == "sam"
    assert len(json.loads(call(owner, "db", op="list", collection="lists"))) == 3


def test_artifact(services: app.Services) -> None:
    store = services.store
    html = '<html><head><meta name="attache-collections" content="trip"></head></html>'
    out = call(ctx(services), "artifact", name="Trip List", html=html)
    assert json.loads(out) == {"url": "http://test/#page=trip-list"}
    assert (services.home / "artifacts" / "trip-list.html").read_text(encoding="utf-8") == html
    assert store.doc_get("_artifacts", "trip-list") is None  # the owner's own page: no record
    member = ctx(services, "member", GROUP)
    call(member, "artifact", name="Family board", html="<p>v1</p>")
    record = store.doc_get("_artifacts", "family-board")
    assert record is not None and record["chat_key"] == GROUP and record["_by"] == "sam"
    call(member, "artifact", name="family board", html="<p>v2</p>")  # its own chat's: may update
    with pytest.raises(PermissionError):
        call(member, "artifact", name="trip list", html="<p>taken over</p>")
    call(ctx(services, chat=GROUP), "artifact", name="Owner in group", html="<p>x</p>")
    record = store.doc_get("_artifacts", "owner-in-group")
    assert record is not None and record["chat_key"] == GROUP
    schema = next(t for t in tools.build(member) if t.name == "artifact").schema
    assert schema["required"] == ["name", "html"] and "collections" not in schema["properties"]


def test_schedule(services: app.Services) -> None:
    owner, member = ctx(services), ctx(services, "member", GROUP)
    soon = json.loads(call(owner, "schedule", op="add", text="tea", in_minutes=10, cron=""))
    assert abs(soon["due"] - (time.time() + 600)) < 5 and soon["kind"] == "message"
    at = json.loads(
        call(owner, "schedule", op="add", text="call", at="2030-01-02T08:30", kind="turn")
    )
    assert at["due"] == datetime(2030, 1, 2, 8, 30).timestamp() and at["kind"] == "turn"
    utc = json.loads(call(owner, "schedule", op="add", text="utc", at="2030-01-02T08:30+00:00"))
    assert utc["due"] == datetime.fromisoformat("2030-01-02T08:30+00:00").timestamp()
    daily = json.loads(
        call(member, "schedule", op="add", text="bins", cron="0 8 * * *", at="", kind="")
    )
    assert (daily["chat_key"], daily["by"], daily["cron"], daily["kind"]) == (
        GROUP,
        "sam",
        "0 8 * * *",
        "message",
    )
    with pytest.raises(PermissionError):
        call(member, "schedule", op="add", text="triage", in_minutes=1, kind="turn")
    with pytest.raises(PermissionError):  # the owner in a group: member rights
        call(ctx(services, chat=GROUP), "schedule", op="add", text="x", in_minutes=1, kind="turn")
    with pytest.raises(PermissionError):
        call(owner, "schedule", op="add", text="x", in_minutes=1, kind="later")
    with pytest.raises(ValueError):
        call(owner, "schedule", op="snooze")
    assert [t["text"] for t in json.loads(call(member, "schedule", op="list"))] == ["bins"]
    assert len(json.loads(call(owner, "schedule", op="list"))) == 3
    assert len(json.loads(call(owner, "schedule", op="list", all=True))) == 4
    with pytest.raises(KeyError):
        call(member, "schedule", op="cancel", id=soon["id"])
    assert "cancelled" in call(member, "schedule", op="cancel", id=str(daily["id"]))
    assert "cancelled" in call(owner, "schedule", op="cancel", id=soon["id"])
    assert len(json.loads(call(owner, "schedule", op="list", all=True))) == 2


def test_build_filters_by_rights(services: app.Services) -> None:
    member = ["script", "send", "db", "artifact", "schedule"]
    owner = ["read_file", "write_file", "edit_file", "run", *member]
    assert [t.name for t in tools.build(ctx(services))] == owner
    assert [t.name for t in tools.build(ctx(services, "member", GROUP))] == member
    assert [t.name for t in tools.build(ctx(services, chat=GROUP))] == member  # owner, in a group
    assert [t.name for t in tools.build(ctx(services, "member", "slack:DSAM"))] == member
    assert tools.build(ctx(services, "guest", GROUP)) == []
    for tool in tools.build(ctx(services)):
        assert tool.description.strip() and "\n" not in tool.description
        assert tool.schema["type"] == "object"
        assert set(tool.schema["required"]) <= set(tool.schema["properties"])
    schema = next(t for t in tools.build(ctx(services)) if t.name == "schedule").schema
    assert schema["required"] == ["op"] and schema["properties"]["in_minutes"] == {"type": "number"}
    assert next(t for t in tools.build(ctx(services)) if t.name == "send").schema == {
        "type": "object",
        "properties": {"text": {"type": "string"}, "files": tools.ARRAY},
        "required": ["text"],
    }
