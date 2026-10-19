"""`attache mcp`: a tiny stdio MCP server that proxies tool calls to the running Attaché.

Owner: models. Budget ~55 lines. Stdlib only (json, urllib). Newline-delimited JSON-RPC 2.0.

Handles: initialize (echo the client's protocolVersion; capabilities {"tools": {}}),
notifications/initialized (no reply), ping, tools/list, tools/call. Anything else gets
JSON-RPC error -32601.

tools/list  -> GET  {url}/api/mcp/{token}/tools  -> {"tools": [{name, description, inputSchema}]}
tools/call  -> POST {url}/api/mcp/{token}/call   {"name", "arguments"}
            -> {"content": [{"type": "text", "text": ...}], "isError": bool}
HTTP failures become an isError result, never a crash.
"""

from __future__ import annotations

from typing import IO, Any


def handle(request: dict[str, Any], url: str, token: str) -> dict[str, Any] | None:
    """One JSON-RPC message in, one response out (None for notifications)."""
    raise NotImplementedError


def serve(
    url: str, token: str, stdin: IO[str] | None = None, stdout: IO[str] | None = None
) -> None:
    """Read requests line by line until EOF, write responses, flush after each."""
    raise NotImplementedError
