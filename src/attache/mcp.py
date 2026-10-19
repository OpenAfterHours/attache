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

import json
import sys
import urllib.request
from typing import IO, Any

from attache import __version__

# Attaché listens on this machine, so never route through a system or environment proxy.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _fetch(url: str, body: dict[str, Any] | None, timeout: float) -> Any:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with _opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def handle(request: dict[str, Any], url: str, token: str) -> dict[str, Any] | None:
    """One JSON-RPC message in, one response out (None for notifications)."""
    if "id" not in request:
        return None
    method, params = request.get("method"), request.get("params") or {}
    base = f"{url.rstrip('/')}/api/mcp/{token}"
    result: Any = {}
    if method == "initialize":
        version = params.get("protocolVersion", "2025-06-18")
        info = {"name": "attache", "version": __version__}
        result = {"protocolVersion": version, "capabilities": {"tools": {}}, "serverInfo": info}
    elif method == "tools/list":
        try:
            result = _fetch(f"{base}/tools", None, 10)
        except Exception as e:
            error = {"code": -32603, "message": f"attache is not reachable: {e}"}
            return {"jsonrpc": "2.0", "id": request["id"], "error": error}
    elif method == "tools/call":
        body = {"name": params.get("name"), "arguments": params.get("arguments") or {}}
        try:
            result = _fetch(f"{base}/call", body, 300)
        except Exception as e:
            text = f"error: attache is not reachable: {e}"
            result = {"content": [{"type": "text", "text": text}], "isError": True}
    elif method != "ping":
        error = {"code": -32601, "message": f"method not found: {method}"}
        return {"jsonrpc": "2.0", "id": request["id"], "error": error}
    return {"jsonrpc": "2.0", "id": request["id"], "result": result}


def serve(
    url: str, token: str, stdin: IO[str] | None = None, stdout: IO[str] | None = None
) -> None:
    """Read requests line by line until EOF, write responses, flush after each."""
    if stdin is None:
        sys.stdin.reconfigure(encoding="utf-8")  # Windows pipes default to the ANSI code page
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    for line in stdin:
        try:
            request = json.loads(line)
        except ValueError:
            continue
        if isinstance(request, dict) and (reply := handle(request, url, token)) is not None:
            stdout.write(json.dumps(reply) + "\n")  # ASCII-only, so stdout's encoding can't matter
            stdout.flush()
