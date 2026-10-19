from __future__ import annotations

import asyncio
import json
import re
import shutil
import socket
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.testclient import TestClient

from attache import __version__, config, skills, web
from attache.store import Store
from attache.types import Endpoint, Inbound, Outbound, Person, Tool, Turn, TurnContext

UI_TOKEN = "ui-token-made-fresh-by-app-build"
KEY = UI_TOKEN.encode()  # page tokens derive from the run's UI token
BASE = "http://127.0.0.1:8765"
UI = {"X-Attache-Token": UI_TOKEN}
CSP = (
    "sandbox allow-scripts allow-forms; default-src 'self' 'unsafe-inline' data:; "
    "img-src 'self' data:; font-src 'self' data:; connect-src 'self'; form-action 'none'; "
    "base-uri 'none'; frame-ancestors 'self'"
)
PREFLIGHT = {
    "Origin": "null",
    "Access-Control-Request-Method": "PUT",
    "Access-Control-Request-Headers": "content-type,x-attache-artifact,x-attache-token",
}


class FakeRouter:
    def __init__(self) -> None:
        self.seen: list[Inbound] = []

    async def handle(self, msg: Inbound) -> None:
        self.seen.append(msg)


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
    )
    store.close()


@pytest.fixture
def env(services: Any) -> Any:
    router, channel = FakeRouter(), web.WebChannel()
    client = TestClient(web.create_app(services, router, channel), base_url=BASE)
    return SimpleNamespace(client=client, router=router, channel=channel, sv=services)


def page(sv: Any, name: str, collections: str | None = "todos", head: bool = True) -> None:
    meta = f'<meta name="attache-collections" content="{collections}">' if collections else ""
    html = f"<!doctype html><html><head>{meta}<title>{name} page</title></head><body>hi</body>"
    path = sv.config.home / "artifacts" / f"{name}.html"
    path.write_text(html if head else "<p>no head</p>", encoding="utf-8")


def auth(name: str, token_for: str | None = None) -> dict[str, str]:
    token = web.artifact_token(KEY, token_for or name)
    return {"X-Attache-Artifact": name, "X-Attache-Token": token}


def src(name: str) -> str:
    """Where the Attaché page loads a page from, as /api/artifacts lists it."""
    return f"/a/{name}?t={web.artifact_token(KEY, name)}"


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
    assert "history.replaceState" in r.text and "localStorage" in r.text
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert "access-control-allow-origin" not in r.headers
    assert not re.search(r"""(src|href)=["']?(https?:)?//""", r.text)  # no external requests
    assert ".innerHTML" not in r.text and "insertAdjacentHTML" not in r.text
    assert 'sandbox="allow-scripts allow-forms"' in r.text
    assert "new tab" not in r.text.lower() and "allow-popups" not in r.text


def test_bridge_js_is_served(env: Any) -> None:
    r = env.client.get("/bridge.js")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/javascript")
    assert "window.attache = Object.freeze({ db, on })" in r.text


def test_artifact_gets_the_bridge_and_the_exact_csp(env: Any) -> None:
    page(env.sv, "home")
    r = env.client.get(src("home"), headers={"Sec-Fetch-Dest": "iframe"})
    token = web.artifact_token(KEY, "home")
    tag = f'<script src="/bridge.js" data-token="{token}" data-artifact="home"></script></head>'
    assert r.status_code == 200 and tag in r.text
    assert r.headers["content-security-policy"] == CSP


def test_artifact_opened_outside_a_frame_is_refused(env: Any) -> None:
    page(env.sv, "home")
    for dest in ("document", "empty", "script"):
        r = env.client.get(src("home"), headers={"Sec-Fetch-Dest": dest})
        assert r.status_code == 403 and "inside the Attaché window" in r.json()["error"]
    assert env.client.get(src("home")).status_code == 200  # no Sec-Fetch-Dest: an older client


@pytest.mark.parametrize("query", ["", "?t=", "?t=wrong", f"?t={web.artifact_token(KEY, 'trip')}"])
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
    assert env.client.get(f"/a/{name}?t={web.artifact_token(KEY, name)}").status_code == 404


def test_artifact_list_gives_each_page_with_its_token(env: Any) -> None:
    page(env.sv, "trip")
    page(env.sv, "home")
    (env.sv.config.home / "artifacts" / "Not A Slug.html").write_text("x", encoding="utf-8")
    r = env.client.get("/api/artifacts", headers=UI)
    assert r.json() == [{"name": "home", "src": src("home")}, {"name": "trip", "src": src("trip")}]
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
    app = web.create_app(services, FakeRouter(), web.WebChannel())
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
        {"X-Attache-Token": web.artifact_token(KEY, "home")},  # a page token is not the UI's
        {"X-Attache-Artifact": "home", "X-Attache-Token": UI_TOKEN},  # nor the reverse
        {"X-Attache-Artifact": "home", "X-Attache-Token": web.artifact_token(KEY, "other")},
        {"X-Attache-Artifact": "home", "X-Attache-Token": web.artifact_token(b"old run", "home")},
        {"X-Attache-Artifact": "../home", "X-Attache-Token": web.artifact_token(KEY, "../home")},
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
    token = web.artifact_token(KEY, "home")
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
    got = r.json()
    assert [m["text"] for m in got] == [f"m{n}" for n in range(11, 60)] + ["Reminder: call Sam"]
    assert set(got[0]) == {"role", "text", "ts", "sender_name"} and got[0]["sender_name"] == "Alex"
    assert got[-1]["sender_name"] == "Reminder"  # the page shows these as Attaché's, not yours
    assert "access-control-allow-origin" not in r.headers
    page(env.sv, "home")
    assert env.client.get("/api/messages", headers=auth("home")).status_code == 403
    assert env.client.get("/api/messages").status_code == 401


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


# tokens


def test_artifact_tokens() -> None:
    token = web.artifact_token(KEY, "home")
    assert re.fullmatch(r"[0-9a-f]{64}", token) and token == web.artifact_token(KEY, "home")
    assert token != web.artifact_token(KEY, "trip")
    assert token != web.artifact_token(b"the next run's ui token", "home")  # rotates per run


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
    web.create_app(services, FakeRouter(), channel)  # registers the store listener
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
    assert seen["ui"][-1][1] == {"chat_key": "local:me", "text": "hello", "files": ["files/a.txt"]}
    assert seen["home"][1][1]["doc"]["title"] == "x"


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
        assert not task.done()
        await channel.stop()
        await asyncio.wait_for(task, 1)

    asyncio.run(scenario())
    assert web.WebChannel.name == "local"


# serving


def test_serve_answers_on_the_socket_it_is_given(services: Any) -> None:
    app = web.create_app(services, FakeRouter(), web.WebChannel())

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
