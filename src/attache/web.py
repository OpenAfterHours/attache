"""Local web server: chat page, artifacts, data bridge, live events, and the tool gateway that
`attache mcp` calls. Owner: web. The routes are listed in create_app.

Artifacts are untrusted: a CSP sandbox without allow-same-origin gives each page an opaque origin,
so it cannot read / (which embeds the UI token). Only the bridge routes send CORS headers."""

from __future__ import annotations

import asyncio
import hmac
import json
import re
import secrets
import socket
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import uvicorn
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware, cors, trustedhost
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from attache.agent import run_tool
from attache.store import _shared, db_access
from attache.types import Inbound, OnMessage, Outbound, ToolCall, Turn

if TYPE_CHECKING:
    from attache.app import Services
    from attache.router import Router

UI, PING, STATIC, SLUG = "_ui", 15, Path(__file__).parent / "static", re.compile(r"[a-z0-9-]+")
META = re.compile(r"<meta\b(?=[^>]*name=.attache-collections.)[^>]*content=[\"']([^\"']*)", re.I)
TAG = '<script src="/bridge.js" data-token="{}" data-artifact="{}"></script>'
CDN = "https://cdnjs.cloudflare.com https://cdn.jsdelivr.net https://fonts.googleapis.com"
SRC = f"default-src 'self' 'unsafe-inline' data: {CDN} https://fonts.gstatic.com"
CSP = f"sandbox allow-scripts allow-forms allow-popups; {SRC}; connect-src 'self'"
UI_CSP = "default-src 'self' 'unsafe-inline' data:; frame-ancestors 'none'"
CORS = {"allow_origins": ["*"], "allow_methods": ["GET", "PUT", "DELETE", "POST"]}
HEADERS = ["X-Attache-Token", "X-Attache-Artifact", "Content-Type"]
STATUS = {HTTPException: 0, PermissionError: 403, KeyError: 404, ValueError: 400}
Scope = tuple[str, "set[str] | None", "str | None"]  # (slug, collections, chat_key)


def artifact_token(secret: bytes, name: str) -> str:
    return hmac.new(secret, f"attache:{name}".encode(), "sha256").hexdigest()


class ToolGateway:
    """Short-lived tokens that expose one turn's tools over HTTP (MCP descriptors and calls)."""

    def __init__(self) -> None:
        self.turns: dict[str, Turn] = {}

    def open(self, turn: Turn) -> str:
        self.turns[token := secrets.token_urlsafe(24)] = turn
        return token

    def close(self, token: str) -> None:
        self.turns.pop(token, None)

    def list(self, token: str) -> list[dict[str, Any]]:
        """MCP tool descriptors; an unknown token gets []."""
        tools = self.turns[token].tools if token in self.turns else []
        return [
            {"name": t.name, "description": t.description, "inputSchema": t.schema} for t in tools
        ]

    async def call(self, token: str, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        """(text, is_error). An unknown token or tool is an error result, not an exception."""
        if (turn := self.turns.get(token)) is None:
            return "error: unknown or expired tool token", True
        text = await run_tool(ToolCall(secrets.token_hex(4), name, args), turn)
        return text, text.startswith("error:")


class WebChannel:
    """Chat key "web:local", and the SSE hub: one (scope, loop, queue) per open page."""

    name = "web"

    def __init__(self) -> None:
        self.subs: list[tuple[Scope, asyncio.AbstractEventLoop, asyncio.Queue[Any]]] = []
        self.stopped = asyncio.Event()

    def publish(self, kind: str, **event: Any) -> None:
        """Queue an event for each page that may see it. Safe from any thread."""
        col, doc = event.get("collection"), event.get("doc")
        for (slug, cols, chat), loop, queue in list(self.subs):
            shared = not chat or (doc is not None and _shared(doc, chat))
            mine = cols is None or (col in cols and shared)
            if (mine if kind == "doc" else slug == UI) and not loop.is_closed():
                loop.call_soon_threadsafe(queue.put_nowait, (kind, event))

    async def stream(self, scope: Scope) -> AsyncIterator[str]:
        """Server-sent events for one page, with a ": ping" comment every 15 s."""
        self.subs.append(sub := (scope, asyncio.get_running_loop(), asyncio.Queue()))
        try:
            yield ": connected\n\n"
            while True:
                try:
                    kind, event = await asyncio.wait_for(sub[2].get(), PING)
                    yield f"event: {kind}\ndata: {json.dumps(event)}\n\n"
                except TimeoutError:
                    yield ": ping\n\n"
        finally:
            self.subs.remove(sub)

    async def start(self, on_message: OnMessage) -> None:
        await self.stopped.wait()  # web messages reach the router straight from /api/chat

    async def send(self, msg: Outbound) -> None:
        self.publish("message", chat_key=msg.chat_key, text=msg.text, files=msg.files)

    async def stop(self) -> None:
        self.stopped.set()


def create_app(services: Services, router: Router, channel: WebChannel) -> Starlette:
    """The app. Registers a store listener so doc changes reach open pages over SSE."""
    secret, store, home = services.secret, services.store, services.config.home
    store.on_change.append(lambda c, i, d: channel.publish("doc", collection=c, id=i, doc=d))
    ui = (STATIC / "ui.html").read_text(encoding="utf-8")
    ui, js = ui.replace("__ATTACHE_UI_TOKEN__", artifact_token(secret, UI)), STATIC / "bridge.js"

    def scope_of(request, sse=False) -> Scope:  # "_artifacts" doc, else <meta>: owner access
        src, pre = (request.query_params, "") if sse else (request.headers, "x-attache-")
        slug, token = src.get(pre + "artifact") or UI, src.get(pre + "token") or ""
        good = hmac.compare_digest(token.encode(), artifact_token(secret, slug).encode())
        if not good or not (slug == UI or SLUG.fullmatch(slug)):
            raise HTTPException(401, "bad or missing token")
        if slug == UI:
            return UI, None, None
        if reg := store.doc_get("_artifacts", slug):
            return slug, set(reg.get("collections") or ()), reg.get("chat_key") or None
        page = home / "artifacts" / f"{slug}.html"
        m = META.search(page.read_text(encoding="utf-8")) if page.is_file() else None
        return slug, {c.strip() for c in (m[1] if m else "").split(",") if c.strip()}, None

    async def body(request):
        if not isinstance(data := await request.json(), dict):
            raise ValueError("send a JSON object")
        return data

    async def error(request, exc):
        code = next(v for k, v in STATUS.items() if isinstance(exc, k)) or exc.status_code
        return JSONResponse({"error": str(getattr(exc, "detail", "") or exc.args[0])}, code)

    async def artifact(request):
        path = home / "artifacts" / f"{(name := request.path_params['name'])}.html"
        if not SLUG.fullmatch(name) or not path.is_file():
            raise HTTPException(404, f"no artifact {name!r}")
        text, tag = path.read_text(encoding="utf-8"), TAG.format(artifact_token(secret, name), name)
        at = m.start() if (m := re.search(r"</head\s*>", text, re.I)) else 0
        return HTMLResponse(text[:at] + tag + text[at:], headers={"Content-Security-Policy": CSP})

    async def artifacts(request):
        if scope_of(request)[0] != UI:
            raise PermissionError("this needs the UI token")
        out = []
        for path in sorted((home / "artifacts").glob("*.html")):
            m = re.search(r"<title[^>]*>([^<]*)", path.read_text(encoding="utf-8"), re.I)
            title = (m[1].strip() if m else "") or path.stem
            out.append({"name": path.stem, "title": title, "url": f"/a/{path.stem}"})
        return JSONResponse([a for a in out if SLUG.fullmatch(a["name"])])

    async def chat(request):  # the reply arrives over SSE
        if scope_of(request)[0] != UI:
            raise PermissionError("this needs the UI token")
        if not isinstance(text := (await body(request)).get("text"), str) or not text.strip():
            raise ValueError("send some text")
        msg = Inbound("web", "local", "owner", services.config.owner.name, text)
        return JSONResponse({"ok": True}, background=BackgroundTask(router.handle, msg))

    async def db(request):
        (slug, cols, chat_key), p = scope_of(request), request.path_params
        if cols is not None and p["collection"] not in cols:
            raise PermissionError(f"this page may not use collection {p['collection']!r}")
        op = {"PUT": "put", "POST": "put", "DELETE": "delete"}.get(request.method)
        op = op or ("get" if "id" in p else "list")
        data = await body(request) if op == "put" else None
        kw = {"by": f"web:{slug}", "chat": chat_key}
        return JSONResponse(db_access(store, op, p["collection"], p.get("id"), data, **kw))

    async def events(request):
        stream = channel.stream(scope_of(request, sse=True))
        return StreamingResponse(stream, media_type="text/event-stream")

    async def mcp(request):
        token, gateway = request.path_params["token"], services.gateway
        if request.method == "GET":
            return JSONResponse({"tools": gateway.list(token)})
        data = await body(request)
        text, err = await gateway.call(token, str(data.get("name")), data.get("arguments") or {})
        return JSONResponse({"content": [{"type": "text", "text": text}], "isError": err})

    routes = [
        Route("/", lambda _: HTMLResponse(ui, headers={"Content-Security-Policy": UI_CSP})),
        Route("/bridge.js", lambda _: Response(js.read_bytes(), media_type="text/javascript")),
        Route("/a/{name}", artifact),
        Route("/api/artifacts", artifacts),
        Route("/api/chat", chat, methods=["POST"]),
        Route("/api/db/{collection}", db, methods=["GET", "POST"]),
        Route("/api/db/{collection}/{id}", db, methods=["GET", "PUT", "DELETE"]),
        Route("/api/events", events),
        Route("/api/mcp/{token}/tools", mcp),
        Route("/api/mcp/{token}/call", mcp, methods=["POST"]),
    ]
    hosts = ["127.0.0.1", "localhost", "::1", services.config.host]
    middleware = [Middleware(trustedhost.TrustedHostMiddleware, allowed_hosts=hosts)]
    middleware.append(Middleware(_BridgeCORS, allow_headers=HEADERS, **CORS))
    errors = dict.fromkeys(STATUS, error)
    return Starlette(routes=routes, middleware=middleware, exception_handlers=errors)


class _BridgeCORS(cors.CORSMiddleware):  # only the bridge routes, called from origin "null"
    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        bridge = scope.get("path", "").startswith(("/api/db/", "/api/events"))
        await (super().__call__ if bridge else self.app)(scope, receive, send)


async def serve(app: Starlette, host: str, port: int) -> None:
    """Run uvicorn in this event loop until cancelled. Port in use -> OSError."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.create_server((host, port), family=family)  # bind here: in use -> OSError
    config = uvicorn.Config(app, log_level="warning", lifespan="off", timeout_graceful_shutdown=1)
    await uvicorn.Server(config).serve(sockets=[sock])
