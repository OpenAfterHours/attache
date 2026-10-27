from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import socket
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from attache import __version__, config, skills, web
from attache.store import Store
from attache.types import Endpoint, Inbound, Outbound, Person, Tool, Turn, TurnContext

UI_TOKEN = "ui-token-made-fresh-by-app-build"
BASE = "http://127.0.0.1:8765"
PUBLIC = "https://pc.tailnet.ts.net"  # [web] public_url, which Funnel serves
UI = {"X-Attache-Token": UI_TOKEN}
CSP = (
    "sandbox allow-scripts allow-forms; default-src 'self' 'unsafe-inline' data:; "
    "img-src 'self' data:; font-src 'self' data:; connect-src 'self'; form-action 'none'; "
    "base-uri 'none'; frame-ancestors 'self'"
)
SHARE_CSP = CSP.replace("frame-ancestors 'self'", "frame-ancestors 'none'")
PREFLIGHT = {
    "Origin": "null",
    "Access-Control-Request-Method": "PUT",
    "Access-Control-Request-Headers": "content-type,x-attache-artifact,x-attache-token",
}


def ptoken(name: str, ui_token: str = UI_TOKEN) -> str:
    """A page's token, which derives from the run's UI token."""
    return web.page_token(SimpleNamespace(ui_token=ui_token), name)


class FakeRouter:
    def __init__(self) -> None:
        self.seen: list[Inbound] = []
        self.asked: list[tuple[web.Scope, str]] = []

    async def handle(self, msg: Inbound) -> None:
        self.seen.append(msg)

    async def page_turn(self, scope: web.Scope, prompt: str) -> str:
        self.asked.append((scope, prompt))
        return f"reply to {scope.slug}"


class FakeChannels:
    """Stands in for ChannelManager's answer() and its open questions (`pending`, ask_id ->
    Pending): accepts "Approve" for the ask "a1" only."""

    def __init__(self) -> None:
        self.answers: list[tuple[str, str, str]] = []
        self.pending: dict[str, Any] = {}

    def answer(self, ask_id: str, person_id: str, choice: str) -> bool:
        self.answers.append((ask_id, person_id, choice))
        return ask_id == "a1" and choice == "Approve"

    def stop_turns(self, chat_key: str) -> bool:
        """Stands in for ChannelManager.stop_turns: True the first time, as if a turn ran."""
        self.stopped = [*getattr(self, "stopped", []), chat_key]
        return len(self.stopped) == 1

    async def activity(self, chat_key: str, kind: str, **data: Any) -> None:
        """Stands in for ChannelManager.activity: MCP tool calls now report their steps."""


@pytest.fixture
def services(home: Path) -> Any:
    (home / "attache.toml").write_text('[owner]\nname = "Alex"\n', encoding="utf-8")
    (home / "artifacts").mkdir()
    store = Store(home / "attache.db")
    yield SimpleNamespace(
        config=config.load(home),
        store=store,
        gateway={},
        ui_token=UI_TOKEN,
        web_url=BASE,
        channels=FakeChannels(),
        snapshots={},
    )
    store.close()


@pytest.fixture
def env(services: Any) -> Any:
    router, channel = FakeRouter(), web.WebChannel()
    channel.on_message = router.handle  # what start() keeps; ChannelManager's returns at once
    client = TestClient(web.create_app(services, channel, router), base_url=BASE)
    return SimpleNamespace(client=client, router=router, channel=channel, sv=services)


@pytest.fixture
def public(env: Any) -> Any:
    """The share links' app, as app.run serves it on [web] public_port when sharing is on."""
    env.sv.config.public_url = PUBLIC
    return TestClient(web.create_app(env.sv, env.channel, public=True), base_url=PUBLIC)


def page(sv: Any, name: str, collections: str | None = "todos", head: bool = True) -> None:
    meta = f'<meta name="attache-collections" content="{collections}">' if collections else ""
    html = f"<!doctype html><html><head>{meta}<title>{name} page</title></head><body>hi</body>"
    path = sv.config.home / "artifacts" / f"{name}.html"
    path.write_text(html if head else "<p>no head</p>", encoding="utf-8")


def auth(name: str, token_for: str | None = None) -> dict[str, str]:
    token = ptoken(token_for or name)
    return {"X-Attache-Artifact": name, "X-Attache-Token": token}


def src(name: str) -> str:
    """Where the Attaché page loads a page from, as /api/artifacts lists it."""
    return f"/a/{name}?t={ptoken(name)}"


def group_page(sv: Any, name: str = "family", collections: str = "todos") -> None:
    page(sv, name, collections)
    sv.store.doc_put("_artifacts", name, {"chat_key": "slack:C1"})


def request(headers: dict[str, str] | None = None, query: str = "") -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {"type": "http", "method": "GET", "path": "/", "headers": raw}
    return Request({**scope, "query_string": query.encode()})


# pages


def test_index_holds_no_token(env: Any) -> None:
    r = env.client.get("/")
    assert r.status_code == 200 and UI_TOKEN not in r.text
    assert "Open Attaché from the link printed in your terminal." in r.text
    assert 'params.get("t")' in r.text and 'params.get("page")' in r.text  # #t=...&page=...
    assert "if (asks.size !== 1) return null;" in r.text  # typed answers: one open question
    assert "chat.asks.forEach(showAsk)" in r.text  # open questions come back after a reload
    assert "history.replaceState" in r.text and "localStorage" in r.text
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert "access-control-allow-origin" not in r.headers
    assert not re.search(r"""(src|href)=["']?(https?:)?//""", r.text)  # no external requests
    assert ".innerHTML" not in r.text and "insertAdjacentHTML" not in r.text
    # A turn's progress: the latest step, its thinking dimmed, Stop or Esc, and a footer.
    assert 'events.addEventListener("activity"' in r.text
    assert "thinking.step.textContent = `${event.label}…`" in r.text
    assert 'api("POST", "/api/stop")' in r.text and 'event.key === "Escape"' in r.text
    assert '["stopped", time]' in r.text and "tokens`, route]" in r.text
    assert 'frame.setAttribute("sandbox", "allow-scripts allow-forms")' in r.text
    assert "new tab" not in r.text.lower() and "allow-popups" not in r.text


def test_bridge_js_is_served(env: Any) -> None:
    r = env.client.get("/bridge.js")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/javascript")
    assert "window.attache = Object.freeze({ db, on, ask })" in r.text
    assert 'if (share) throw new Error("not available on shared links");' in r.text
    assert '{ "X-Attache-Share": share }' in r.text and "`share=${enc(share)}`" in r.text


def test_artifact_gets_the_bridge_and_the_exact_csp(env: Any) -> None:
    page(env.sv, "home")
    r = env.client.get(src("home"), headers={"Sec-Fetch-Dest": "iframe"})
    token = ptoken("home")
    tag = f'<script src="/bridge.js" data-token="{token}" data-artifact="home"></script></head>'
    assert r.status_code == 200 and tag in r.text
    assert r.headers["content-security-policy"] == CSP


def test_artifact_opened_outside_a_frame_is_refused(env: Any) -> None:
    page(env.sv, "home")
    for dest in ("document", "empty", "script"):
        r = env.client.get(src("home"), headers={"Sec-Fetch-Dest": dest})
        assert r.status_code == 403 and "inside the Attaché window" in r.json()["error"]
    assert env.client.get(src("home")).status_code == 200  # no Sec-Fetch-Dest: an older client


@pytest.mark.parametrize("query", ["", "?t=", "?t=wrong", f"?t={ptoken('trip')}"])
def test_artifact_needs_its_own_token(env: Any, query: str) -> None:
    page(env.sv, "home")  # any local process could read a page's token before this check
    r = env.client.get(f"/a/home{query}", headers={"Sec-Fetch-Dest": "iframe"})
    assert r.status_code == 403 and "data-token" not in r.text


def test_artifact_without_head_gets_the_bridge_first(env: Any) -> None:
    page(env.sv, "bare", head=False)
    assert env.client.get(src("bare")).text.startswith('<script src="/bridge.js"')


@pytest.mark.parametrize("name", ["missing", "Home", "HOME", "a.b", "a_b", "%2e%2e", "home.html"])
def test_bad_or_missing_artifact_names_are_404(env: Any, name: str) -> None:
    page(env.sv, "home")  # exists, so "Home" fails on the name even on a case-insensitive disk
    assert env.client.get(f"/a/{name}?t={ptoken(name)}").status_code == 404


def test_artifact_list_gives_each_page_with_its_token(env: Any) -> None:
    page(env.sv, "trip")
    page(env.sv, "home")
    (env.sv.config.home / "artifacts" / "Not A Slug.html").write_text("x", encoding="utf-8")
    r = env.client.get("/api/artifacts", headers=UI)
    assert r.json() == [
        {"name": name, "src": src(name), "version": web._version(env.sv, name)}
        for name in ("home", "trip")
    ]
    assert env.client.get(r.json()[0]["src"]).status_code == 200
    assert "access-control-allow-origin" not in r.headers
    assert env.client.get("/api/artifacts", headers=auth("home")).status_code == 403
    assert env.client.get("/api/artifacts").status_code == 401


# the Host check


@pytest.mark.parametrize(
    ("base", "status"),
    [
        ("http://127.0.0.1:8765", 200),
        ("http://localhost:8765", 200),
        ("http://[::1]:8765", 200),  # IPv6 keeps its brackets
        ("http://[::1]", 200),
        ("http://0.0.0.0:8765", 400),
        ("http://[::]:8765", 400),
        ("http://attacker.example", 400),
        ("http://127.0.0.1.attacker.example", 400),
    ],
)
def test_only_loopback_hosts_are_served(services: Any, base: str, status: int) -> None:
    app = web.create_app(services, web.WebChannel())
    assert TestClient(app, base_url=base).get("/").status_code == status


# scopes


def test_scope_from_headers(services: Any) -> None:
    page(services, "home", "todos, _tasks")
    page(services, "plain", collections=None)
    group_page(services)
    assert web.scope_of(services, request(UI)) == web.Scope("_ui", None)
    assert web.scope_of(services, request(auth("home"))) == web.Scope(
        "home", frozenset({"todos", "_tasks"})
    )
    assert web.scope_of(services, request(auth("family"))) == web.Scope(
        "family", frozenset({"todos"}), "slack:C1"
    )
    assert web.scope_of(services, request(auth("plain"))).collections == frozenset()
    assert web.scope_of(services, request(auth("gone"))).collections == frozenset()


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-Attache-Token": "wrong"},
        {"X-Attache-Token": ptoken("home")},  # a page token is not the UI's
        {"X-Attache-Artifact": "home", "X-Attache-Token": UI_TOKEN},  # nor the reverse
        {"X-Attache-Artifact": "home", "X-Attache-Token": ptoken("other")},
        {"X-Attache-Artifact": "home", "X-Attache-Token": ptoken("home", "old run")},
        {"X-Attache-Artifact": "../home", "X-Attache-Token": ptoken("../home")},
        {"X-Attache-Artifact": "home", "X-Attache-Token": "ünicode"},
    ],
)
def test_scope_refuses_bad_tokens(services: Any, headers: dict[str, str]) -> None:
    page(services, "home")
    with pytest.raises(HTTPException) as refused:
        web.scope_of(services, request(headers))
    assert refused.value.status_code == 401


def test_scope_from_the_event_stream_query(services: Any) -> None:
    page(services, "home")
    token = ptoken("home")
    found = web.scope_of(services, request(query=f"artifact=home&token={token}"), query=True)
    assert found == web.Scope("home", frozenset({"todos"}))
    assert web.scope_of(services, request(query=f"token={UI_TOKEN}"), query=True).slug == "_ui"
    with pytest.raises(HTTPException):  # the query form ignores headers
        web.scope_of(services, request(UI), query=True)
    with pytest.raises(HTTPException):  # and the header form ignores the query
        web.scope_of(services, request(query=f"token={UI_TOKEN}"))


# the data bridge


def test_db_needs_the_right_token(env: Any) -> None:
    page(env.sv, "home")
    assert env.client.get("/api/db/todos").status_code == 401
    assert env.client.get("/api/db/todos", headers=auth("home", "other")).status_code == 401
    r = env.client.get("/api/db/notes", headers=auth("home"))
    assert r.status_code == 403 and "notes" in r.json()["error"]


def test_page_without_meta_reaches_nothing(env: Any) -> None:
    page(env.sv, "plain", collections=None)
    for collection in ("todos", "_tasks", "notes"):
        assert env.client.get(f"/api/db/{collection}", headers=auth("plain")).status_code == 403


def test_owner_page_can_create_read_update_and_delete(env: Any) -> None:
    page(env.sv, "home")
    c, h = env.client, auth("home")
    doc = c.post("/api/db/todos", headers=h, json={"title": "Milk", "status": "todo"}).json()
    assert doc["title"] == "Milk" and doc["_by"] == "web:home"
    assert c.get("/api/db/todos", headers=h).json() == [doc]
    assert c.get(f"/api/db/todos/{doc['id']}", headers=h).json() == doc
    done = c.put(f"/api/db/todos/{doc['id']}", headers=h, json={**doc, "status": "done"}).json()
    assert done["status"] == "done" and done["id"] == doc["id"]
    assert c.delete(f"/api/db/todos/{doc['id']}", headers=h).json() is True
    r = c.get(f"/api/db/todos/{doc['id']}", headers=h)
    assert r.status_code == 404 and "no document" in r.json()["error"]


def test_group_page_sees_only_shared_docs_and_cannot_delete(env: Any) -> None:
    group_page(env.sv)
    st, c, h = env.sv.store, env.client, auth("family")
    shared = st.doc_put("todos", None, {"title": "Passports", "_shared": ["slack:C1"]})
    private = st.doc_put("todos", None, {"title": "Gift for Sam"})
    assert [d["title"] for d in c.get("/api/db/todos", headers=h).json()] == ["Passports"]
    assert c.get(f"/api/db/todos/{private['id']}", headers=h).status_code == 403
    assert c.get("/api/db/notes", headers=h).status_code == 403
    added = c.post("/api/db/todos", headers=h, json={"title": "Sun cream"}).json()
    assert added["_shared"] == ["slack:C1"]
    assert c.delete(f"/api/db/todos/{shared['id']}", headers=h).status_code == 403
    assert st.doc_get("todos", shared["id"]) is not None


def test_ui_token_reaches_every_collection(env: Any) -> None:
    env.sv.store.doc_put("notes", "n1", {"text": "hi"})
    assert env.client.get("/api/db/notes/n1", headers=UI).json()["text"] == "hi"
    assert env.client.get("/api/db/_tasks", headers=UI).json() == []
    assert env.client.put("/api/db/_tasks/1", headers=UI, json={}).status_code == 403


def test_bundled_home_page_gets_its_declared_collections(env: Any) -> None:
    template = skills.BUNDLED / "artifact-kit" / "templates" / "home.html"
    shutil.copy(template, env.sv.config.home / "artifacts" / "home.html")
    for collection, status in (("todos", 200), ("_tasks", 200), ("notes", 403)):
        assert env.client.get(f"/api/db/{collection}", headers=auth("home")).status_code == status


def test_cors_only_on_bridge_routes(env: Any) -> None:
    for path in ("/api/db/todos", "/api/db/todos/1", "/api/events"):
        r = env.client.options(path, headers=PREFLIGHT)
        assert r.status_code == 200 and r.headers["access-control-allow-origin"] == "*"
        allowed = r.headers["access-control-allow-headers"].lower()
        assert {"x-attache-token", "x-attache-artifact", "content-type"} <= set(allowed.split(", "))
        methods = r.headers["access-control-allow-methods"]
        assert {"GET", "PUT", "DELETE", "POST"} <= set(methods.split(", "))
    denied = env.client.get("/api/db/todos", headers={"Origin": "null"})
    assert denied.status_code == 401 and denied.headers["access-control-allow-origin"] == "*"
    for path in ("/", "/api/chat", "/api/artifacts", "/api/messages", "/mcp/x"):
        headers = env.client.options(path, headers=PREFLIGHT).headers
        assert "access-control-allow-origin" not in headers, path
    r = env.client.post("/api/chat", headers={**UI, "Origin": "null"}, json={"text": "hi"})
    assert r.status_code == 200 and "access-control-allow-origin" not in r.headers


# chat and history


def test_chat_hands_an_owner_message_to_the_router(env: Any) -> None:
    r = env.client.post("/api/chat", headers=UI, json={"text": "hello"})
    assert r.json() == {"ok": True}
    [msg] = env.router.seen
    assert (msg.channel, msg.chat_id, msg.sender_id, msg.sender_name) == (
        "local",
        "me",
        "owner",
        "Alex",
    )
    assert msg.text == "hello" and msg.chat_key == "local:me"
    assert msg.origin == "web"  # so the reply's echo of it is not shown twice
    page(env.sv, "home")
    assert env.client.post("/api/chat", headers=auth("home"), json={"text": "x"}).status_code == 403
    assert env.client.post("/api/chat", json={"text": "x"}).status_code == 401
    assert len(env.router.seen) == 1


def test_messages_are_the_last_fifty_of_the_owner_chat(env: Any) -> None:
    st = env.sv.store
    for n in range(60):
        st.add_message("local:me", "user" if n % 2 else "assistant", f"m{n}", "owner", "Alex")
    st.add_message("local:me", "user", "Reminder: call Sam", "", "Reminder")
    st.add_message("slack:C1", "user", "elsewhere", "sam", "Sam")
    r = env.client.get("/api/messages", headers=UI)
    assert r.json()["asks"] == []
    got = r.json()["messages"]
    assert [m["text"] for m in got] == [f"m{n}" for n in range(11, 60)] + ["Reminder: call Sam"]
    assert {"role", "text", "ts", "sender_name"} <= set(got[0]) and got[0]["sender_name"] == "Alex"
    assert got[-1]["sender_name"] == "Reminder"  # the page shows these as Attaché's, not yours
    assert "access-control-allow-origin" not in r.headers
    page(env.sv, "home")
    assert env.client.get("/api/messages", headers=auth("home")).status_code == 403
    assert env.client.get("/api/messages").status_code == 401


def test_messages_bring_back_the_owners_open_questions(env: Any) -> None:
    """After a reload the page rebuilds these, buttons and all, instead of a plain copy."""

    def pending(chat_key: str, person_id: str, text: str, done: bool = False) -> Any:
        future, options = SimpleNamespace(done=lambda: done), ["Approve", "Decline"]
        return SimpleNamespace(
            chat_key=chat_key, person_id=person_id, text=text, options=options, future=future
        )

    env.sv.channels.pending = {
        "a1": pending("local:me", "owner", "Sam asked: share the packing list?"),
        "a2": pending("local:me", "owner", "Answered a moment ago", done=True),
        "a3": pending("local:me", "sam", "For Sam, not for you"),
        "a4": pending("slack:D0ALEX", "owner", "In another chat"),
    }
    asks = env.client.get("/api/messages", headers=UI).json()["asks"]
    question = {"ask_id": "a1", "text": "Sam asked: share the packing list?"}
    assert asks == [{**question, "options": ["Approve", "Decline"]}]


@pytest.mark.parametrize("body", [b"[1, 2]", b'"text"', b"42", b"null", b"not json"])
def test_bodies_must_be_json_objects(env: Any, body: bytes) -> None:
    page(env.sv, "home")
    env.sv.gateway["tok"] = make_turn(env.sv)
    json_type = {"Content-Type": "application/json"}
    targets = [("/api/chat", UI), ("/mcp/tok", {}), ("/api/db/todos", auth("home"))]
    for path, headers in [*targets, ("/api/db/todos/x", auth("home"))]:
        method = "PUT" if path.endswith("/x") else "POST"
        r = env.client.request(method, path, headers={**headers, **json_type}, content=body)
        assert r.status_code == 400 and r.json()["error"], path
    assert env.router.seen == [] and env.sv.store.doc_list("todos") == []


# MCP


def make_turn(services: Any) -> Turn:
    async def echo(args: dict[str, Any], ctx: TurnContext) -> str:
        return f"echo {args['x']} for {ctx.sender.name}"

    async def boom(args: dict[str, Any], ctx: TurnContext) -> str:
        raise ValueError("bad")

    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    tools = [Tool("echo", "Echo x.", schema, echo), Tool("boom", "Fails.", {}, boom)]
    ctx = TurnContext("local:me", Person("owner", "Alex", "owner"), False, services)
    return Turn(ctx, Endpoint("e", "claude-cli", "m"), "", [], "hi", tools)


def rpc(env: Any, method: str, params: dict[str, Any] | None = None, token: str = "tok") -> Any:
    message = {"jsonrpc": "2.0", "id": 7, "method": method, "params": params or {}}
    return env.client.post(f"/mcp/{token}", json=message)


def test_mcp_initialize_and_ping(env: Any) -> None:
    env.sv.gateway["tok"] = make_turn(env.sv)
    r = rpc(env, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {}})
    assert r.status_code == 200 and r.headers["content-type"] == "application/json"
    assert r.json() == {
        "jsonrpc": "2.0",
        "id": 7,
        "result": {
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "attache", "version": __version__},
        },
    }
    assert rpc(env, "ping").json() == {"jsonrpc": "2.0", "id": 7, "result": {}}


def test_mcp_notifications_get_202(env: Any) -> None:
    env.sv.gateway["tok"] = make_turn(env.sv)
    notification = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    r = env.client.post("/mcp/tok", json=notification)
    assert r.status_code == 202 and r.content == b""


def test_mcp_lists_and_calls_the_turns_tools(env: Any) -> None:
    env.sv.gateway["tok"] = make_turn(env.sv)
    tools = rpc(env, "tools/list").json()["result"]["tools"]
    assert [t["name"] for t in tools] == ["echo", "boom"]
    assert tools[0]["description"] == "Echo x." and tools[0]["inputSchema"]["type"] == "object"
    result = rpc(env, "tools/call", {"name": "echo", "arguments": {"x": "1"}}).json()["result"]
    assert result == {"content": [{"type": "text", "text": "echo 1 for Alex"}], "isError": False}
    result = rpc(env, "tools/call", {"name": "boom", "arguments": {}}).json()["result"]
    assert result["isError"] is True and "ValueError: bad" in result["content"][0]["text"]
    result = rpc(env, "tools/call", {"name": "nope"}).json()["result"]
    assert result["isError"] is True and "unknown tool" in result["content"][0]["text"]


def test_mcp_errors(env: Any) -> None:
    env.sv.gateway["tok"] = make_turn(env.sv)
    error = rpc(env, "resources/list").json()["error"]
    assert error["code"] == -32601 and "resources/list" in error["message"]
    assert rpc(env, "tools/list", token="gone").status_code == 404
    assert env.client.get("/mcp/tok").status_code == 405
    assert env.client.delete("/mcp/tok").status_code == 405
    del env.sv.gateway["tok"]  # the turn finished
    assert rpc(env, "tools/list").status_code == 404


def test_mcp_refuses_browsers_and_other_content(env: Any) -> None:
    env.sv.gateway["tok"] = make_turn(env.sv)
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    for origin in ("https://attacker.example", "null", "http://127.0.0.1:8765"):
        r = env.client.post("/mcp/tok", json=ping, headers={"Origin": origin})
        assert r.status_code == 403  # a web page, even a simple cross-origin POST
    for content_type in ("text/plain", "application/x-www-form-urlencoded", ""):
        r = env.client.post(
            "/mcp/tok", content=json.dumps(ping), headers={"Content-Type": content_type}
        )
        assert r.status_code == 415, content_type
    batch = env.client.post("/mcp/tok", json=[ping, ping])
    assert batch.status_code == 400 and batch.json()["error"] == "send a JSON object"
    charset = {"Content-Type": "application/json; charset=utf-8"}
    assert env.client.post("/mcp/tok", content=json.dumps(ping), headers=charset).status_code == 200


# the OpenAfterHours design system and fonts

# The :root values of OpenAfterHours/rwa_calculator docs/assets/stylesheets/tokens.css, the
# org's single source of truth, pinned here so the vendored copy cannot drift unnoticed.
UPSTREAM_TOKENS = {
    "--oah-orange": "#ff9100",
    "--oah-orange-bright": "#ffa724",
    "--oah-orange-hover": "#e68200",
    "--oah-orange-soft": "#ffb74d",
    "--oah-owl-body": "#c9442a",
    "--oah-owl-eye": "#ffc857",
    "--oah-terminal-green": "#3dd68c",
    "--oah-slate-900": "#0d0e11",
    "--oah-slate-850": "#11141a",
    "--oah-slate-800": "#161719",
    "--oah-slate-700": "#1f2430",
    "--oah-slate-600": "#2a3142",
    "--oah-slate-500": "#3b4356",
    "--oah-slate-400": "#64748b",
    "--oah-slate-300": "#a0a4ab",
    "--oah-slate-200": "#cbd0d8",
    "--oah-slate-100": "#e2e4e8",
    "--oah-slate-050": "#f1f5f9",
    "--font-sans": '"Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, '
    "Arial, sans-serif",
    "--font-display": '"Space Grotesk", "Inter", -apple-system, sans-serif',
    "--font-mono": '"JetBrains Mono", "Roboto Mono", "Fira Code", ui-monospace, SFMono-Regular, '
    "Menlo, monospace",
    "--ease-std": "cubic-bezier(0.4, 0, 0.2, 1)",
}
OAH = (web.STATIC / "oah-tokens.css").read_text(encoding="utf-8")
FONTS_CSS = (web.STATIC / "fonts.css").read_text(encoding="utf-8")
KIT = skills.BUNDLED / "artifact-kit"


def root_block(css: str) -> str:
    return re.search(r":root \{.*?\n\}", css, re.S)[0]


def test_vendored_tokens_match_upstream() -> None:
    found = re.findall(r"(--[a-z0-9-]+):\s*([^;]+);", root_block(OAH))
    assert {name: " ".join(value.split()) for name, value in found} == UPSTREAM_TOKENS
    assert "SINGLE SOURCE OF TRUTH" in OAH  # the upstream header is kept
    assert "@import url(" not in OAH and "googleapis" not in OAH  # fonts are served locally


def test_ui_and_page_kit_carry_the_tokens_and_fonts() -> None:
    faces = FONTS_CSS[FONTS_CSS.index("@font-face") :].strip()
    for path in (web.STATIC / "ui.html", KIT / "tokens.css"):
        text = path.read_text(encoding="utf-8")
        assert root_block(OAH) in text and faces in text, path.name
    urls = re.findall(r'url\("([^"]+)"\)', FONTS_CSS)
    assert urls and all(re.fullmatch(r"/fonts/[a-z0-9-]+\.woff2", url) for url in urls)


def test_bundled_fonts_are_woff2_and_small() -> None:
    fonts = sorted((web.STATIC / "fonts").glob("*.woff2"))
    assert len(fonts) == 7  # Inter 400-700, Space Grotesk, JetBrains Mono 400 and 600
    for font in fonts:
        assert re.fullmatch(r"[a-z0-9-]+\.woff2", font.name)
        assert font.read_bytes()[:4] == b"wOF2", font.name
    assert sum(font.stat().st_size for font in fonts) < 256_000
    named = {url.removeprefix("/fonts/") for url in re.findall(r'url\("([^"]+)"\)', FONTS_CSS)}
    assert named == {font.name for font in fonts}
    for family in ("Inter", "SpaceGrotesk", "JetBrainsMono"):
        assert (web.STATIC / "fonts" / f"OFL-{family}.txt").is_file()


def test_fonts_are_served_to_sandboxed_pages(env: Any) -> None:
    r = env.client.get("/fonts/inter-latin-400-normal.woff2", headers={"Origin": "null"})
    assert r.status_code == 200 and r.content[:4] == b"wOF2"
    assert r.headers["access-control-allow-origin"] == "*"
    assert "max-age=31536000" in r.headers["cache-control"]
    for name in ("OFL-Inter.txt", "Inter-latin.woff2", "missing.woff2", "x.ttf", "..%5Cui.html"):
        assert env.client.get(f"/fonts/{name}").status_code == 404, name


# tokens


def test_artifact_tokens() -> None:
    token = ptoken("home")
    assert re.fullmatch(r"[0-9a-f]{64}", token) and token == ptoken("home")
    assert token != ptoken("trip")
    assert token != ptoken("home", "the next run's ui token")  # rotates per run


# live events


def test_events_need_a_token(env: Any) -> None:
    page(env.sv, "home")
    assert env.client.get("/api/events").status_code == 401
    assert env.client.get("/api/events?artifact=home&token=nope").status_code == 401
    assert env.client.get("/api/events", headers=UI).status_code == 401  # query form only


async def drain(stream: AsyncIterator[str]) -> list[tuple[str, dict[str, Any]]]:
    """Every event the stream has ready, until it goes quiet (which also closes it)."""
    events = []
    while True:
        try:
            chunk = await asyncio.wait_for(anext(stream), 0.2)
        except TimeoutError:
            return events
        if not chunk.startswith(":"):  # comments: connected, ping
            kind, data = chunk.strip().split("\n")
            events.append((kind.removeprefix("event: "), json.loads(data.removeprefix("data: "))))


def test_events_reach_only_the_streams_whose_scope_covers_them(services: Any) -> None:
    channel = web.WebChannel()
    web.create_app(services, channel)  # registers the store listener
    scopes = {
        "ui": web.Scope("_ui", None),
        "home": web.Scope("home", frozenset({"todos"})),
        "notes": web.Scope("notes", frozenset({"notes"})),
        "family": web.Scope("family", frozenset({"todos"}), "slack:C1"),
    }

    async def scenario() -> dict[str, list[tuple[str, dict[str, Any]]]]:
        streams = {name: channel.stream(scope) for name, scope in scopes.items()}
        for stream in streams.values():
            assert await anext(stream) == ": connected\n\n"  # subscribed from here on
        services.store.doc_put("todos", "p", {"title": "private"})
        shared = {"title": "x", "_shared": ["slack:C1"]}
        thread = threading.Thread(target=services.store.doc_put, args=("todos", "s", shared))
        thread.start()  # the store may call its listeners from any thread
        thread.join()
        services.store.doc_delete("todos", "p")
        await channel.send(Outbound("local:me", "hello", ["files/a.txt"]))
        seen = {name: await drain(stream) for name, stream in streams.items()}
        assert channel.subs == []  # each closed stream unsubscribed
        return seen

    seen = asyncio.run(scenario())
    summary = {name: [(kind, e.get("id")) for kind, e in events] for name, events in seen.items()}
    assert summary["ui"] == [("doc", "p"), ("doc", "s"), ("doc", "p"), ("message", None)]
    assert summary["home"] == [("doc", "p"), ("doc", "s"), ("doc", "p")]
    assert summary["notes"] == []
    assert summary["family"] == [("doc", "s")]  # only the doc shared with its chat
    message = {"chat_key": "local:me", "text": "hello", "files": ["files/a.txt"], "author": None}
    assert message.items() <= seen["ui"][-1][1].items()
    assert seen["home"][1][1]["doc"]["title"] == "x"


def ui_events(services: Any, action: Any) -> list[tuple[str, dict[str, Any]]]:
    """The events a UI stream and a page stream get while `action(channel)` runs."""
    channel = web.WebChannel()

    async def scenario() -> list[tuple[str, dict[str, Any]]]:
        ui, page = channel.stream(web.Scope("_ui", None)), channel.stream(web.Scope("home", None))
        await anext(ui)
        await anext(page)
        await action(channel)
        assert await drain(page) == []  # messages and asks are for the Attaché page only
        return await drain(ui)

    return asyncio.run(scenario())


def test_send_skips_the_pages_own_message_and_marks_mirrored_ones(services: Any) -> None:
    async def action(channel: web.WebChannel) -> None:
        await channel.send(Outbound("local:me", "typed in this page", origin="web"))
        await channel.send(
            Outbound("local:me", "typed in the terminal", author="Alex", origin="terminal")
        )
        await channel.send(Outbound("local:me", "a reply"))

    events = ui_events(services, action)
    assert [(e["text"], e["author"], e["origin"]) for _, e in events] == [
        (
            "typed in the terminal",
            "Alex",
            "terminal",
        ),  # the page shows it as yours, "from terminal"
        ("a reply", None, ""),
    ]


def test_ask_reaches_the_page_as_an_ask_event(services: Any) -> None:
    async def action(channel: web.WebChannel) -> None:
        await channel.ask("me", "a1", "Sam asked: share the packing list?", ["Approve", "Decline"])

    question = {"ask_id": "a1", "text": "Sam asked: share the packing list?"}
    assert ui_events(services, action) == [("ask", {**question, "options": ["Approve", "Decline"]})]


def test_ask_says_whether_an_attache_page_saw_it() -> None:
    channel = web.WebChannel()

    async def scenario() -> list[bool]:
        seen = [await channel.ask("me", "a1", "Approve?", ["Yes", "No"])]  # no page open
        page = channel.stream(web.Scope("home", None))  # an artifact page shows no asks
        await anext(page)
        seen.append(await channel.ask("me", "a2", "Approve?", ["Yes", "No"]))
        ui = channel.stream(web.Scope("_ui", None))
        await anext(ui)
        seen.append(await channel.ask("me", "a3", "Approve?", ["Yes", "No"]))
        await page.aclose()
        await ui.aclose()
        return seen

    assert asyncio.run(scenario()) == [False, False, True]


def test_answer_goes_to_the_channel_manager_as_the_owner(env: Any) -> None:
    r = env.client.post("/api/answer", headers=UI, json={"ask_id": "a1", "choice": "Approve"})
    assert r.json() == {"ok": True}
    r = env.client.post("/api/answer", headers=UI, json={"ask_id": "a1", "choice": "Maybe"})
    assert r.json() == {"ok": False}  # not an option, or the ask is no longer open
    assert env.sv.channels.answers == [("a1", "owner", "Approve"), ("a1", "owner", "Maybe")]
    page(env.sv, "home")
    assert env.client.post("/api/answer", headers=auth("home"), json={}).status_code == 403
    assert env.client.post("/api/answer", json={}).status_code == 401
    bad = {**UI, "Content-Type": "application/json"}
    assert env.client.post("/api/answer", headers=bad, content=b"[1]").status_code == 400
    assert len(env.sv.channels.answers) == 2


# a turn's progress, and stopping it


def test_activity_reaches_only_the_attache_page(services: Any) -> None:
    """Steps and thinking may quote the owner's mail or files: never for pages or links."""
    channel = web.WebChannel()
    scopes = [
        web.Scope("_ui", None),
        web.Scope("home", None),  # an owner page that may read every collection
        web.Scope("family", frozenset({"todos"}), "slack:C1"),  # a chat's page, or a share link
    ]
    finished = {"seconds": 4.2, "tokens": 3100, "route": "claude", "stopped": False}

    async def scenario() -> list[list[tuple[str, dict[str, Any]]]]:
        streams = [channel.stream(scope) for scope in scopes]
        for stream in streams:
            await anext(stream)
        await channel.activity("me", "started", {"route": "claude"})
        await channel.activity("me", "step", {"label": "script mail inbox", "seconds": 1.25})
        await channel.activity("me", "thinking", {"text": "The boiler mail is newest. Then…"})
        await channel.activity("me", "finished", finished)
        return [await drain(stream) for stream in streams]

    ui, owner_page, chat_page = asyncio.run(scenario())
    assert ui == [
        ("activity", {"kind": "started", "route": "claude"}),
        ("activity", {"kind": "step", "label": "script mail inbox", "seconds": 1.25}),
        ("activity", {"kind": "thinking", "text": "The boiler mail is newest. Then…"}),
        ("activity", {"kind": "finished", **finished}),
    ]
    assert owner_page == [] and chat_page == []


def test_stop_stops_the_owners_turns_from_the_attache_page_only(
    env: Any, public: TestClient
) -> None:
    assert env.client.post("/api/stop", headers=UI).json() == {"stopped": True}
    assert env.client.post("/api/stop", headers=UI).json() == {"stopped": False}  # none left
    assert env.sv.channels.stopped == ["local:me", "local:me"]
    page(env.sv, "home")
    assert env.client.post("/api/stop", headers=auth("home")).status_code == 403
    assert env.client.post("/api/stop").status_code == 401
    assert env.client.get("/api/stop", headers=UI).status_code == 405
    _, header = share(env)
    assert public.post("/api/stop", headers=header).status_code == 404
    r = env.client.options(
        "/api/stop", headers={**PREFLIGHT, "Access-Control-Request-Method": "POST"}
    )
    assert "access-control-allow-origin" not in r.headers  # no CORS: pages can't call it
    assert env.sv.channels.stopped == ["local:me", "local:me"]


def test_snapshot_url_lets_the_render_browser_open_the_page(env: Any) -> None:
    page(env.sv, "home")
    url = web.snapshot_url(env.sv, "home")
    match = re.fullmatch(rf"{BASE}/a/home\?t={ptoken('home')}&r=([\w-]{{20,}})", url)
    assert match, url
    [(render, (name, until))] = env.sv.snapshots.items()  # in memory, for this run only
    assert render == match[1] and name == "home" and 590 < until - time.time() <= 600
    top = {"Sec-Fetch-Dest": "document"}
    r = env.client.get(url.removeprefix(BASE), headers=top)
    assert r.status_code == 200 and 'data-artifact="home"' in r.text
    assert r.headers["content-security-policy"] == CSP  # still sandboxed when top-level
    assert web.snapshot_url(env.sv, "home") != url  # a new token each time
    page(env.sv, "trip")
    trip = web.snapshot_url(env.sv, "trip").partition("&r=")[2]
    for query in (
        f"?t={ptoken('home')}",  # no r: top-level stays refused
        f"?t={ptoken('home')}&r=wrong",
        f"?t={ptoken('home')}&r={trip}",  # another page's
        f"?t={ptoken('home')}&r={ptoken('render:home')}",  # the old HMAC form
        f"?t=wrong&r={render}",
    ):
        assert env.client.get(f"/a/home{query}", headers=top).status_code == 403, query
    assert env.client.get(src("home"), headers={"Sec-Fetch-Dest": "iframe"}).status_code == 200


def test_snapshot_links_last_ten_minutes(env: Any) -> None:
    page(env.sv, "home")
    url = web.snapshot_url(env.sv, "home").removeprefix(BASE)
    [render] = env.sv.snapshots
    env.sv.snapshots[render] = ("home", time.time() - 1)  # as if ten minutes had passed
    r = env.client.get(url, headers={"Sec-Fetch-Dest": "document"})
    assert r.status_code == 403 and "inside the Attaché window" in r.json()["error"]


# attache.ask


def test_ask_runs_a_page_turn_for_the_page_that_asks(env: Any) -> None:
    group_page(env.sv)
    r = env.client.post("/api/ask", headers=auth("family"), json={"prompt": "What's left?"})
    assert r.status_code == 200 and r.json() == {"reply": "reply to family"}
    assert env.router.asked == [
        (web.Scope("family", frozenset({"todos"}), "slack:C1"), "What's left?")
    ]
    env.client.post("/api/ask", headers=auth("family"), json={"prompt": "x" * 5000})
    assert env.router.asked[-1][1] == "x" * 2000  # capped
    assert env.router.seen == []  # not a chat message


def test_ask_takes_a_page_token_only(env: Any, public: TestClient) -> None:
    group_page(env.sv)
    link = web.share_url(env.sv, "family").rpartition("/")[2]
    ask = {"prompt": "hi"}
    assert env.client.post("/api/ask", headers=UI, json=ask).status_code == 403  # the UI's alone
    assert env.client.post("/api/ask", json=ask).status_code == 401
    assert (
        env.client.post("/api/ask", headers={"X-Attache-Share": link}, json=ask).status_code == 401
    )
    assert env.client.post("/api/ask", headers=auth("family", "other"), json=ask).status_code == 401
    assert public.post("/api/ask", headers={"X-Attache-Share": link}, json=ask).status_code == 404
    assert env.router.asked == []
    r = env.client.options(
        "/api/ask", headers={**PREFLIGHT, "Access-Control-Request-Method": "POST"}
    )
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == "*"  # from frames


# share links


def test_share_url_stores_only_a_hash_of_its_token(env: Any) -> None:
    env.sv.config.public_url = PUBLIC
    group_page(env.sv, collections="todos, trip")
    url = web.share_url(env.sv, "family")
    token = re.fullmatch(rf"{PUBLIC}/s/([\w-]{{32}})", url)[1]
    [share] = env.sv.store.doc_list("_shares")
    assert share["id"] == hashlib.sha256(token.encode()).hexdigest() and token not in str(share)
    version = hashlib.sha256((env.sv.config.home / "artifacts" / "family.html").read_bytes())
    assert share["name"] == "family" and share["chat_key"] == "slack:C1"
    assert (
        share["collections"] == ["todos", "trip"] and share["version"] == version.hexdigest()[:12]
    )
    assert 7 * 86400 - 10 < share["until"] - time.time() <= 7 * 86400
    assert web.share_url(env.sv, "family") != url  # a new link each time


def test_owner_pages_cannot_be_shared(env: Any) -> None:
    page(env.sv, "home")  # no "_artifacts" record: the owner's own page
    with pytest.raises(PermissionError):
        web.share_url(env.sv, "home")
    assert env.sv.store.doc_list("_shares") == []


def share(env: Any, collections: str = "todos") -> tuple[str, dict[str, str]]:
    """A group page "family" shared: its link's token, and the bridge's header for it."""
    env.sv.config.public_url = PUBLIC
    group_page(env.sv, collections=collections)
    token = web.share_url(env.sv, "family").rpartition("/")[2]
    return token, {"X-Attache-Share": token}


def test_share_link_serves_the_page_top_level(env: Any, public: TestClient) -> None:
    token, _ = share(env)
    r = public.get(f"/s/{token}", headers={"Sec-Fetch-Dest": "document"})
    tag = f'<script src="/bridge.js" data-share="{token}"></script></head>'
    assert r.status_code == 200 and tag in r.text and "data-token" not in r.text
    assert r.headers["content-security-policy"] == SHARE_CSP  # sandboxed, and never framed
    assert r.headers["referrer-policy"] == "no-referrer"  # the link stays out of Referer
    assert "access-control-allow-origin" not in r.headers


def test_share_link_dies_when_it_expires_or_the_page_changes(env: Any, public: TestClient) -> None:
    token, _ = share(env)
    assert public.get("/s/").status_code == 404
    for wrong in ("not-a-token", f"{token}x", token.upper()):
        assert public.get(f"/s/{wrong}").status_code == 410, wrong
    page(env.sv, "family", "todos, notes")  # the page changed
    r = public.get(f"/s/{token}")
    assert r.status_code == 410 and "ask for a new one" in r.text
    token, _ = share(env)
    (env.sv.config.home / "artifacts" / "family.html").unlink()  # the page is gone
    assert public.get(f"/s/{token}").status_code == 410
    token, _ = share(env)
    assert public.get(f"/s/{token}").status_code == 200
    key = hashlib.sha256(token.encode()).hexdigest()
    record = env.sv.store.doc_get("_shares", key)
    env.sv.store.doc_put("_shares", key, {**record, "until": time.time() - 1})
    assert public.get(f"/s/{token}").status_code == 410  # a week has passed


def test_share_link_gets_the_chats_member_access_only(env: Any, public: TestClient) -> None:
    _, header = share(env)
    st = env.sv.store
    shared = st.doc_put("todos", None, {"title": "Passports", "_shared": ["slack:C1"]})
    private = st.doc_put("todos", None, {"title": "Gift for Sam"})
    st.doc_put("notes", None, {"text": "private", "_shared": ["slack:C1"]})
    assert [d["title"] for d in public.get("/api/db/todos", headers=header).json()] == ["Passports"]
    assert public.get(f"/api/db/todos/{private['id']}", headers=header).status_code == 403
    assert public.get("/api/db/notes", headers=header).status_code == 403  # not declared
    added = public.post("/api/db/todos", headers=header, json={"title": "Sun cream"}).json()
    assert added["_shared"] == ["slack:C1"] and added["_by"] == "web:family"
    assert public.delete(f"/api/db/todos/{shared['id']}", headers=header).status_code == 403
    page(env.sv, "family", "todos, notes")  # editing the page doesn't widen an old link
    assert public.get("/api/db/notes", headers=header).status_code == 410
    found = web.scope_of(env.sv, request(query=f"share={share(env)[0]}"), query=True, public=True)
    assert found == web.Scope("family", frozenset({"todos"}), "slack:C1")  # the event stream


def test_public_app_takes_share_tokens_only(env: Any, public: TestClient) -> None:
    token, header = share(env)
    page(env.sv, "home")
    for headers in (UI, auth("home"), auth("family"), {}):
        assert public.get("/api/db/todos", headers=headers).status_code == 410, headers
    with pytest.raises(HTTPException):
        web.scope_of(env.sv, request(query=f"token={UI_TOKEN}"), query=True, public=True)
    assert env.client.get("/api/db/todos", headers=header).status_code == 401  # nor the reverse
    assert env.client.get(f"/s/{token}").status_code == 404  # the local app has no share links


def test_public_app_has_only_the_share_routes(env: Any, public: TestClient) -> None:
    _, header = share(env)
    page(env.sv, "home")
    env.sv.gateway["tok"] = make_turn(env.sv)
    for path in ("/", "/api/artifacts", "/api/messages", src("home"), "/api/x"):
        assert public.get(path, headers={**UI, **header}).status_code == 404, path
    for path in ("/api/chat", "/api/answer", "/api/ask", "/mcp/tok"):
        assert public.post(path, headers={**UI, **header}, json={}).status_code == 404, path
    assert public.get("/bridge.js").status_code == 200
    assert public.get("/fonts/inter-latin-400-normal.woff2").status_code == 200
    r = public.options(
        "/api/db/todos", headers={**PREFLIGHT, "Access-Control-Request-Headers": "x-attache-share"}
    )
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == "*"
    assert env.router.seen == [] and env.sv.channels.answers == []


@pytest.mark.parametrize(
    ("base", "status"),
    [
        (PUBLIC, 200),
        ("https://PC.tailnet.ts.net", 200),
        ("http://127.0.0.1:8766", 200),  # Funnel may pass loopback's Host on
        ("http://localhost:8766", 200),
        ("https://other.tailnet.ts.net", 400),  # DNS rebinding names stay refused
    ],
)
def test_public_app_serves_its_host_and_loopback(env: Any, base: str, status: int) -> None:
    env.sv.config.public_url = PUBLIC
    app = web.create_app(env.sv, env.channel, public=True)
    assert TestClient(app, base_url=base).get("/bridge.js").status_code == status


def test_one_store_listener_feeds_both_apps(env: Any, public: TestClient) -> None:
    """The public app shares the local app's channel, so it must not add a second listener
    (each change would reach every page twice)."""
    assert len(env.sv.store.on_change) == 1


# what a share link may write: anyone who has it can, so it is capped (the local app is not)


def test_share_link_bodies_are_at_most_64_kb(env: Any, public: TestClient) -> None:
    _, header = share(env)
    big = public.post("/api/db/todos", headers=header, json={"title": "x" * 5_000_000})
    assert big.status_code == 413 and "64 KB" in big.text
    edge = public.post("/api/db/todos", headers=header, json={"title": "x" * 65_000})
    assert edge.status_code == 200
    assert [len(d["title"]) for d in env.sv.store.doc_list("todos")] == [65_000]
    r = env.client.post("/api/db/todos", headers=auth("family"), json={"title": "x" * 100_000})
    assert r.status_code == 200  # a page in the Attaché window isn't capped


def test_share_link_refuses_a_body_of_unknown_size(env: Any, public: TestClient) -> None:
    """A chunked body has no Content-Length, so its size is known only once it is all read."""
    _, header = share(env)
    sent: list[int] = []

    def chunks() -> Any:
        for n in range(1000):  # 4 MB, in 4 KB chunks
            sent.append(n)
            yield b"x" * 4096

    r = public.post("/api/db/todos", headers=header, content=chunks())
    assert r.status_code == 413 and "Content-Length" in r.text
    assert sent == []  # refused before a single chunk was read
    for length in ("", "ten", "-1", str(64 * 1024 + 1)):
        headers = {**header, "Content-Type": "application/json", "Content-Length": length}
        assert public.post("/api/db/todos", headers=headers, content=b"{}").status_code == 413
    assert env.sv.store.doc_list("todos") == []
    r = env.client.post("/api/db/todos", headers=auth("family"), content=iter([b'{"a": 1}']))
    assert r.status_code == 200  # the Attaché window takes chunks


@pytest.mark.parametrize("framing", ["", "Content-Length: 10\r\n"])
def test_share_link_answers_a_chunked_body_before_reading_it(env: Any, framing: str) -> None:
    """Over a real socket: only the headers are sent, and the 413 comes at once. Chunks win
    over a Content-Length, so a small one beside them changes nothing."""
    token, _ = share(env)
    app = web.create_app(env.sv, env.channel, public=True)

    async def scenario(sock: socket.socket) -> bytes:
        server = asyncio.create_task(web.serve(app, sock))
        for _ in range(100):  # until uvicorn is listening
            try:
                reader, writer = await asyncio.open_connection(*sock.getsockname()[:2])
                break
            except OSError:
                await asyncio.sleep(0.05)
        head = f"POST /api/db/todos HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Attache-Share: {token}\r\n"
        writer.write(f"{head}Transfer-Encoding: chunked\r\n{framing}\r\n".encode())
        reply = await asyncio.wait_for(reader.readuntil(b"\r\n"), 2)  # no body sent yet
        writer.close()
        server.cancel()
        with pytest.raises(asyncio.CancelledError):
            await server
        return reply

    with socket.create_server(("127.0.0.1", 0)) as sock:
        assert asyncio.run(scenario(sock)).startswith(b"HTTP/1.1 413")
    assert env.sv.store.doc_list("todos") == []


def test_share_link_writes_at_most_60_an_hour(
    env: Any, public: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    token, header = share(env)
    codes = [
        public.post("/api/db/todos", headers=header, json={"n": n}).status_code for n in range(61)
    ]
    assert codes == [200] * 60 + [429] and len(env.sv.store.doc_list("todos")) == 60
    assert "60 changes this hour" in public.post("/api/db/todos", headers=header, json={}).text
    assert public.get("/api/db/todos", headers=header).status_code == 200  # reads go on
    other = {"X-Attache-Share": share(env)[0]}  # another link has its own count
    assert public.post("/api/db/todos", headers=other, json={"n": 61}).status_code == 200
    for n in range(70):  # the Attaché window has no such limit
        assert env.client.post("/api/db/todos", headers=auth("family"), json={"n": n}).is_success
    later = time.time() + 3600
    monkeypatch.setattr(web, "time", SimpleNamespace(time=lambda: later))  # the next hour
    assert public.post("/api/db/todos", headers=header, json={"n": 62}).status_code == 200
    assert token not in str(env.sv.store.doc_list("todos"))


def test_share_link_adds_no_doc_past_500(env: Any, public: TestClient) -> None:
    _, header = share(env)
    st = env.sv.store
    for n in range(499):
        st.doc_put("todos", f"d{n}", {"n": n, "_shared": ["slack:C1"]})
    for n in range(20):
        st.doc_put("todos", f"p{n}", {"n": n})  # not the chat's: these don't count
    assert public.post("/api/db/todos", headers=header, json={"n": 499}).status_code == 200
    full = public.post("/api/db/todos", headers=header, json={"n": 500})
    assert full.status_code == 403 and "500" in full.json()["error"]
    assert public.put("/api/db/todos/new-id", headers=header, json={}).status_code == 403
    assert public.put("/api/db/todos/d1", headers=header, json={"n": 1, "done": True}).is_success
    assert len(st.doc_list("todos", shared_with="slack:C1")) == 500
    r = env.client.post("/api/db/todos", headers=auth("family"), json={"n": 501})
    assert r.status_code == 200  # the Attaché window isn't capped


# collections no page reaches


@pytest.mark.parametrize("chat_key", [None, "slack:C1"])
def test_no_page_reaches_shares_or_requests(env: Any, chat_key: str | None) -> None:
    st = env.sv.store
    st.doc_put(
        "_requests", "r1", {"summary": "Sam asks to change a skill", "_shared": ["slack:C1"]}
    )
    st.doc_put("_shares", "h1", {"name": "trip", "_shared": ["slack:C1"]})
    page(env.sv, "spy", "todos, _shares, _requests")
    if chat_key:
        st.doc_put("_artifacts", "spy", {"chat_key": chat_key})
    scope = web.page_scope(env.sv, "spy")
    assert scope.collections == frozenset({"todos"})
    for collection in ("_shares", "_requests"):
        r = env.client.get(f"/api/db/{collection}", headers=auth("spy"))
        assert r.status_code == 403 and collection in r.json()["error"]
        assert env.client.get(f"/api/db/{collection}/h1", headers=auth("spy")).status_code == 403
    assert env.client.get("/api/db/_requests", headers=UI).json()[0]["id"] == "r1"  # the owner's


def test_share_links_never_reach_shares_or_requests(env: Any, public: TestClient) -> None:
    _, header = share(env, collections="todos, _shares, _requests")
    [record] = env.sv.store.doc_list("_shares")
    assert record["collections"] == ["todos"]
    for collection in ("_shares", "_requests"):
        assert public.get(f"/api/db/{collection}", headers=header).status_code == 403


def test_snapshot_url_drops_expired_snapshots(env: Any) -> None:
    page(env.sv, "home")
    env.sv.snapshots.update({"old": ("home", time.time() - 1), "live": ("home", time.time() + 60)})
    web.snapshot_url(env.sv, "home")
    assert "old" not in env.sv.snapshots and "live" in env.sv.snapshots
    assert len(env.sv.snapshots) == 2


def test_stream_pings_when_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web, "PING_SECONDS", 0.01)
    channel = web.WebChannel()

    async def scenario() -> str:
        stream = channel.stream(web.Scope("_ui", None))
        await anext(stream)
        ping = await anext(stream)
        await stream.aclose()
        return ping

    assert asyncio.run(scenario()) == ": ping\n\n"
    assert channel.subs == []


def test_web_channel_runs_until_stopped() -> None:
    channel = web.WebChannel()

    async def scenario() -> None:
        async def on_message(msg: Inbound) -> None: ...

        task = asyncio.create_task(channel.start(on_message))
        await asyncio.sleep(0)
        assert not task.done() and channel.on_message is on_message  # kept for /api/chat
        await channel.stop()
        await asyncio.wait_for(task, 1)

    asyncio.run(scenario())
    assert web.WebChannel.name == "local"


def test_stop_ends_every_open_stream() -> None:
    """So stopping with a page open doesn't leave uvicorn a stream to cancel (and log)."""
    channel = web.WebChannel()

    async def scenario() -> list[list[str]]:
        streams = [channel.stream(web.Scope("_ui", None)), channel.stream(web.Scope("home", None))]
        for stream in streams:
            await anext(stream)
        waiting = [asyncio.create_task(anext(stream)) for stream in streams]  # like a live page
        await asyncio.sleep(0.05)
        await channel.stop()
        ends = [
            [await task] + [chunk async for chunk in stream]
            for task, stream in zip(waiting, streams, strict=True)
        ]
        late = channel.stream(web.Scope("_ui", None))
        ends.append([chunk async for chunk in late])  # one opened after stop ends straight away
        return ends

    ends = asyncio.run(asyncio.wait_for(scenario(), 2))
    assert ends == [["event: bye\ndata: {}\n\n"], ["event: bye\ndata: {}\n\n"], [": connected\n\n"]]
    assert channel.subs == []


# serving


def test_serve_answers_on_the_socket_it_is_given(services: Any) -> None:
    app = web.create_app(services, web.WebChannel())

    async def scenario(sock: socket.socket) -> bytes:
        server = asyncio.create_task(web.serve(app, sock))
        port = sock.getsockname()[1]
        for _ in range(100):  # until uvicorn is listening
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                break
            except OSError:
                await asyncio.sleep(0.05)
        writer.write(b"GET /bridge.js HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
        reply = await reader.read()
        writer.close()
        server.cancel()
        with pytest.raises(asyncio.CancelledError):
            await server
        return reply

    with socket.create_server(("127.0.0.1", 0)) as sock:
        reply = asyncio.run(scenario(sock))
    assert reply.startswith(b"HTTP/1.1 200") and b"window.attache" in reply


def test_serve_lets_running_requests_finish_when_cancelled() -> None:
    """A turn started by /api/chat runs in the request; cancelling serve waits for it (up to
    about 2 s, then it is cancelled), so app.run can close the store after."""

    async def slow(request: Request) -> Any:
        await asyncio.sleep(float(request.query_params["s"]))
        return PlainTextResponse("finished")

    app = Starlette(routes=[Route("/slow", slow)])

    async def ask_then_cancel(sock: socket.socket, seconds: float) -> tuple[bytes, float]:
        server = asyncio.create_task(web.serve(app, sock))
        for _ in range(100):  # until uvicorn is listening
            try:
                reader, writer = await asyncio.open_connection(*sock.getsockname()[:2])
                break
            except OSError:
                await asyncio.sleep(0.05)
        writer.write(f"GET /slow?s={seconds} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        await asyncio.sleep(0.2)  # the request is running
        start = time.monotonic()
        server.cancel()
        with pytest.raises(asyncio.CancelledError):
            await server
        elapsed = time.monotonic() - start
        reply = await asyncio.wait_for(reader.read(), 1)
        writer.close()
        return reply, elapsed

    with socket.create_server(("127.0.0.1", 0)) as sock:
        reply, elapsed = asyncio.run(ask_then_cancel(sock, 0.5))
    assert b"200 OK" in reply and reply.endswith(b"finished") and elapsed < 2
    with socket.create_server(("127.0.0.1", 0)) as sock:
        reply, elapsed = asyncio.run(ask_then_cancel(sock, 30))
    assert b"finished" not in reply and 1.5 < elapsed < 4  # bounded: cancelled after about 2 s


def test_stopping_with_a_page_open_shuts_down_at_once(
    services: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """The channels stop before the server (app.run's order): the open event stream ends, so
    uvicorn has no task to cancel, and logs no "Cancel 1 running task(s)" with a traceback."""
    channel = web.WebChannel()
    app = web.create_app(services, channel)

    async def scenario(sock: socket.socket) -> tuple[bytes, float]:
        server = asyncio.create_task(web.serve(app, sock))
        for _ in range(100):
            try:
                reader, writer = await asyncio.open_connection(*sock.getsockname()[:2])
                break
            except OSError:
                await asyncio.sleep(0.05)
        writer.write(
            f"GET /api/events?token={UI_TOKEN} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode()
        )
        await reader.readuntil(b": connected\n\n")  # the page is connected
        start = time.monotonic()
        await channel.stop()
        server.cancel()
        with pytest.raises(asyncio.CancelledError):
            await server
        rest = await asyncio.wait_for(reader.read(), 1)
        writer.close()
        return rest, time.monotonic() - start

    with socket.create_server(("127.0.0.1", 0)) as sock:
        rest, elapsed = asyncio.run(scenario(sock))
    assert b"event: bye" in rest and elapsed < 1
    assert "Cancel" not in caplog.text and "Traceback" not in caplog.text
