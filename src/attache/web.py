"""The local web server: chat page, artifacts, the data bridge, live events and MCP. Owner: web.

Routes
  GET  /                       static/ui.html. It holds no token: the terminal prints
                               http://127.0.0.1:<port>/#t=<services.ui_token> and the page keeps it.
  GET  /bridge.js              static/bridge.js
  GET  /a/{name}               artifacts/<name>.html (name [a-z0-9-]+) with the bridge script
                               injected and CSP; 403 when Sec-Fetch-Dest says it is not a frame
  GET  /api/artifacts          the page names, sorted                                UI token
  GET  /api/messages           the last 50 messages of local:me: [{role, text, ts}]  UI token
  POST /api/chat {text}        an owner Inbound in local:me; the reply arrives over SSE UI token
  GET|POST /api/db/{c}         list | create   } the UI token or a page's token, under the db
  GET|PUT|DELETE /api/db/{c}/{id}             } tool's rules (store.db_access)
  GET  /api/events             SSE with ?token= (the UI) or ?artifact=&token= (a page):
                               "message" {chat_key, text, files} for the UI only, "doc"
                               {collection, id, doc} within the scope, a ping every 15 s
  POST /mcp/{token}            MCP over Streamable HTTP (plain JSON responses) for the tools of
                               the turn services.gateway[token]; Claude Code connects here

Security
  Artifacts are written by the agent and may quote emails or web pages, so they are untrusted.
  The CSP sandbox without allow-same-origin gives each page an opaque origin: it cannot read the
  Attaché page or its storage, and reaches the server only through the bridge routes, which
  send CORS headers for that "null" origin (tokens travel in headers, never cookies). Other
  routes send none. The CSP also blocks every external resource, pop-ups, form posts and
  framing by other sites; pages open only inside the Attaché window. A page sends the headers
  X-Attache-Artifact: <slug> and X-Attache-Token: artifact_token(secret, slug). Its collections
  come only from its <meta name="attache-collections" content="a,b">. Pages published by a member
  or in a group have an "_artifacts" record {chat_key}, and see only docs shared with that chat.
  Only loopback Host headers are served (against DNS rebinding); never a wildcard address.
  Bad token 401, refused 403, missing 404, bad JSON 400.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import re
import socket
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

import uvicorn
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from attache import __version__
from attache.agent import run_tool
from attache.store import _shared, db_access
from attache.types import Inbound, OnMessage, Outbound, Tool, ToolCall

if TYPE_CHECKING:
    from attache.app import Services
    from attache.router import Router

UI = "_ui"  # the scope slug of the Attaché page itself
PING_SECONDS = 15
STATIC = Path(__file__).parent / "static"
SLUG = re.compile(r"[a-z0-9-]+")
LOOPBACK = ["127.0.0.1", "localhost", "[::1]"]  # Starlette keeps the brackets of IPv6 hosts
META = re.compile(r"<meta\b(?=[^>]*name=.attache-collections.)[^>]*content=[\"']([^\"']*)", re.I)
TAG = '<script src="/bridge.js" data-token="{}" data-artifact="{}"></script>'
CSP = (
    "sandbox allow-scripts allow-forms; default-src 'self' 'unsafe-inline' data:; "
    "img-src 'self' data:; font-src 'self' data:; connect-src 'self'; form-action 'none'; "
    "base-uri 'none'; frame-ancestors 'self'"
)
UI_CSP = "default-src 'self' 'unsafe-inline' data:; frame-ancestors 'none'; base-uri 'none'"
ERRORS = {PermissionError: 403, KeyError: 404, ValueError: 400}
SERVER = {"capabilities": {"tools": {}}, "serverInfo": {"name": "attache", "version": __version__}}


def artifact_token(secret: bytes, name: str) -> str:
    """The token that lets the page `name` use the bridge."""
    return hmac.new(secret, f"attache:{name}".encode(), "sha256").hexdigest()


class Scope(NamedTuple):
    """Who is calling: the Attaché page (slug "_ui", every collection) or one artifact page."""

    slug: str
    collections: frozenset[str] | None  # None: every collection
    chat_key: str | None = None  # set for pages published by a member or in a group


def scope_of(services: Services, request: Request, *, query: bool = False) -> Scope:
    """The caller's scope, from the X-Attache-Artifact and X-Attache-Token headers, or with
    query=True from ?artifact=&token= (an EventSource cannot send headers). No artifact means
    the Attaché page, whose token is services.ui_token. A wrong token raises 401. A page's
    collections are those its <meta> declares; an "_artifacts" record adds its chat_key."""
    source = request.query_params if query else request.headers
    key = "{}" if query else "x-attache-{}"
    artifact = source.get(key.format("artifact"))
    token = source.get(key.format("token")) or ""
    expected = artifact_token(services.secret, artifact) if artifact else services.ui_token
    valid = not artifact or SLUG.fullmatch(artifact)
    if not valid or not hmac.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(401, "bad or missing token")
    if not artifact:
        return Scope(UI, None)
    page = services.config.home / "artifacts" / f"{artifact}.html"
    match = page.is_file() and META.search(page.read_text(encoding="utf-8"))
    names = match[1].split(",") if match else []
    record = services.store.doc_get("_artifacts", artifact) or {}
    return Scope(artifact, frozenset(n.strip() for n in names if n.strip()), record.get("chat_key"))


def _mcp_tool(tool: Tool) -> dict[str, Any]:
    return {"name": tool.name, "description": tool.description, "inputSchema": tool.schema}


class WebChannel:
    """The web page's side of the owner's chat (local:me), and the hub for live events: one
    (scope, loop, queue) per open event stream."""

    name = "local"

    def __init__(self) -> None:
        self.subs: list[tuple[Scope, asyncio.AbstractEventLoop, asyncio.Queue[Any]]] = []
        self.stopped = asyncio.Event()

    def publish(self, kind: str, **event: Any) -> None:
        """Queue an event for each open stream allowed to see it. Safe from any thread.
        Messages go to the Attaché page only; a doc change goes to the streams whose scope
        covers its collection, and for a chat's page only if the doc is shared with that chat
        (so deletions, where doc is None, never reach those)."""
        for (slug, collections, chat), loop, queue in list(self.subs):
            if kind == "doc":
                shared = chat is None or (event["doc"] is not None and _shared(event["doc"], chat))
                wanted = shared and (collections is None or event["collection"] in collections)
            else:
                wanted = slug == UI
            if wanted and not loop.is_closed():
                loop.call_soon_threadsafe(queue.put_nowait, (kind, event))

    async def stream(self, scope: Scope) -> AsyncIterator[str]:
        """Server-sent events for one page, with a ": ping" comment when idle."""
        self.subs.append(sub := (scope, asyncio.get_running_loop(), asyncio.Queue()))
        try:
            yield ": connected\n\n"
            while True:
                try:
                    kind, event = await asyncio.wait_for(sub[2].get(), PING_SECONDS)
                    yield f"event: {kind}\ndata: {json.dumps(event)}\n\n"
                except TimeoutError:
                    yield ": ping\n\n"
        finally:
            self.subs.remove(sub)

    async def start(self, on_message: OnMessage) -> None:
        await self.stopped.wait()  # messages typed in the page reach the router via /api/chat

    async def send(self, msg: Outbound) -> None:
        self.publish("message", chat_key=msg.chat_key, text=msg.text, files=msg.files)

    async def stop(self) -> None:
        self.stopped.set()


class _BridgeCORS(CORSMiddleware):
    """CORS on the bridge routes only: artifact pages call them from the origin "null"."""

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope["path"].startswith(("/api/db/", "/api/events")):
            await super().__call__(scope, receive, send)
        else:
            await self.app(scope, receive, send)


async def _error(request: Request, exc: Exception) -> JSONResponse:
    code = next(code for kind, code in ERRORS.items() if isinstance(exc, kind))
    return JSONResponse({"error": str(exc.args[0] if exc.args else exc)}, code)


def create_app(services: Services, router: Router, channel: WebChannel) -> Starlette:
    """The app. Registers a store listener, so doc changes reach open pages."""
    store, home = services.store, services.config.home
    store.on_change.append(lambda c, i, d: channel.publish("doc", collection=c, id=i, doc=d))

    def static(name: str, **options: Any) -> Any:
        return lambda request: FileResponse(STATIC / name, **options)

    def ui_only(request: Request) -> None:
        if scope_of(services, request).slug != UI:
            raise PermissionError("this needs the Attaché page's token")

    async def artifact(request: Request) -> Response:
        name = request.path_params["name"]
        page = home / "artifacts" / f"{name}.html"
        if not SLUG.fullmatch(name) or not page.is_file():
            raise KeyError(f"no artifact {name!r}")
        if request.headers.get("sec-fetch-dest", "iframe") != "iframe":
            raise PermissionError("pages open inside the Attaché window only")
        text = page.read_text(encoding="utf-8")
        tag = TAG.format(artifact_token(services.secret, name), name)
        html, found = re.subn(r"</head\s*>", lambda end: tag + end[0], text, count=1, flags=re.I)
        headers = {"Content-Security-Policy": CSP}
        return Response(html if found else tag + text, headers=headers, media_type="text/html")

    async def artifacts(request: Request) -> Response:
        ui_only(request)
        return JSONResponse(sorted(page.stem for page in (home / "artifacts").glob("*.html")))

    async def messages(request: Request) -> Response:
        ui_only(request)
        keep = ("role", "text", "ts")
        return JSONResponse([{k: m[k] for k in keep} for m in store.history("local:me", 50)])

    async def chat(request: Request) -> Response:
        ui_only(request)
        text = str((await request.json()).get("text", ""))  # the page never sends empty text
        msg = Inbound("local", "me", "owner", services.config.owner.name, text)
        return JSONResponse({"ok": True}, background=BackgroundTask(router.handle, msg))

    async def db(request: Request) -> Response:
        scope = scope_of(services, request)
        collection, doc_id = request.path_params["collection"], request.path_params.get("id")
        if scope.collections is not None and collection not in scope.collections:
            raise PermissionError(f"this page may not use collection {collection!r}")
        ops = {"POST": "put", "PUT": "put", "DELETE": "delete"}
        op = ops.get(request.method) or ("get" if doc_id else "list")
        data = await request.json() if op == "put" else None
        by, chat = f"web:{scope.slug}", scope.chat_key
        return JSONResponse(db_access(store, op, collection, doc_id, data, by=by, chat=chat))

    async def events(request: Request) -> Response:
        stream = channel.stream(scope_of(services, request, query=True))
        return StreamingResponse(stream, media_type="text/event-stream")

    async def mcp(request: Request) -> Response:
        """One JSON-RPC 2.0 request for the tools of the turn the token names."""
        if (turn := services.gateway.get(request.path_params["token"])) is None:
            raise KeyError("unknown or finished turn")
        message = await request.json()
        if "id" not in message:  # a notification: nothing to answer
            return Response(status_code=202)
        method, params = message.get("method"), message.get("params") or {}
        results = {
            "initialize": {**SERVER, "protocolVersion": params.get("protocolVersion")},
            "ping": {},
            "tools/list": {"tools": [_mcp_tool(tool) for tool in turn.tools]},
        }
        if method == "tools/call":
            args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            out = await run_tool(ToolCall(str(message["id"]), str(params.get("name")), args), turn)
            content = [{"type": "text", "text": out}]
            results[method] = {"content": content, "isError": out.startswith("error:")}
        reply = {"jsonrpc": "2.0", "id": message["id"]}
        if method in results:
            return JSONResponse({**reply, "result": results[method]})
        return JSONResponse({**reply, "error": {"code": -32601, "message": f"no method {method}"}})

    routes = [
        Route("/", static("ui.html", headers={"Content-Security-Policy": UI_CSP})),
        Route("/bridge.js", static("bridge.js", media_type="text/javascript")),
        Route("/a/{name}", artifact),
        Route("/api/artifacts", artifacts),
        Route("/api/messages", messages),
        Route("/api/chat", chat, methods=["POST"]),
        Route("/api/db/{collection}", db, methods=["GET", "POST"]),
        Route("/api/db/{collection}/{id}", db, methods=["GET", "PUT", "DELETE"]),
        Route("/api/events", events),
        Route("/mcp/{token}", mcp, methods=["POST"]),
    ]
    headers = ["X-Attache-Token", "X-Attache-Artifact", "Content-Type"]
    methods = ["GET", "PUT", "DELETE", "POST"]
    middleware = [
        Middleware(TrustedHostMiddleware, allowed_hosts=LOOPBACK),
        Middleware(_BridgeCORS, allow_origins=["*"], allow_methods=methods, allow_headers=headers),
    ]
    errors = dict.fromkeys(ERRORS, _error)  # HTTPException (401, 405) answers as plain text
    return Starlette(routes=routes, middleware=middleware, exception_handlers=errors)


async def serve(app: Starlette, sock: socket.socket) -> None:
    """Run uvicorn on a socket that app.run has bound, until cancelled. The graceful shutdown
    is short, so open event streams do not hold up Ctrl+C."""
    config = uvicorn.Config(app, log_level="warning", lifespan="off", timeout_graceful_shutdown=1)
    await uvicorn.Server(config).serve(sockets=[sock])
