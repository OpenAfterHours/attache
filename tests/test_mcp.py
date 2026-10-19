from __future__ import annotations

import io
import json
import threading
from collections.abc import Iterator
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar

import pytest

from attache import __version__, mcp

TOOLS = {"tools": [{"name": "add", "description": "Add", "inputSchema": {"type": "object"}}]}


class Gateway(BaseHTTPRequestHandler):
    """Stands in for web.py's /api/mcp routes; only token "tok" is valid."""

    seen: ClassVar[list[tuple[str, str, Any]]] = []

    def _send(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self.seen.append(("GET", self.path, None))
        if self.path == "/api/mcp/tok/tools":
            self._send(200, TOOLS)
        else:
            self._send(404, {"detail": "no"})

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.seen.append(("POST", self.path, body))
        if self.path != "/api/mcp/tok/call":
            return self._send(403, {"detail": "bad token"})
        text = f"called {body['name']} with {json.dumps(body['arguments'])}"
        self._send(200, {"content": [{"type": "text", "text": text}], "isError": False})

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def url() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    Gateway.seen = []
    yield f"http://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()
    server.server_close()


def rpc(method: str, params: dict[str, Any] | None = None, id: int = 1) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}}


def test_initialize_echoes_protocol_version() -> None:
    reply = mcp.handle(rpc("initialize", {"protocolVersion": "2025-03-26"}), "http://x", "tok")
    assert reply == {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "attache", "version": __version__},
        },
    }


def test_notifications_get_no_reply_and_ping_does() -> None:
    note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    assert mcp.handle(note, "http://x", "tok") is None
    assert mcp.handle(rpc("ping", id=7), "http://x", "tok") == {
        "jsonrpc": "2.0",
        "id": 7,
        "result": {},
    }


def test_unknown_method_is_32601() -> None:
    reply = mcp.handle(rpc("resources/list"), "http://x", "tok")
    assert reply is not None and reply["error"]["code"] == -32601


def test_tools_list_proxies_to_gateway(url: str) -> None:
    reply = mcp.handle(rpc("tools/list"), url, "tok")
    assert reply == {"jsonrpc": "2.0", "id": 1, "result": TOOLS}
    assert Gateway.seen == [("GET", "/api/mcp/tok/tools", None)]


def test_tools_call_proxies_to_gateway(url: str) -> None:
    params = {"name": "add", "arguments": {"a": 1, "b": "é"}}
    reply = mcp.handle(rpc("tools/call", params, id=3), url, "tok")
    assert reply is not None and reply["id"] == 3
    assert reply["result"] == {
        "content": [{"type": "text", "text": 'called add with {"a": 1, "b": "\\u00e9"}'}],
        "isError": False,
    }
    assert Gateway.seen == [("POST", "/api/mcp/tok/call", params)]


def test_http_failures_become_error_results(url: str) -> None:
    reply = mcp.handle(rpc("tools/call", {"name": "add"}), url, "wrong")
    assert reply is not None and reply["result"]["isError"] is True
    assert "403" in reply["result"]["content"][0]["text"]
    listed = mcp.handle(rpc("tools/list"), url, "wrong")
    assert listed is not None and "error" in listed


@pytest.mark.parametrize("error", [ConnectionRefusedError(10061, "refused"), IncompleteRead(b"")])
def test_network_errors_are_error_results(
    error: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(mcp._opener, "open", fail)
    reply = mcp.handle(rpc("tools/call", {"name": "add"}), "http://127.0.0.1:1", "tok")
    assert reply is not None and reply["result"]["isError"] is True


def test_serve_reads_lines_until_eof(url: str) -> None:
    lines = [
        rpc("initialize", {"protocolVersion": "2025-06-18"}, id=1),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        rpc("tools/list", id=2),
    ]
    stdin = io.StringIO("".join(json.dumps(m) + "\n" for m in lines) + "garbage\n\n")
    stdout = io.StringIO()
    mcp.serve(url, "tok", stdin, stdout)
    replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [r["id"] for r in replies] == [1, 2]
    assert replies[1]["result"] == TOOLS
