from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from attache.backends.claude_cli import ClaudeCLI, _subprocess, parse_events
from attache.types import Endpoint, Person, Turn, TurnContext

FIXTURE = Path(__file__).parent / "fixtures" / "claude_stream.jsonl"
STREAM = FIXTURE.read_text(encoding="utf-8").splitlines(keepends=True)
SESSION = "5b1f2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"  # the session id in the fixture
NOT_FOUND = json.dumps(
    {
        "type": "result",
        "subtype": "error_during_execution",
        "is_error": True,
        "session_id": "old",
        "errors": ["No conversation found with session ID: old"],
    }
)
ENDPOINT = Endpoint("claude", "claude-cli", "claude-sonnet-5-5", options={"bin": sys.executable})


class Gateway:
    def __init__(self) -> None:
        self.opened: list[Turn] = []
        self.closed: list[str] = []

    def open(self, turn: Turn) -> str:
        self.opened.append(turn)
        return "tok123"

    def close(self, token: str) -> None:
        self.closed.append(token)


class Runner:
    """Fake runner: records each call (and the temp files it was given), then replays lines."""

    def __init__(self, *outputs: list[str]) -> None:
        self.outputs, self.calls = list(outputs), []

    async def __call__(self, argv: list[str], stdin_text: str, cwd: Path) -> Any:
        files = {
            a: Path(a).read_text(encoding="utf-8")
            for a in argv
            if Path(a).suffix in {".json", ".md"}
        }
        self.calls.append({"argv": argv, "stdin": stdin_text, "cwd": cwd, "files": files})
        for line in self.outputs.pop(0):
            yield line


def make_turn(home: Path, role: str = "owner", session: dict[str, Any] | None = None) -> Turn:
    services = SimpleNamespace(gateway=Gateway(), web_url="http://127.0.0.1:8765", home=home)
    ctx = TurnContext("web:local", Person("p", "Phil", role), False, services)
    return Turn(ctx, ENDPOINT, "You are Attaché.", [], "Reply with: ok", [], session or {})


def test_command_without_and_with_resume(tmp_path: Path) -> None:
    cli = ClaudeCLI(ENDPOINT)
    mcp, system = tmp_path / "mcp.json", tmp_path / "system.md"
    argv = cli.command(make_turn(tmp_path), mcp, system)
    assert argv[0] == sys.executable
    assert argv[1:] == [
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        "claude-sonnet-5-5",
        "--append-system-prompt-file",
        str(system),
        "--mcp-config",
        str(mcp),
        "--strict-mcp-config",
        "--allowedTools",
        "Read,Write,Edit,Glob,Grep,Bash,WebFetch,WebSearch,mcp__attache",
    ]
    resumed = cli.command(make_turn(tmp_path, session={"session_id": "abc"}), mcp, system)
    assert resumed == [*argv, "--resume", "abc"]


def test_missing_binary_names_it(tmp_path: Path) -> None:
    endpoint = Endpoint("claude", "claude-cli", "m", options={"bin": "no-such-claude-binary"})
    with pytest.raises(RuntimeError, match="no-such-claude-binary"):
        ClaudeCLI(endpoint).command(make_turn(tmp_path), tmp_path / "a", tmp_path / "b")


def test_parse_events_on_real_stream() -> None:
    reply = parse_events(STREAM)
    assert reply.text == "ok"
    assert reply.session == {"session_id": SESSION}
    assert reply.usage == {"cost_usd": 0.00357832, "input_tokens": 2, "output_tokens": 4}


def test_parse_events_falls_back_to_assistant_text() -> None:
    lines = [
        "not json at all\n",
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "a"}]}}),
        json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use"}]}}),
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "b"}]}}),
        json.dumps({"type": "result", "subtype": "success", "is_error": False, "session_id": "s"}),
    ]
    reply = parse_events(lines)
    assert reply.text == "a\n\nb" and reply.session == {"session_id": "s"}


def test_parse_events_raises_on_error_result() -> None:
    with pytest.raises(RuntimeError, match="No conversation found"):
        parse_events([NOT_FOUND])
    with pytest.raises(RuntimeError, match="credit balance"):
        parse_events([json.dumps({"type": "result", "is_error": True, "result": "credit balance"})])


def test_only_the_owner_may_use_it(tmp_path: Path) -> None:
    runner = Runner(STREAM)
    turn = make_turn(tmp_path, role="member")
    with pytest.raises(PermissionError):
        asyncio.run(ClaudeCLI(ENDPOINT, runner).run(turn))
    assert runner.calls == [] and turn.ctx.services.gateway.opened == []


def test_run_wires_gateway_mcp_and_cleans_up(tmp_path: Path) -> None:
    runner = Runner(STREAM)
    turn = make_turn(tmp_path)
    reply = asyncio.run(ClaudeCLI(ENDPOINT, runner).run(turn))
    assert reply.text == "ok" and reply.session == {"session_id": SESSION}
    gateway = turn.ctx.services.gateway
    assert gateway.opened == [turn] and gateway.closed == ["tok123"]
    (call,) = runner.calls
    assert call["stdin"] == "Reply with: ok" and call["cwd"] == tmp_path
    mcp_path = call["argv"][call["argv"].index("--mcp-config") + 1]
    server = json.loads(call["files"][mcp_path])["mcpServers"]["attache"]
    assert server["command"] == sys.executable
    assert server["args"] == [
        "-m",
        "attache",
        "mcp",
        "--url",
        "http://127.0.0.1:8765",
        "--token",
        "tok123",
    ]
    system_path = call["argv"][call["argv"].index("--append-system-prompt-file") + 1]
    assert call["files"][system_path] == "You are Attaché."
    assert not Path(mcp_path).parent.exists()  # temp files are gone


def test_unknown_resume_session_retries_fresh(tmp_path: Path) -> None:
    runner = Runner([NOT_FOUND], STREAM)
    turn = make_turn(tmp_path, session={"session_id": "old"})
    reply = asyncio.run(ClaudeCLI(ENDPOINT, runner).run(turn))
    assert reply.session == {"session_id": SESSION}
    first, second = (c["argv"] for c in runner.calls)
    assert first[-2:] == ["--resume", "old"] and "--resume" not in second


def test_failure_still_closes_token_and_removes_files(tmp_path: Path) -> None:
    runner = Runner(['{"type":"result","is_error":true,"result":"boom"}'])
    turn = make_turn(tmp_path, session={"session_id": "old"})
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(ClaudeCLI(ENDPOINT, runner).run(turn))
    assert len(runner.calls) == 1  # not a missing session, so no retry
    assert turn.ctx.services.gateway.closed == ["tok123"]
    argv = runner.calls[0]["argv"]
    assert not Path(argv[argv.index("--mcp-config") + 1]).parent.exists()


async def _collect(argv: list[str], stdin_text: str, cwd: Path) -> list[str]:
    return [line async for line in _subprocess(argv, stdin_text, cwd)]


def test_default_runner_pipes_stdin_and_streams_lines(tmp_path: Path) -> None:
    script = "import sys, os; print(sys.stdin.read().upper()); print(os.getcwd())"
    argv = [sys.executable, "-X", "utf8", "-c", script]  # claude reads stdin as UTF-8 too
    lines = asyncio.run(_collect(argv, "héllo", tmp_path))
    assert lines[0].strip() == "HÉLLO" and Path(lines[1].strip()) == tmp_path


def test_default_runner_raises_with_stderr_when_no_result(tmp_path: Path) -> None:
    script = "import sys; sys.stderr.write('error: unknown option'); sys.exit(2)"
    with pytest.raises(RuntimeError, match="code 2: error: unknown option"):
        asyncio.run(_collect([sys.executable, "-c", script], "", tmp_path))


def test_default_runner_leaves_error_results_to_the_parser(tmp_path: Path) -> None:
    script = f"import sys; print({NOT_FOUND!r}); sys.exit(1)"
    lines = asyncio.run(_collect([sys.executable, "-c", script], "", tmp_path))
    with pytest.raises(RuntimeError, match="No conversation found"):
        parse_events(lines)
