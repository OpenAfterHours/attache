"""The local web server: chat page, artifacts, the data bridge, live events and MCP.

Routes
  GET  /                       static/ui.html. It holds no token: the terminal prints
                               http://127.0.0.1:<port>/#t=<services.ui_token> and the page keeps it.
  GET  /bridge.js              static/bridge.js
  GET  /fonts/<file>.woff2     each font shipped in static/fonts/ (one route per file, so any
                               other name is 404), cached for a year, with
                               Access-Control-Allow-Origin: * (a sandboxed page's font loads
                               are CORS requests from the origin "null")
  GET  /a/{name}?t=<token>     artifacts/<name>.html (name [a-z0-9-]+) with the bridge script
                               injected and CSP; 403 without the page's token. Only frames load
                               it (Sec-Fetch-Dest: iframe), except that snapshot_url's extra
                               &r=<render token> lets the render skill's browser open it top-level
  GET  /api/artifacts          [{name, src}], src = "/a/<name>?t=<token>", sorted   UI token
  GET  /api/messages           the last 50 of local:me, as store.history gives them    UI token
  POST /api/chat {text}        an owner Inbound in local:me (origin "web"); the reply arrives
                               over SSE                                                UI token
  POST /api/answer {ask_id, choice}  the owner's answer to an ask: {"ok": accepted}   UI token
  GET|POST /api/db/{c}         list | create   } the UI token or a page's token, under the db
  GET|PUT|DELETE /api/db/{c}/{id}             } tool's rules (store.db_access)
  GET  /api/events             SSE with ?token= (the UI) or ?artifact=&token= (a page). For the
                               UI only: "message" (an Outbound's fields; with author set, a
                               message the owner typed in the terminal) and "ask" {ask_id,
                               text, options}. "doc" {collection, id, doc} within the scope.
                               A ping every 15 s.
  POST /mcp/{token}            MCP over Streamable HTTP (plain JSON responses) for the tools of
                               the turn services.gateway[token]; Claude Code connects here

Security
  Artifacts are written by the agent and may quote emails or web pages, so they are untrusted.
  The CSP sandbox without allow-same-origin gives each page an opaque origin: it cannot read the
  Attaché page or its storage, and reaches the server only through the bridge routes, which
  send CORS headers for that "null" origin (tokens travel in headers, never cookies). Other
  routes send none. The CSP also blocks every external resource, pop-ups, form posts and
  framing by other sites; pages open only inside the Attaché window. A page sends the headers
  X-Attache-Artifact: <slug> and X-Attache-Token: artifact_token(ui_token, slug), so page
  tokens change with every run, as the UI token does. Its collections come only from its
  <meta name="attache-collections" content="a,b">. Pages published by a member or in a group
  have an "_artifacts" record {chat_key}, and see only docs shared with that chat.
  Only loopback Host headers are served (against DNS rebinding); never a wildcard address.
  Starlette keeps the brackets of an IPv6 host, so "[::1]" matches.
  /mcp refuses any request with an Origin header (403), so web pages cannot post to it, and
  wants Content-Type: application/json (415). Bad token 401, refused 403, missing 404, and a
  body that is not a JSON object 400.
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
from attache.agent import run_tool, tool_spec
from attache.store import _shared, db_access
from attache.types import Inbound, OnMessage, Outbound, ToolCall

if TYPE_CHECKING:
    from attache.app import Services
    from attache.router import Router

UI = "_ui"  # the scope slug of the Attaché page itself
PING_SECONDS = 15
STATIC = Path(__file__).parent / "static"
SLUG = re.compile(r"[a-z0-9-]+")
META = re.compile(r"<meta\b(?=[^>]*name=.attache-collections.)[^>]*content=[\"']([^\"']*)", re.I)
PAGE_HEADERS = {
    "Content-Security-Policy": "sandbox allow-scripts allow-forms; default-src 'self' "
    "'unsafe-inline' data:; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; "
    "form-action 'none'; base-uri 'none'; frame-ancestors 'self'"
}
UI_CSP = "default-src 'self' 'unsafe-inline' data:; frame-ancestors 'none'; base-uri 'none'"
FONT_HEADERS = {"Access-Control-Allow-Origin": "*", "Cache-Control": "max-age=31536000, immutable"}
ERRORS = {PermissionError: 403, KeyError: 404, ValueError: 400}
SERVER = {"capabilities": {"tools": {}}, "serverInfo": {"name": "attache", "version": __version__}}


def artifact_token(key: bytes, name: str) -> str:
    """The token that lets the page `name` load and use the bridge; key is the run's UI token."""
    return hmac.new(key, f"attache:{name}".encode(), "sha256").hexdigest()


def page_token(services: Services, name: str) -> str:
    return artifact_token(services.ui_token.encode(), name)


def snapshot_url(services: Services, name: str) -> str:
    """A link that opens the page `name` top-level, for the render skill to take a picture of.
    Its r= token is valid for that page only, and rotates with the run like every page token."""
    tokens = f"t={page_token(services, name)}&r={page_token(services, f'render:{name}')}"
    return f"{services.web_url}/a/{name}?{tokens}"


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
    field = "{}" if query else "x-attache-{}"
    artifact = source.get(field.format("artifact"))
    token = source.get(field.format("token")) or ""
    expected = page_token(services, artifact) if artifact else services.ui_token
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


async def body(request: Request) -> dict[str, Any]:
    """The request's JSON body, which must be an object: ValueError (so 400) otherwise."""
    if not isinstance(data := await request.json(), dict):
        raise ValueError("send a JSON object")
    return data


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
            shared = chat is None or (event.get("doc") is not None and _shared(event["doc"], chat))
            in_scope = collections is None or event.get("collection") in collections
            wanted = (shared and in_scope) if kind == "doc" else slug == UI
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
        """A reply, or with `author` set a message the owner typed elsewhere (the terminal).
        What the page sent itself (origin "web") it already shows."""
        if msg.origin != "web":
            self.publish("message", **vars(msg))

    async def ask(self, chat_id: str, ask_id: str, text: str, options: list[str]) -> None:
        self.publish("ask", ask_id=ask_id, text=text, options=options)

    async def stop(self) -> None:
        self.stopped.set()


class _BridgeCORS(CORSMiddleware):
    """CORS on the bridge routes only: artifact pages call them from the origin "null"."""

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        bridge = scope["type"] == "http" and scope["path"].startswith(("/api/db/", "/api/events"))
        await (super().__call__ if bridge else self.app)(scope, receive, send)


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
        token, render = page_token(services, name), page_token(services, f"render:{name}")
        given, rendering = request.query_params.get("t", ""), request.query_params.get("r", "")
        dest = request.headers.get("sec-fetch-dest", "iframe")
        top = dest == "document" and hmac.compare_digest(rendering.encode(), render.encode())
        if not (dest == "iframe" or top) or not hmac.compare_digest(given.encode(), token.encode()):
            raise PermissionError("pages open inside the Attaché window only")
        text = page.read_text(encoding="utf-8")
        tag = f'<script src="/bridge.js" data-token="{token}" data-artifact="{name}"></script>'
        html, found = re.subn(r"</head\s*>", lambda end: tag + end[0], text, count=1, flags=re.I)
        return Response(html if found else tag + text, headers=PAGE_HEADERS, media_type="text/html")

    async def artifacts(request: Request) -> Response:
        ui_only(request)
        files = (home / "artifacts").glob("*.html")
        names = sorted(p.stem for p in files if SLUG.fullmatch(p.stem))
        listing = [{"name": n, "src": f"/a/{n}?t={page_token(services, n)}"} for n in names]
        return JSONResponse(listing)

    async def messages(request: Request) -> Response:
        ui_only(request)
        return JSONResponse(store.history("local:me", 50))  # reminders come from "Reminder"

    async def chat(request: Request) -> Response:
        ui_only(request)
        text = str((await body(request)).get("text", ""))  # the page never sends empty text
        msg = Inbound("local", "me", "owner", services.config.owner.name, text, origin="web")
        return JSONResponse({"ok": True}, background=BackgroundTask(router.handle, msg))

    async def answer(request: Request) -> Response:
        ui_only(request)
        data = await body(request)
        ok = services.approvals.answer(str(data.get("ask_id")), "owner", str(data.get("choice")))
        return JSONResponse({"ok": ok})

    async def db(request: Request) -> Response:
        scope = scope_of(services, request)
        collection, doc_id = request.path_params["collection"], request.path_params.get("id")
        if scope.collections is not None and collection not in scope.collections:
            raise PermissionError(f"this page may not use collection {collection!r}")
        ops = {"POST": "put", "PUT": "put", "DELETE": "delete"}
        op = ops.get(request.method) or ("get" if doc_id else "list")
        data = await body(request) if op == "put" else None
        by, chat = f"web:{scope.slug}", scope.chat_key
        return JSONResponse(db_access(store, op, collection, doc_id, data, by=by, chat=chat))

    async def events(request: Request) -> Response:
        stream = channel.stream(scope_of(services, request, query=True))
        return StreamingResponse(stream, media_type="text/event-stream")

    async def mcp(request: Request) -> Response:
        """One JSON-RPC 2.0 request for the tools of the turn the token names."""
        if "origin" in request.headers:  # a browser: MCP clients send no Origin
            raise PermissionError("MCP takes no requests from web pages")
        if (turn := services.gateway.get(request.path_params["token"])) is None:
            raise KeyError("unknown or finished turn")
        if request.headers.get("content-type", "").partition(";")[0] != "application/json":
            raise HTTPException(415, "send Content-Type: application/json")
        message = await body(request)  # a batch (a JSON array) is refused here too
        if "id" not in message:  # a notification: nothing to answer
            return Response(status_code=202)
        method, params = message.get("method"), message.get("params") or {}
        if method == "initialize":
            result = {**SERVER, "protocolVersion": params.get("protocolVersion")}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [tool_spec(tool, key="inputSchema") for tool in turn.tools]}
        elif method == "tools/call":
            args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            out = await run_tool(ToolCall(str(message["id"]), str(params.get("name")), args), turn)
            result = {"content": [{"type": "text", "text": out}], "isError": out[:6] == "error:"}
        else:
            error = {"code": -32601, "message": f"no method {method}"}
            return JSONResponse({"jsonrpc": "2.0", "id": message["id"], "error": error})
        return JSONResponse({"jsonrpc": "2.0", "id": message["id"], "result": result})

    fonts = sorted(f"fonts/{p.name}" for p in (STATIC / "fonts").glob("*.woff2"))  # others: 404
    routes = [
        Route("/", static("ui.html", headers={"Content-Security-Policy": UI_CSP})),
        Route("/bridge.js", static("bridge.js", media_type="text/javascript")),
        *(Route(f"/{font}", static(font, headers=FONT_HEADERS)) for font in fonts),
        Route("/a/{name}", artifact),
        Route("/api/artifacts", artifacts),
        Route("/api/messages", messages),
        Route("/api/chat", chat, methods=["POST"]),
        Route("/api/answer", answer, methods=["POST"]),
        Route("/api/db/{collection}", db, methods=["GET", "POST"]),
        Route("/api/db/{collection}/{id}", db, methods=["GET", "PUT", "DELETE"]),
        Route("/api/events", events),
        Route("/mcp/{token}", mcp, methods=["POST"]),
    ]
    headers = ["X-Attache-Token", "X-Attache-Artifact", "Content-Type"]
    methods = ["GET", "PUT", "DELETE", "POST"]
    middleware = [
        Middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"]),
        Middleware(_BridgeCORS, allow_origins=["*"], allow_methods=methods, allow_headers=headers),
    ]
    errors = dict.fromkeys(ERRORS, _error)  # HTTPException (401, 405, 415) answers as plain text
    return Starlette(routes=routes, middleware=middleware, exception_handlers=errors)


async def serve(app: Starlette, sock: socket.socket) -> None:
    """Run uvicorn on a socket that app.run has bound, until cancelled. Cancelled, it stops
    taking requests and gives the ones running (an /api/chat turn, say) up to 2 seconds to
    finish before they are cancelled, so they are done before app.run closes the store.
    (uvicorn sets should_exit and shuts down by itself when a signal stops it; a cancel skips
    that, so the finally does the shutdown, unless it has happened or startup never ended.)"""
    config = uvicorn.Config(app, log_level="warning", lifespan="off", timeout_graceful_shutdown=2)
    server = uvicorn.Server(config)
    try:
        await server.serve(sockets=[sock])
    finally:
        if server.started and not server.should_exit:
            await server.shutdown(sockets=[sock])
