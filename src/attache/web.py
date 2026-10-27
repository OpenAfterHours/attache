"""The web servers: the local one (chat page, artifacts, the data bridge, live events, MCP) and,
when sharing is on, the public one for share links.

Local routes (create_app(services, channel, router)), on [web] host:port
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
                               &r=<snapshot token> lets the render skill's browser open it
                               top-level, for 10 minutes
  GET  /api/artifacts          [{name, src, version, modified}], src = "/a/<name>?t=<token>",
                               modified the file's mtime in seconds (recent pages) UI token
  GET|PUT /api/settings        safe editable settings and revision-checked save    UI token
  POST /api/settings/validate  preview {revision, changes|toml}, without saving     UI token
  POST /api/settings/restore   restore {revision, backup_revision}                 UI token
  GET  /api/messages           {messages: the last 50 of local:me, as store.history gives
                               them; asks: [{ask_id, text, options}] still waiting for the
                               owner there, so a reload keeps their buttons}          UI token
  POST /api/chat {text}        an owner Inbound in local:me (origin "web"); the reply arrives
                               over SSE                                                UI token
  POST /api/answer {ask_id, choice}  the owner's answer to an ask: {"ok": accepted}   UI token
  POST /api/stop               stop the turns running in local:me (ChannelManager.stop_turns):
                               {"stopped": whether one was}                           UI token
  POST /api/ask {prompt}      a page turn (router.page_turn) for the page whose token it
                               carries: {"reply": text}. A page's token only, never the UI's
  GET|POST /api/db/{c}         list | create   } the UI token or a page's token, under the db
  GET|PUT|DELETE /api/db/{c}/{id}             } tool's rules (store.db_access)
  GET  /api/events             SSE with ?token= (the UI) or ?artifact=&token= (a page). For the
                               UI only: "message" (an Outbound's fields; with author set, a
                               message the owner typed in the terminal), "ask" {ask_id,
                               text, options} and "activity" {kind, ...data}, a local:me turn's
                               progress (started, step, thinking, finished; see
                               ChannelManager.activity), or kind "new" when the owner started
                               the chat afresh (/new: reload it). "doc" {collection, id, doc}
                               within the scope. A ping every 15 s.
  POST /mcp/{token}            MCP over Streamable HTTP (plain JSON responses) for the tools of
                               the turn services.gateway[token]; Claude Code connects here

Public routes (create_app(services, channel, public=True)), on 127.0.0.1:[web] public_port,
which Tailscale Funnel exposes as [web] public_url. Nothing else exists there.
  GET  /s/{token}              the shared page top-level, with the bridge (data-share); 410
                               once the link has expired or the page has changed
  GET  /bridge.js, /fonts/...  as above
  /api/db/..., /api/events     as above, for share scopes only: the X-Attache-Share header,
                               or ?share= for SSE. Writes through a link are capped: 64 KB
                               a body by its Content-Length, checked before reading, so no
                               length or chunks is 413 too; 60 an hour per link (429); and
                               no new doc once the chat has 500 in that collection (403)

Security
  Artifacts are written by the agent and may quote emails or web pages, so they are untrusted.
  The CSP sandbox without allow-same-origin gives each page an opaque origin: it cannot read the
  Attaché page or its storage, and reaches the server only through the bridge routes, which
  send CORS headers for that "null" origin (tokens travel in headers, never cookies). Other
  routes send none. The CSP also blocks every external resource, pop-ups, form posts and
  framing by other sites; pages open only inside the Attaché window. A page sends the headers
  X-Attache-Artifact: <slug> and X-Attache-Token: page_token(services, slug), so page
  tokens change with every run, as the UI token does. Its collections come only from its
  <meta name="attache-collections" content="a,b">. Pages published by a member or in a group
  have an "_artifacts" record {chat_key}, and see only docs shared with that chat. No page
  reaches "_shares" or "_requests".
  Only loopback Host headers are served (against DNS rebinding); never a wildcard address.
  Starlette keeps the brackets of an IPv6 host, so "[::1]" matches.
  Share links (share_url) exist only for pages with a chat_key, and get that chat's member
  access, never the owner's. The store keeps only the sha256 of each link's token, in
  "_shares". Funnel connects from loopback too, so the public app is a separate server on its
  own port, rather than routes told apart by the Host header. It takes the public host and
  the loopback names (Funnel may pass either on), and has only the share routes.
  /mcp refuses any request with an Origin header (403), so web pages cannot post to it, and
  wants Content-Type: application/json (415). Bad token 401, refused 403, missing 404, and a
  body that is not a JSON object 400.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import re
import secrets
import socket
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple
from urllib.parse import urlsplit

import uvicorn
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from attache import __version__
from attache.agent import run_tool, tool_spec
from attache.settings import MAX_BYTES, Settings, SettingsError
from attache.store import _shared, db_access
from attache.types import Inbound, OnMessage, Outbound, ToolCall

if TYPE_CHECKING:
    from attache.app import Services

UI = "_ui"  # the scope slug of the Attaché page itself
PING_SECONDS = 15
STATIC = Path(__file__).parent / "static"
SLUG = re.compile(r"[a-z0-9-]+")
META = re.compile(r"<meta\b(?=[^>]*name=.attache-collections.)[^>]*content=[\"']([^\"']*)", re.I)
PAGE_CSP = (  # then who may frame the page: 'self' (the Attaché window), or 'none' (a share link)
    "sandbox allow-scripts allow-forms; default-src 'self' 'unsafe-inline' data:; img-src 'self' "
    "data:; font-src 'self' data:; connect-src 'self'; form-action 'none'; base-uri 'none'; "
    "frame-ancestors "
)
PAGE_HEADERS = {"Content-Security-Policy": PAGE_CSP + "'self'"}
SHARE_HEADERS = {"Content-Security-Policy": PAGE_CSP + "'none'", "Referrer-Policy": "no-referrer"}
UI_CSP = "default-src 'self' 'unsafe-inline' data:; frame-ancestors 'none'; base-uri 'none'"
FONT_HEADERS = {"Access-Control-Allow-Origin": "*", "Cache-Control": "max-age=31536000, immutable"}
LOOPBACK = ["127.0.0.1", "localhost", "[::1]"]
BRIDGE = ("/api/db/", "/api/events", "/api/ask")  # the routes pages call, from the origin "null"
ERRORS = {PermissionError: 403, KeyError: 404, ValueError: 400}
SERVER = {"capabilities": {"tools": {}}, "serverInfo": {"name": "attache", "version": __version__}}


def page_token(services: Services, name: str) -> str:
    """The token that lets the page `name` load and use the bridge: an HMAC keyed with the
    run's UI token, so it changes every run too."""
    return hmac.new(services.ui_token.encode(), f"attache:{name}".encode(), "sha256").hexdigest()


def _version(services: Services, name: str) -> str | None:
    """A short hash of the page file (None when there is none), so a share link dies when the
    page changes or goes."""
    page = services.config.home / "artifacts" / f"{name}.html"
    return hashlib.sha256(page.read_bytes()).hexdigest()[:12] if page.is_file() else None


def snapshot_url(services: Services, name: str) -> str:
    """A link that opens the page `name` top-level, for the render skill to take a picture of.
    Its r= token is random, for that page only, and lasts 10 minutes of this run
    (services.snapshots, in memory, which drops the expired ones as new ones come)."""
    services.snapshots = {t: s for t, s in services.snapshots.items() if s[1] > time.time()}
    token = secrets.token_urlsafe(16)
    services.snapshots[token] = (name, time.time() + 600)
    return f"{services.web_url}/a/{name}?t={page_token(services, name)}&r={token}"


def share_url(services: Services, name: str) -> str:
    """A link to the page `name` for people off this machine: {public_url}/s/<token>, for 7
    days and while the page file stays as it is now. Only a page with a chat_key (published by
    a member or in a group) can be shared, else PermissionError; the link gets that chat's
    member access to the collections the page declares now. The store keeps the token's
    sha256 only, so reading "_shares" gives no working link."""
    scope = page_scope(services, name)
    if scope.chat_key is None:
        raise PermissionError("only a page made in a chat can be shared")
    token = secrets.token_urlsafe(24)
    grant = {"name": name, "chat_key": scope.chat_key, "collections": sorted(scope.collections)}
    share = {**grant, "version": _version(services, name), "until": time.time() + 7 * 86400}
    services.store.doc_put("_shares", hashlib.sha256(token.encode()).hexdigest(), share)
    return f"{services.config.public_url}/s/{token}"


class Scope(NamedTuple):
    """Who is calling: the Attaché page (slug "_ui", every collection) or one artifact page."""

    slug: str
    collections: frozenset[str] | None  # None: every collection
    chat_key: str | None = None  # set for pages published by a member or in a group


def page_scope(services: Services, name: str) -> Scope:
    """The page's scope now: the collections its <meta> declares, and the chat_key of its
    "_artifacts" record, if it has one. "_shares" (link hashes) and "_requests" (members'
    requests to the owner) are never a page's, even if it declares them."""
    page = services.config.home / "artifacts" / f"{name}.html"
    match = page.is_file() and META.search(page.read_text(encoding="utf-8"))
    declared = match[1].split(",") if match else []
    names = {n.strip() for n in declared if n.strip()} - {"_shares", "_requests"}
    record = services.store.doc_get("_artifacts", name) or {}
    return Scope(name, frozenset(names), record.get("chat_key"))


def share_scope(services: Services, token: str) -> Scope:
    """The scope a share link's token grants, as stored when the link was made: 410 for an
    unknown token, and once the link has expired, or the page has changed or gone."""
    share = services.store.doc_get("_shares", hashlib.sha256(token.encode()).hexdigest())
    gone = share is None or share["until"] < time.time()  # an unknown token, or a week old
    if gone or _version(services, share["name"]) != share["version"]:
        raise HTTPException(410, "this link has expired or the page changed; ask for a new one")
    return Scope(share["name"], frozenset(share["collections"]), share["chat_key"])


def scope_of(
    services: Services, request: Request, *, query: bool = False, public: bool = False
) -> Scope:
    """The caller's scope, from the X-Attache-Artifact and X-Attache-Token headers, or with
    query=True from ?artifact=&token= (an EventSource cannot send headers). No artifact means
    the Attaché page, whose token is services.ui_token. A wrong token raises 401. A page's
    collections are those its <meta> declares; an "_artifacts" record adds its chat_key.
    public=True (the public app) takes only a share link's token, from X-Attache-Share or
    ?share=, and never the others."""
    source = request.query_params if query else request.headers
    field = "{}" if query else "x-attache-{}"
    if public:
        return share_scope(services, source.get(field.format("share")) or "")
    artifact = source.get(field.format("artifact"))
    token = source.get(field.format("token")) or ""
    expected = page_token(services, artifact) if artifact else services.ui_token
    valid = not artifact or SLUG.fullmatch(artifact)
    if not valid or not hmac.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(401, "bad or missing token")
    return page_scope(services, artifact) if artifact else Scope(UI, None)


async def body(request: Request) -> dict[str, Any]:
    """The request's JSON body, which must be an object: ValueError (so 400) otherwise."""
    if not isinstance(data := await request.json(), dict):
        raise ValueError("send a JSON object")
    return data


def _page(page: Path, tag: str, headers: dict[str, str]) -> Response:
    """The page with the bridge's script `tag` before its </head>, or first if it has none."""
    text = page.read_text(encoding="utf-8")
    html, found = re.subn(r"</head\s*>", lambda end: tag + end[0], text, count=1, flags=re.I)
    return Response(html if found else tag + text, headers=headers, media_type="text/html")


class WebChannel:
    """The web page's side of the owner's chat (local:me), and the hub for live events: one
    (scope, loop, queue) per open event stream."""

    name = "local"

    def __init__(self) -> None:
        self.subs: list[tuple[Scope, asyncio.AbstractEventLoop, asyncio.Queue[Any]]] = []
        self.stopped = asyncio.Event()

    def publish(self, kind: str, /, **event: Any) -> None:  # an activity event has a "kind" too
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
        """Server-sent events for one page, with a ": ping" comment when idle. It ends once
        stop() has run, so open pages never hold up the server's shutdown."""
        self.subs.append(sub := (scope, asyncio.get_running_loop(), asyncio.Queue()))
        try:
            yield ": connected\n\n"
            while not self.stopped.is_set():
                try:
                    kind, event = await asyncio.wait_for(sub[2].get(), PING_SECONDS)
                    yield f"event: {kind}\ndata: {json.dumps(event)}\n\n"
                except TimeoutError:
                    yield ": ping\n\n"
        finally:
            self.subs.remove(sub)

    async def start(self, on_message: OnMessage) -> None:
        """Keep `on_message` for /api/chat, the way messages typed in the page come in."""
        self.on_message = on_message
        await self.stopped.wait()

    async def send(self, msg: Outbound) -> None:
        """A reply, or with `author` set a message the owner typed elsewhere (the terminal).
        What the page sent itself (origin "web") it already shows."""
        if msg.origin != "web":
            self.publish("message", **vars(msg))

    async def ask(self, chat_id: str, ask_id: str, text: str, options: list[str]) -> bool:
        """Show a question on the open Attaché pages: False when there is none to see it."""
        self.publish("ask", ask_id=ask_id, text=text, options=options)
        return any(scope.slug == UI for scope, _, _ in self.subs)

    async def activity(self, chat_id: str, kind: str, data: dict[str, Any]) -> None:
        """A local:me turn's progress, as {kind, ...data}: for the Attaché page only, like
        messages, so artifact pages and share links never see steps or thinking."""
        self.publish("activity", kind=kind, **data)

    async def stop(self) -> None:
        self.stopped.set()
        for _, _, queue in self.subs:  # wake each stream, which sends "bye" and ends
            queue.put_nowait(("bye", {}))


class _BridgeCORS(CORSMiddleware):
    """CORS on the bridge routes only: artifact pages call them from the origin "null"."""

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        bridge = scope["type"] == "http" and scope["path"].startswith(BRIDGE)
        await (super().__call__ if bridge else self.app)(scope, receive, send)


async def _error(request: Request, exc: Exception) -> JSONResponse:
    code = next(code for kind, code in ERRORS.items() if isinstance(exc, kind))
    return JSONResponse({"error": str(exc.args[0] if exc.args else exc)}, code)


def create_app(
    services: Services, channel: WebChannel, router: Any = None, public: bool = False
) -> Starlette:
    """The local app, or with public=True the share links' app (see the module docstring).
    router is the local app's Router (app.run's), whose page_turn answers /api/ask. The local
    one registers a store listener, so doc changes reach open pages, both apps'."""
    store, home = services.store, services.config.home
    writes: dict[tuple[str, int], int] = {}  # (share token, hour) -> writes through that link
    settings = Settings(services.config)
    if not public:
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
        token, query = page_token(services, name), request.query_params
        dest = request.headers.get("sec-fetch-dest", "iframe")
        framed = dest == "iframe"  # inside the Attaché window
        shot, until = services.snapshots.get(query.get("r", ""), (None, 0))  # see snapshot_url
        rendering = dest == "document" and shot == name and time.time() < until
        if not (framed or rendering):
            raise PermissionError("pages open inside the Attaché window only")
        if not hmac.compare_digest(query.get("t", "").encode(), token.encode()):
            raise PermissionError("bad or missing page token")
        tag = f'<script src="/bridge.js" data-token="{token}" data-artifact="{name}"></script>'
        return _page(page, tag, PAGE_HEADERS)

    async def shared(request: Request) -> Response:
        token = request.path_params["token"]
        scope = share_scope(services, token)  # so the token is one share_url made
        tag = f'<script src="/bridge.js" data-share="{token}"></script>'
        return _page(home / "artifacts" / f"{scope.slug}.html", tag, SHARE_HEADERS)

    async def artifacts(request: Request) -> Response:
        ui_only(request)
        files = (home / "artifacts").glob("*.html")
        pages = sorted(p for p in files if SLUG.fullmatch(p.stem))
        listing = [
            {
                "name": p.stem,
                "src": f"/a/{p.stem}?t={page_token(services, p.stem)}",
                "version": _version(services, p.stem),
                "modified": p.stat().st_mtime,
            }
            for p in pages
        ]
        return JSONResponse(listing)

    async def edit_settings(request: Request) -> Response:
        ui_only(request)
        headers = {"Cache-Control": "no-store"}
        try:
            if request.method == "GET":
                return JSONResponse(settings.read(), headers=headers)
            if request.headers.get("content-type", "").partition(";")[0] != "application/json":
                return JSONResponse({"error": "Send Content-Type: application/json."}, 415, headers)
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > MAX_BYTES * 2:
                    raise SettingsError("Settings requests are limited to 128 KB.", 413)
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise SettingsError("Send a JSON object.")
            if request.url.path.endswith("/restore"):
                data["restore"] = True
            save = request.method == "PUT" or request.url.path.endswith("/restore")
            return JSONResponse(settings.update(data, save=save), headers=headers)
        except SettingsError as exc:
            return JSONResponse({"error": str(exc)}, exc.status, headers)
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"error": "Send valid JSON."}, 400, headers)
        except OSError:
            return JSONResponse(
                {"error": "Settings file access failed; check local permissions."}, 500, headers
            )

    async def messages(request: Request) -> Response:
        """The chat (reminders come from "Reminder") and the questions still waiting for the
        owner's answer there, so a reloaded page can show their buttons again."""
        ui_only(request)
        asks = [
            {"ask_id": ask_id, "text": ask.text, "options": ask.options}
            for ask_id, ask in services.channels.pending.items()
            if ask.chat_key == "local:me" and ask.person_id == "owner" and not ask.future.done()
        ]
        return JSONResponse({"messages": store.history("local:me", 50), "asks": asks})

    async def chat(request: Request) -> Response:
        ui_only(request)
        text = str((await body(request)).get("text", ""))  # the page never sends empty text
        msg = Inbound("local", "me", "owner", services.config.owner.name, text, origin="web")
        await channel.on_message(msg)  # returns at once: the turn runs as a ChannelManager task
        return JSONResponse({"ok": True})

    async def answer(request: Request) -> Response:
        ui_only(request)
        data = await body(request)
        ok = services.channels.answer(str(data.get("ask_id")), "owner", str(data.get("choice")))
        return JSONResponse({"ok": ok})

    async def stop(request: Request) -> Response:
        """The page's Stop button (or Esc). The stopped turn sends "finished" with stopped."""
        ui_only(request)
        return JSONResponse({"stopped": services.channels.stop_turns("local:me")})

    async def ask(request: Request) -> Response:
        """attache.ask from a page open in the Attaché window: a turn limited to its scope."""
        if (scope := scope_of(services, request)).slug == UI:
            raise PermissionError("attache.ask takes a page's token")
        prompt = str((await body(request)).get("prompt", ""))[:2000]  # capped
        return JSONResponse({"reply": await router.page_turn(scope, prompt)})

    async def db(request: Request) -> Response:
        scope = scope_of(services, request, public=public)
        collection, doc_id = request.path_params["collection"], request.path_params.get("id")
        if scope.collections is not None and collection not in scope.collections:
            raise PermissionError(f"this page may not use collection {collection!r}")
        ops = {"POST": "put", "PUT": "put", "DELETE": "delete"}
        op = ops.get(request.method) or ("get" if doc_id else "list")
        if public and op == "put":  # anyone with a share link can write, so its writes are capped
            hour = (request.headers["x-attache-share"], int(time.time() // 3600))
            writes[hour] = writes.get(hour, 0) + 1
            if writes[hour] > 60:
                raise HTTPException(429, "this link has made 60 changes this hour; try again later")
            # Checked before reading a byte. A chunked body's size is known only once it has all
            # come, and it wins over any Content-Length, so it is refused whatever that says.
            length = request.headers.get("content-length", "")
            chunked = "transfer-encoding" in request.headers
            if chunked or not length.isdigit() or int(length) > 64 * 1024:
                raise HTTPException(413, "send a Content-Length of at most 64 KB, not chunks")
            new = not doc_id or store.doc_get(collection, doc_id) is None
            if new and len(store.doc_list(collection, shared_with=scope.chat_key)) >= 500:
                raise PermissionError(f"{collection} already holds 500 docs for this chat")
        data = await body(request) if op == "put" else None
        by, chat = f"web:{scope.slug}", scope.chat_key
        return JSONResponse(db_access(store, op, collection, doc_id, data, by=by, chat=chat))

    async def events(request: Request) -> Response:
        stream = channel.stream(scope_of(services, request, query=True, public=public))
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
    local = [
        Route("/", static("ui.html", headers={"Content-Security-Policy": UI_CSP})),
        Route("/a/{name}", artifact),
        Route("/api/artifacts", artifacts),
        Route("/api/settings", edit_settings, methods=["GET", "PUT"]),
        Route("/api/settings/validate", edit_settings, methods=["POST"]),
        Route("/api/settings/restore", edit_settings, methods=["POST"]),
        Route("/api/messages", messages),
        Route("/api/chat", chat, methods=["POST"]),
        Route("/api/answer", answer, methods=["POST"]),
        Route("/api/stop", stop, methods=["POST"]),
        Route("/api/ask", ask, methods=["POST"]),
        Route("/mcp/{token}", mcp, methods=["POST"]),
    ]
    routes = [
        Route("/bridge.js", static("bridge.js", media_type="text/javascript")),
        *(Route(f"/{font}", static(font, headers=FONT_HEADERS)) for font in fonts),
        Route("/api/db/{collection}", db, methods=["GET", "POST"]),
        Route("/api/db/{collection}/{id}", db, methods=["GET", "PUT", "DELETE"]),
        Route("/api/events", events),
        *([Route("/s/{token}", shared)] if public else local),
    ]
    # Funnel may pass on its own Host or loopback's; either way this app has only share routes.
    hosts = [urlsplit(services.config.public_url).hostname, *LOOPBACK] if public else LOOPBACK
    headers = ["X-Attache-Token", "X-Attache-Artifact", "X-Attache-Share", "Content-Type"]
    methods = ["GET", "PUT", "DELETE", "POST"]
    middleware = [
        Middleware(TrustedHostMiddleware, allowed_hosts=hosts),
        Middleware(_BridgeCORS, allow_origins=["*"], allow_methods=methods, allow_headers=headers),
    ]
    errors = dict.fromkeys(ERRORS, _error)  # HTTPException (401, 405, 410, 415) as plain text
    return Starlette(routes=routes, middleware=middleware, exception_handlers=errors)


async def serve(app: Starlette, sock: socket.socket) -> None:
    """Run uvicorn on a socket that app.run has bound, until cancelled. Cancelled, it stops
    taking requests and gives the ones running (an /api/chat turn, say) up to 2 seconds to
    finish before they are cancelled, so they are done before app.run closes the store.
    uvicorn is kept from trapping Ctrl+C itself: asyncio.run then cancels app.run, whose
    shutdown ends the page's event streams before this one runs, so it is quick and quiet. (A
    cancel skips uvicorn's own shutdown, so the finally does it, unless startup never ended.)"""
    config = uvicorn.Config(app, log_level="warning", lifespan="off", timeout_graceful_shutdown=2)
    server = uvicorn.Server(config)
    server.capture_signals = contextlib.nullcontext  # Ctrl+C goes to asyncio.run, as above
    try:
        await server.serve(sockets=[sock])
    finally:
        if server.started and not server.should_exit:
            await server.shutdown(sockets=[sock])
