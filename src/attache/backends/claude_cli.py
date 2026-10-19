"""Claude Code as a backend: `claude -p` runs its own loop. Owner: models. Budget ~100 lines.

Per turn:
  1. Open a tool-gateway token (services.gateway.open(turn)) so our tools are reachable.
  2. Write a temp MCP config naming one stdio server, "attache":
     [sys.executable, "-m", "attache", "mcp", "--url", services.web_url, "--token", token].
  3. Write turn.system to a temp file for --append-system-prompt-file.
  4. Run: <bin> -p --output-format stream-json --verbose --model <model>
          --append-system-prompt-file <file> --mcp-config <file> --strict-mcp-config
          --allowedTools <owner built-ins + mcp__attache> [--resume <session_id>]
     with cwd = home, and the user's text on stdin (avoids Windows command-line limits).
  5. Parse stream-json lines; the final "result" event carries the text and session_id.
  6. Close the gateway token and delete the temp files, even on error.

Only the owner's turns may use this backend (Anthropic's terms: a subscription login is for
your own use). Raise PermissionError for any other role. A missing `claude` binary raises
RuntimeError naming endpoint.options["bin"] (default "claude", resolved with shutil.which).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import sys
import tempfile
from collections.abc import AsyncIterator, Iterable
from dataclasses import replace
from pathlib import Path
from typing import Any

from attache.types import Endpoint, Reply, Turn

OWNER_BUILTINS = ["Read", "Write", "Edit", "Glob", "Grep", "Bash", "WebFetch", "WebSearch"]
NO_SESSION = "No conversation found"  # what claude says when --resume names an unknown session


async def _subprocess(argv: list[str], stdin_text: str, cwd: Path) -> AsyncIterator[str]:
    pipe = asyncio.subprocess.PIPE
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=cwd, stdin=pipe, stdout=pipe, stderr=pipe, limit=2**24
    )
    try:
        stderr = asyncio.ensure_future(proc.stderr.read())  # drain so a full pipe can't block it
        with contextlib.suppress(ConnectionError):  # it exited early; stderr says why
            proc.stdin.write(stdin_text.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
        got_result = False
        async for raw in proc.stdout:
            line = raw.decode("utf-8", "replace")
            got_result = got_result or '"type":"result"' in line.replace(" ", "")
            yield line
        code = await proc.wait()
        if code and not got_result:
            tail = (await stderr).decode("utf-8", "replace").strip()[-2000:]
            raise RuntimeError(f"claude exited with code {code}: {tail}")
    finally:
        if proc.returncode is None:
            proc.kill()


class ClaudeCLI:
    def __init__(self, endpoint: Endpoint, runner: Any = None) -> None:
        """runner(argv, stdin_text, cwd) -> async iterable of stdout lines; tests pass a fake."""
        self.endpoint, self.runner = endpoint, runner or _subprocess

    def command(self, turn: Turn, mcp_config: Path, system_file: Path) -> list[str]:
        name = self.endpoint.options.get("bin", "claude")
        if not (exe := shutil.which(name)):
            raise RuntimeError(f"endpoint '{self.endpoint.name}': claude binary {name!r} not found")
        argv = [exe, "-p", "--output-format", "stream-json", "--verbose"]
        argv += ["--model", self.endpoint.model, "--append-system-prompt-file", str(system_file)]
        argv += ["--mcp-config", str(mcp_config), "--strict-mcp-config"]
        argv += ["--allowedTools", ",".join([*OWNER_BUILTINS, "mcp__attache"])]
        if session_id := turn.session.get("session_id"):
            argv += ["--resume", session_id]
        return argv

    async def run(self, turn: Turn) -> Reply:
        if turn.ctx.role != "owner":
            raise PermissionError("the claude-cli backend only answers the owner's own turns")
        services = turn.ctx.services
        token = services.gateway.open(turn)
        tmp = Path(tempfile.mkdtemp(prefix="attache-"))
        try:
            mcp_config, system_file = tmp / "mcp.json", tmp / "system.md"
            args = ["-m", "attache", "mcp", "--url", services.web_url, "--token", token]
            server = {"type": "stdio", "command": sys.executable, "args": args}
            mcp_config.write_text(json.dumps({"mcpServers": {"attache": server}}), encoding="utf-8")
            system_file.write_text(turn.system, encoding="utf-8")
            try:
                return await self._once(turn, mcp_config, system_file)
            except RuntimeError as e:
                if not turn.session.get("session_id") or NO_SESSION not in str(e):
                    raise
                return await self._once(replace(turn, session={}), mcp_config, system_file)
        finally:
            services.gateway.close(token)
            shutil.rmtree(tmp, ignore_errors=True)

    async def _once(self, turn: Turn, mcp_config: Path, system_file: Path) -> Reply:
        argv, home = self.command(turn, mcp_config, system_file), turn.ctx.services.home
        return parse_events([line async for line in self.runner(argv, turn.text, home)])


def parse_events(lines: Iterable[str]) -> Reply:
    """Reply.text from the final "result" event (fall back to joined assistant text blocks),
    Reply.session = {"session_id": ...}, Reply.usage = {"cost_usd": ..., ...}. A result with
    is_error true raises RuntimeError with its message. Non-JSON lines are ignored."""
    events = []
    for line in lines:
        with contextlib.suppress(ValueError):
            events.append(json.loads(line))
    events = [e for e in events if isinstance(e, dict)]
    result = next((e for e in reversed(events) if e.get("type") == "result"), {})
    if result.get("is_error"):
        errors = "; ".join(result.get("errors") or [])
        raise RuntimeError(result.get("result") or errors or result.get("subtype", "claude failed"))
    texts = [
        b.get("text", "")
        for e in events
        if e.get("type") == "assistant"
        for b in (e.get("message") or {}).get("content") or []
        if b.get("type") == "text"
    ]
    u = result.get("usage") or {}
    usage = {"cost_usd": result.get("total_cost_usd")}
    usage |= {k: u.get(k) for k in ("input_tokens", "output_tokens")}
    text = result.get("result") or "\n\n".join(texts)
    return Reply(text, {"session_id": result.get("session_id")}, usage)
