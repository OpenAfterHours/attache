from __future__ import annotations

import asyncio
import json
import re
import shutil
import socket
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.testclient import TestClient

from attache import config, skills, web
from attache.store import Store
from attache.types import Endpoint, Inbound, Outbound, Person, Tool, Turn, TurnContext

SECRET = b"0123456789abcdef0123456789abcdef"
BASE = "http://127.0.0.1:8765"
UI = {"X-Attache-Token": web.artifact_token(SECRET, "_ui")}
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
        secret=SECRET,
        gateway=web.ToolGateway(),
        web_url=BASE,
    )
    store.close()


@pytest.fixture
def env(services: Any) -> Any:
    router, channel = FakeRouter(), web.WebChannel()
    client = TestClient(web.create_app(services, router, channel), base_url=BASE)
    return SimpleNamespace(client=client, router=router, channel=channel, sv=services)


def page(sv: Any, name: str, collections: str = "todos", head: bool = True) -> None:
    meta = f'<meta name="attache-collections" content="{collections}"><title>{name} page</title>'
    html = f"<!doctype html><html><head>{meta}</head><body>hi</body></html>"
    path = sv.config.home / "artifacts" / f"{name}.html"
    path.write_text(html if head else "<p>no head</p>", encoding="utf-8")


def auth(name: str, token_for: str | None = None) -> dict[str, str]:
    token = web.artifact_token(SECRET, token_for or name)
    return {"X-Attache-Artifact": name, "X-Attache-Token": token}


def group_page(sv: Any) -> None:
    record = {"name": "family", "collections": ["todos"], "chat_key": "slack:C1", "by": "owner"}
    sv.store.doc_put("_artifacts", "family", record)


# pages


def test_index_embeds_the_ui_token(env: Any) -> None:
    r = env.client.get("/")
    assert r.status_code == 200 and UI["X-Attache-Token"] in r.text
    assert "__ATTACHE_UI_TOKEN__" not in r.text
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert "access-control-allow-origin" not in r.headers
    assert not re.search(r"""(src|href)=["']?(https?:)?//""", r.text)  # no external requests
    assert ".innerHTML" not in r.text and "insertAdjacentHTML" not in r.text
    assert 'sandbox="allow-scripts allow-forms allow-popups"' in r.text


def test_bridge_js_is_served(env: Any) -> None:
    r = env.client.get("/bridge.js")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/javascript")
    assert "window.attache" in r.text and "X-Attache-Token" in r.text


def test_artifact_gets_the_bridge_and_a_sandbox(env: Any) -> None:
    page(env.sv, "home")
    r = env.client.get("/a/home")
    token = web.artifact_token(SECRET, "home")
    tag = f'<script src="/bridge.js" data-token="{token}" data-artifact="home"></script></head>'
    assert r.status_code == 200 and tag in r.text
    csp = r.headers["content-security-policy"]
    assert csp.startswith("sandbox allow-scripts allow-forms allow-popups;")
    assert "allow-same-origin" not in csp and "connect-src 'self'" in csp
    assert "default-src 'self' 'unsafe-inline' data: https://cdnjs.cloudflare.com" in csp


def test_artifact_without_head_gets_the_bridge_first(env: Any) -> None:
    page(env.sv, "bare", head=False)
    assert env.client.get("/a/bare").text.startswith('<script src="/bridge.js"')


@pytest.mark.parametrize("name", ["missing", "Home", "a.b", "a_b", "%2e%2e", "home.html"])
def test_bad_or_missing_artifact_names_are_404(env: Any, name: str) -> None:
    page(env.sv, "home")
    (env.sv.config.home / "artifacts" / "Home.html").write_text("x", encoding="utf-8")
    assert env.client.get(f"/a/{name}").status_code == 404


def test_artifact_list_uses_titles(env: Any) -> None:
    page(env.sv, "home")
    (env.sv.config.home / "artifacts" / "plain.html").write_text("<p>x</p>", encoding="utf-8")
    (env.sv.config.home / "artifacts" / "Not A Slug.html").write_text("x", encoding="utf-8")
    r = env.client.get("/api/artifacts", headers=UI)
    assert r.json() == [
        {"name": "home", "title": "home page", "url": "/a/home"},
        {"name": "plain", "title": "plain", "url": "/a/plain"},
    ]
    assert "access-control-allow-origin" not in r.headers
    assert env.client.get("/api/artifacts", headers=auth("home")).status_code == 403
    assert env.client.get("/api/artifacts").status_code == 401


def test_only_loopback_hosts_are_served(services: Any) -> None:
    app = web.create_app(services, FakeRouter(), web.WebChannel())
    assert TestClient(app, base_url="http://attacker.example").get("/").status_code == 400


# the data bridge


def test_db_needs_the_right_token(env: Any) -> None:
    page(env.sv, "home")
    assert env.client.get("/api/db/todos").status_code == 401
    assert env.client.get("/api/db/todos", headers=auth("home", "other")).status_code == 401
    assert env.client.get("/api/db/todos", headers=auth("../home", "../home")).status_code == 401
    r = env.client.get("/api/db/notes", headers=auth("home"))
    assert r.status_code == 403 and "notes" in r.json()["error"]


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
    assert c.put("/api/db/todos/x", headers=h, content=b"not json").status_code == 400


def test_group_page_sees_only_shared_docs_and_cannot_delete(env: Any) -> None:
    page(env.sv, "family", collections="todos,notes")  # the "_artifacts" doc wins over <meta>
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
    for path in ("/", "/api/chat", "/api/artifacts", "/api/mcp/x/tools"):
        assert (
            "access-control-allow-origin" not in env.client.options(path, headers=PREFLIGHT).headers
        )
    r = env.client.post("/api/chat", headers={**UI, "Origin": "null"}, json={"text": "hi"})
    assert r.status_code == 200 and "access-control-allow-origin" not in r.headers


# chat


def test_chat_hands_an_owner_message_to_the_router(env: Any) -> None:
    r = env.client.post("/api/chat", headers=UI, json={"text": "hello"})
    assert r.json() == {"ok": True}
    [msg] = env.router.seen
    assert (msg.channel, msg.chat_id, msg.sender_id, msg.sender_name) == (
        "web",
        "local",
        "owner",
        "Alex",
    )
    assert msg.text == "hello" and msg.chat_key == "web:local"
    page(env.sv, "home")
    assert env.client.post("/api/chat", headers=auth("home"), json={"text": "x"}).status_code == 403
    assert env.client.post("/api/chat", headers=UI, json={"text": " "}).status_code == 400
    assert env.client.post("/api/chat", json={"text": "x"}).status_code == 401
    assert len(env.router.seen) == 1


def test_pages_cannot_ask_the_agent(env: Any) -> None:
    page(env.sv, "home")
    r = env.client.post("/api/ask", headers=auth("home"), json={"prompt": "Chase Dan"})
    assert r.status_code == 404 and env.router.seen == []
    assert "/api/ask" not in env.client.get("/bridge.js").text


@pytest.mark.parametrize("body", [b"[1, 2]", b'"text"', b"42", b"null", b"not json"])
def test_bodies_must_be_json_objects(env: Any, body: bytes) -> None:
    page(env.sv, "home")
    token = env.sv.gateway.open(make_turn(env.sv))
    for path, headers in (
        ("/api/chat", UI),
        (f"/api/mcp/{token}/call", {}),
        ("/api/db/todos", auth("home")),
    ):
        r = env.client.post(path, headers=headers, content=body)
        assert r.status_code == 400 and r.json()["error"], path
    assert env.router.seen == [] and env.sv.store.doc_list("todos") == []


# the tool gateway


def make_turn(services: Any) -> Turn:
    async def echo(args: dict[str, Any], ctx: TurnContext) -> str:
        return f"echo {args['x']} for {ctx.sender.name}"

    async def boom(args: dict[str, Any], ctx: TurnContext) -> str:
        raise ValueError("bad")

    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    tools = [Tool("echo", "Echo x.", schema, echo), Tool("boom", "Fails.", {}, boom)]
    ctx = TurnContext("terminal:local", Person("owner", "Alex", "owner"), False, services)
    return Turn(ctx, Endpoint("e", "claude-cli", "m"), "", [], "hi", tools)


def test_mcp_lists_and_calls_through_the_gateway(env: Any) -> None:
    gateway, c = env.sv.gateway, env.client
    token = gateway.open(make_turn(env.sv))
    tools = c.get(f"/api/mcp/{token}/tools").json()["tools"]
    assert tools[0] == {
        "name": "echo",
        "description": "Echo x.",
        "inputSchema": tools[0]["inputSchema"],
    }
    assert [t["name"] for t in tools] == ["echo", "boom"]
    r = c.post(f"/api/mcp/{token}/call", json={"name": "echo", "arguments": {"x": "1"}}).json()
    assert r == {"content": [{"type": "text", "text": "echo 1 for Alex"}], "isError": False}
    r = c.post(f"/api/mcp/{token}/call", json={"name": "boom", "arguments": {}}).json()
    assert r["isError"] is True and "ValueError: bad" in r["content"][0]["text"]
    assert c.post(f"/api/mcp/{token}/call", json={"name": "nope"}).json()["isError"] is True
    gateway.close(token)
    assert c.get(f"/api/mcp/{token}/tools").json() == {"tools": []}
    r = c.post(f"/api/mcp/{token}/call", json={"name": "echo", "arguments": {"x": "1"}}).json()
    assert r["isError"] is True and "unknown" in r["content"][0]["text"]


def test_gateway_tokens_are_random_and_separate(services: Any) -> None:
    gateway = web.ToolGateway()
    a, b = gateway.open(make_turn(services)), gateway.open(make_turn(services))
    assert a != b and len(a) >= 32
    gateway.close(a)
    assert gateway.list(a) == [] and len(gateway.list(b)) == 2
    assert asyncio.run(gateway.call("nope", "echo", {}))[1] is True


# tokens


def test_artifact_tokens() -> None:
    token = web.artifact_token(SECRET, "home")
    assert re.fullmatch(r"[0-9a-f]{64}", token) and token == web.artifact_token(SECRET, "home")
    assert token != web.artifact_token(SECRET, "_ui")
    assert token != web.artifact_token(b"another secret", "home")


# live events


def test_events_need_a_token(env: Any) -> None:
    page(env.sv, "home")
    assert env.client.get("/api/events").status_code == 401
    assert env.client.get("/api/events?artifact=home&token=nope").status_code == 401


def test_doc_events_reach_only_pages_whose_scope_covers_them(services: Any) -> None:
    channel = web.WebChannel()
    web.create_app(services, FakeRouter(), channel)  # registers the store listener
    scopes = {
        "ui": ("_ui", None, None),
        "home": ("home", {"todos"}, None),
        "notes": ("notes", {"notes"}, None),
        "family": ("family", {"todos"}, "slack:C1"),
    }

    async def scenario() -> dict[str, list[Any]]:
        streams = {name: channel.stream(scope) for name, scope in scopes.items()}
        for stream in streams.values():
            assert await anext(stream) == ": connected\n\n"
        services.store.doc_put("todos", "p", {"title": "private"})
        thread = threading.Thread(  # the store may call listeners from any thread
            target=services.store.doc_put,
            args=("todos", "s", {"title": "x", "_shared": ["slack:C1"]}),
        )
        thread.start()
        thread.join()
        services.store.doc_delete("todos", "p")
        await channel.send(Outbound("web:local", "hello", ["files/a.txt"]))
        await asyncio.sleep(0.01)  # run the call_soon_threadsafe callbacks
        seen = {scope[0]: list(queue._queue) for scope, _, queue in channel.subs}
        assert (await anext(streams["home"])).startswith('event: doc\ndata: {"collection": "todos"')
        for stream in streams.values():
            await stream.aclose()
        assert channel.subs == []
        return seen

    seen = asyncio.run(scenario())
    events = {
        name: [(kind, e.get("id")) for kind, e in seen[slug]] for name, (slug, *_) in scopes.items()
    }
    assert events["ui"] == [("doc", "p"), ("doc", "s"), ("doc", "p"), ("message", None)]
    assert events["home"] == [("doc", "p"), ("doc", "s"), ("doc", "p")]
    assert events["notes"] == []
    assert events["family"] == [("doc", "s")]  # only the doc shared with its chat
    message = seen["_ui"][-1][1]
    assert message == {"chat_key": "web:local", "text": "hello", "files": ["files/a.txt"]}
    assert json.loads(json.dumps(seen["home"][1][1]))["doc"]["title"] == "x"


def test_stream_pings_when_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web, "PING", 0.01)
    channel = web.WebChannel()

    async def scenario() -> str:
        stream = channel.stream(("_ui", None, None))
        await anext(stream)
        ping = await anext(stream)
        await stream.aclose()
        return ping

    assert asyncio.run(scenario()) == ": ping\n\n"


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
    assert web.WebChannel.name == "web"


def test_serve_raises_oserror_when_the_port_is_taken(services: Any) -> None:
    app = web.create_app(services, FakeRouter(), web.WebChannel())
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        with pytest.raises(OSError):
            asyncio.run(web.serve(app, "127.0.0.1", taken.getsockname()[1]))
