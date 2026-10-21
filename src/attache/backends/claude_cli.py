"""Claude Code as a backend: `claude -p` runs its own tool loop and reaches our tools over MCP.

Only the owner's turns may use it, because a Claude subscription login is for your own use
(Anthropic's terms). The message goes on stdin, which avoids Windows command-line length
limits. claude runs in a private directory next to <home> ("<home>-claude"), with <home> added
by --add-dir, so a CLAUDE.md or .claude/settings.json (hooks) planted in <home> is never loaded
as project config; Claude Code looks for those in its working directory and the ones above it.
That directory stays the same across turns so every session is in one Claude Code project and
--resume finds it. The user's own ~/.claude config still applies; that is the owner's choice.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import shutil
import tempfile
from collections.abc import AsyncIterator, Iterable
from dataclasses import replace
from pathlib import Path
from typing import Any

from attache.agent import TOKENS, label, progress
from attache.channels import PIPE, SPAWN, kill_tree
from attache.types import Endpoint, Reply, Turn, TurnContext

OWNER_BUILTINS = ["Read", "Write", "Edit", "Glob", "Grep", "Bash", "WebFetch", "WebSearch"]
# Our tools that Claude Code's own Read/Write/Edit/Bash already cover, so they are not served.
DUPLICATE_TOOLS = {"read_file", "write_file", "edit_file", "run"}
# Errors a fresh session fixes: an unknown --resume id, or a conversation past the context limit.
RESET_ON = ("no conversation found", "prompt is too long", "context window", "context length")
SCRATCH = "Your working directory is a scratch folder. Every relative path below is relative to"


class StaleSessionError(RuntimeError):
    """The resumed session can't go on, and no tool ran, so a fresh session can safely retry."""


async def _spawn(argv: list[str], text: str, cwd: Path, env: dict[str, str]) -> AsyncIterator[str]:
    # Its own process group, like a sidecar's (channels.SPAWN: piped stdin and stdout, long
    # lines), so kill_tree also ends the commands its Bash tool started.
    proc = await asyncio.create_subprocess_exec(*argv, cwd=cwd, env=env, stderr=PIPE, **SPAWN)
    try:
        stderr = asyncio.ensure_future(proc.stderr.read())  # drain so a full pipe can't block it
        with contextlib.suppress(ConnectionError):  # it exited early; stderr says why
            proc.stdin.write(text.encode("utf-8"))
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
    finally:  # done, failed, or cancelled (the turn timed out): nothing it started lives on
        await kill_tree(proc)


def _env() -> dict[str, str]:
    """os.environ, with our local MCP traffic kept off proxies and room for 10-minute tools."""
    skip = os.environ.get("NO_PROXY") or os.environ.get("no_proxy")
    no_proxy = ",".join(filter(None, [skip, "127.0.0.1,localhost,::1"]))
    env = {**os.environ, "NO_PROXY": no_proxy, "no_proxy": no_proxy, "MCP_TOOL_TIMEOUT": "660000"}
    env.pop("CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD", None)  # keep <home>'s CLAUDE.md out
    return env


def prompt(turn: Turn) -> str:
    """turn.text after a fenced transcript of the history this Claude session hasn't seen: all
    of it (the last 30 messages, 20,000 characters) when fresh, else what followed its last reply.
    """
    history, label = turn.history, "Earlier messages in this chat"
    replies = [i for i, m in enumerate(history) if m["role"] == "assistant"]
    if turn.session.get("session_id") and replies:
        history, label = history[replies[-1] + 1 :], "Messages in this chat since your last reply"
    body = "\n\n".join(f"{m['role']}: {m['content']}" for m in history[-30:])[-20_000:]
    if not body:
        return turn.text
    fence = "```"
    while fence in body:  # longer than any run of backticks inside, so the text can't close it
        fence += "`"
    return f"{label}, oldest first:\n{fence}\n{body}\n{fence}\n\nNew message:\n{turn.text}"


class ClaudeCLI:
    def __init__(self, endpoint: Endpoint, runner: Any = None) -> None:
        """runner(argv, stdin_text, cwd, env) -> async iterable of stdout lines; tests pass one."""
        self.endpoint, self.runner = endpoint, runner or _spawn

    def command(self, turn: Turn, mcp_config: Path, system_file: Path) -> list[str]:
        name = self.endpoint.options.get("bin", "claude")
        if not (exe := shutil.which(name)):
            raise RuntimeError(f"endpoint '{self.endpoint.name}': claude binary {name!r} not found")
        argv = [exe, "-p", "--output-format", "stream-json", "--verbose"]
        argv += ["--model", self.endpoint.model, "--append-system-prompt-file", str(system_file)]
        argv += ["--mcp-config", str(mcp_config), "--strict-mcp-config"]
        argv += ["--add-dir", str(turn.ctx.services.home)]
        argv += ["--allowedTools", ",".join([*OWNER_BUILTINS, "mcp__attache"])]
        if session_id := turn.session.get("session_id"):
            argv += ["--resume", session_id]
        return argv

    async def run(self, turn: Turn) -> Reply:
        if turn.ctx.role != "owner" or turn.ctx.is_group:  # groups carry untrusted text
            raise PermissionError("the claude-cli backend only answers the owner's own chats")
        services = turn.ctx.services
        token = secrets.token_urlsafe(24)
        cwd = services.home.parent / f"{services.home.name}-claude"  # outside <home>: see above
        cwd.mkdir(exist_ok=True)
        files = Path(tempfile.mkdtemp(prefix="attache-"))  # this turn's MCP config and prompt
        try:
            tools = [t for t in turn.tools if t.name not in DUPLICATE_TOOLS]
            services.gateway[token] = replace(turn, tools=tools)
            server = {"type": "http", "url": f"{services.web_url}/mcp/{token}"}
            config = json.dumps({"mcpServers": {"attache": server}})
            (files / "mcp.json").write_text(config, encoding="utf-8")
            system = f"{SCRATCH} {services.home}; use absolute paths under it.\n\n{turn.system}"
            (files / "system.md").write_text(system, encoding="utf-8")
            try:
                return await self._once(turn, files, cwd)
            except StaleSessionError:
                if not turn.session.get("session_id"):
                    raise
                return await self._once(replace(turn, session={}), files, cwd)  # full transcript
        finally:
            services.gateway.pop(token, None)
            shutil.rmtree(files, ignore_errors=True)

    async def _once(self, turn: Turn, files: Path, cwd: Path) -> Reply:
        argv = self.command(turn, files / "mcp.json", files / "system.md")
        lines = []
        async for line in self.runner(argv, prompt(turn), cwd, _env()):
            lines.append(line)
            await _report(turn.ctx, _event(line))  # as it comes, not once claude is done
        return parse_events(lines)


def _event(line: str) -> dict[str, Any]:
    """One line of claude's stream-json output, or {} when it is not a JSON object."""
    with contextlib.suppress(ValueError):
        if isinstance(event := json.loads(line), dict):
            return event
    return {}


def _blocks(event: dict[str, Any]) -> list[dict[str, Any]]:
    """An assistant event's content blocks; none for any other event, or a malformed one, so a
    bad event never fails the turn."""
    message = event.get("message")
    if event.get("type") != "assistant" or not isinstance(message, dict):
        return []
    return [block for block in message.get("content") or [] if isinstance(block, dict)]


async def _report(ctx: TurnContext, event: dict[str, Any]) -> None:
    """Claude Code's thinking, and its own tools as steps (agent.progress; seconds None, as the
    stream doesn't say). Its calls to Attaché's tools (mcp__...) are left out: they come
    through the MCP endpoint, where run_tool reports them."""
    for block in _blocks(event):
        kind, name = block.get("type"), str(block.get("name"))
        if kind == "thinking":
            await progress(ctx, "thinking", text=str(block.get("thinking", "")))
        elif kind == "tool_use" and not name.startswith("mcp__"):
            await progress(ctx, "step", label=label(name, block.get("input")), seconds=None)


def parse_events(lines: Iterable[str]) -> Reply:
    """Reply.text from the final "result" event (fall back to joined assistant text blocks),
    Reply.session = {"session_id": ...}, Reply.usage = {"cost_usd": ..., ...}. A result with
    is_error true raises RuntimeError with its message (StaleSessionError when a fresh session would
    fix it and no tool has run yet, since running tools twice could repeat a send or a write).
    Non-JSON lines are ignored."""
    events = [_event(line) for line in lines]
    result = next((e for e in reversed(events) if e.get("type") == "result"), {})
    blocks = [block for event in events for block in _blocks(event)]
    if result.get("is_error"):
        message = result.get("result") or "; ".join(result.get("errors") or ["claude failed"])
        used_tools = any(b.get("type") == "tool_use" for b in blocks)
        stale = not used_tools and any(s in message.lower() for s in RESET_ON)
        raise (StaleSessionError if stale else RuntimeError)(message)
    usage = {k: (result.get("usage") or {}).get(k) for k in TOKENS}
    usage["cost_usd"] = result.get("total_cost_usd")
    text = result.get("result") or "\n\n".join(b["text"] for b in blocks if b.get("type") == "text")
    return Reply(text, {"session_id": result.get("session_id")}, usage)
