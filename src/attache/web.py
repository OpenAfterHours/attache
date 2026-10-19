"""The local web server: chat page, artifacts, the data bridge, live events, and the tool
gateway that CLI backends reach through `attache mcp`. Owner: web. Budget ~180 lines.

Routes (Starlette):
  GET  /                       static/ui.html (chat, artifact list, Home)
  GET  /bridge.js              static/bridge.js
  GET  /a/{name}               artifacts/<name>.html with, injected before </head> (or at the
                               top): <script src="/bridge.js" data-token="<artifact token>">
                               and a CSP header: default-src 'self' 'unsafe-inline' data:
                               https://cdnjs.cloudflare.com https://cdn.jsdelivr.net
                               https://fonts.googleapis.com https://fonts.gstatic.com;
                               connect-src 'self'
  GET  /api/artifacts          [{name, title, url}] (owner UI)
  POST /api/chat     {text}    an Inbound on channel "web", chat "local", sender "owner";
                               returns {"ok": true} at once, the reply arrives over SSE
  GET  /api/events?token=      SSE. Events: "message" {chat_key, text, files} for the web chat;
                               "doc" {collection, id, doc} for collections the token may read
  GET  /api/db/{collection}            list   } header X-Attache-Token: an artifact token or
  GET  /api/db/{collection}/{id}       get    } the UI token. Same rules as the db tool: a
  PUT  /api/db/{collection}/{id}       put    } token reaches only its artifact's collections,
  DELETE /api/db/{collection}/{id}     delete } and a group artifact only that chat's shared
                                                docs. 403 otherwise.
  POST /api/ask      {prompt}  artifact asks the agent: an Inbound in the artifact's chat
  GET  /api/mcp/{token}/tools  ToolGateway.list
  POST /api/mcp/{token}/call   ToolGateway.call

Tokens are HMAC-SHA256(services.secret, ...) hex strings, compared with hmac.compare_digest.
The UI token is artifact_token(secret, "_ui") and is embedded in ui.html when served. The
server binds config.host (127.0.0.1 by default), so only this machine can reach it in phase 1.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from attache.types import OnMessage, Outbound, Turn

if TYPE_CHECKING:
    from starlette.applications import Starlette

    from attache.app import Services
    from attache.router import Router


def artifact_token(secret: bytes, name: str) -> str:
    raise NotImplementedError


def sign_link(secret: bytes, path: str, expires: float) -> str:
    """'<path>?e=<expires>&s=<hmac>' for sharing a page outside this machine (phase 3)."""
    raise NotImplementedError


def verify_link(secret: bytes, path: str, expires: str, signature: str, now: float) -> bool:
    raise NotImplementedError


class ToolGateway:
    """Short-lived tokens that expose one turn's tools over HTTP."""

    def open(self, turn: Turn) -> str:
        raise NotImplementedError

    def close(self, token: str) -> None:
        raise NotImplementedError

    def list(self, token: str) -> list[dict[str, Any]]:
        """MCP tool descriptors: {"name", "description", "inputSchema"}. Unknown token -> []."""
        raise NotImplementedError

    async def call(self, token: str, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        """(text, is_error). Unknown token or tool -> an error result, not an exception."""
        raise NotImplementedError


class WebChannel:
    """Chat key "web:local". send() pushes a "message" event to every open page."""

    name = "web"

    async def start(self, on_message: OnMessage) -> None:
        raise NotImplementedError

    async def send(self, msg: Outbound) -> None:
        raise NotImplementedError

    async def stop(self) -> None:
        raise NotImplementedError


def create_app(services: Services, router: Router, channel: WebChannel) -> Starlette:
    raise NotImplementedError


async def serve(app: Starlette, host: str, port: int) -> None:
    """Run uvicorn in this event loop until cancelled. Port in use -> OSError."""
    raise NotImplementedError
