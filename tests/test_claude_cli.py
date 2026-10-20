from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from attache.backends.claude_cli import ClaudeCLI, _spawn, parse_events, prompt
from attache.types import Endpoint, Person, Tool, Turn, TurnContext

FIXTURE = Path(__file__).parent / "fixtures" / "claude_stream.jsonl"
STREAM = FIXTURE.read_text(encoding="utf-8").splitlines(keepends=True)
SESSION = "5b1f2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"  # the session id in the fixture
WEB_URL = "http://127.0.0.1:8765"
ENDPOINT = Endpoint("claude", "claude-cli", "claude-sonnet-5-5", options={"bin": sys.executable})


def error_result(message: str) -> str:
    return json.dumps(
        {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "session_id": "old",
            "errors": [message],
        }
    )


NOT_FOUND = error_result("No conversation found with session ID: old")


async def _noop(args: dict[str, Any], ctx: TurnContext) -> str:
    return ""


TOOL_NAMES = ["read_file", "write_file", "edit_file", "run", "script", "send", "db"]
TOOLS = [Tool(name, name, {"type": "object"}, _noop) for name in TOOL_NAMES]


class Runner:
    """Fake runner: records each call, including what the gateway and temp files held while
    claude was "running", then replays the given stdout lines."""

    def __init__(self, gateway: dict[str, Turn], *outputs: list[str]) -> None:
        self.gateway = gateway
        self.outputs = list(outputs)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, argv: list[str], stdin_text: str, cwd: Path, env: dict[str, str]):
        temp = Path(flag(argv, "--mcp-config")).parent
        files = {p.name: p.read_text(encoding="utf-8") for p in temp.iterdir()}
        call = {"argv": argv, "stdin": stdin_text, "cwd": cwd, "env": env, "temp": temp}
        self.calls.append(call | {"files": files, "gateway": dict(self.gateway)})
        for line in self.outputs.pop(0):
            yield line


def make_turn(
    home: Path,
    role: str = "owner",
    session: dict[str, Any] | None = None,
    history: list[dict[str, str]] | None = None,
) -> Turn:
    services = SimpleNamespace(gateway={}, web_url=WEB_URL, home=home)
    ctx = TurnContext("local:me", Person("p", "Phil", role), False, services)
    return Turn(
        ctx, ENDPOINT, "You are Attaché.", history or [], "Reply with: ok", TOOLS, session or {}
    )


def run_turn(turn: Turn, *outputs: list[str]) -> tuple[Any, Runner]:
    runner = Runner(turn.ctx.services.gateway, *outputs)
    reply = asyncio.run(ClaudeCLI(ENDPOINT, runner).run(turn))
    return reply, runner


def flag(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


# command


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
        "--add-dir",
        str(tmp_path),
        "--allowedTools",
        "Read,Write,Edit,Glob,Grep,Bash,WebFetch,WebSearch,mcp__attache",
    ]
    resumed = cli.command(make_turn(tmp_path, session={"session_id": "abc"}), mcp, system)
    assert resumed == [*argv, "--resume", "abc"]


def test_missing_binary_names_it(tmp_path: Path) -> None:
    endpoint = Endpoint("claude", "claude-cli", "m", options={"bin": "no-such-claude-binary"})
    with pytest.raises(RuntimeError, match="no-such-claude-binary"):
        ClaudeCLI(endpoint).command(make_turn(tmp_path), tmp_path / "a", tmp_path / "b")


# parse_events


def test_parse_events_on_real_stream() -> None:
    reply = parse_events(STREAM)
    assert reply.text == "ok"
    assert reply.session == {"session_id": SESSION}
    assert reply.usage == {"cost_usd": 0.00357832, "input_tokens": 2, "output_tokens": 4}


def test_parse_events_falls_back_to_assistant_text() -> None:
    def assistant(*blocks: dict[str, Any]) -> str:
        return json.dumps({"type": "assistant", "message": {"content": list(blocks)}})

    lines = [
        "not json at all\n",
        assistant({"type": "text", "text": "a"}),
        assistant({"type": "tool_use"}),
        assistant({"type": "text", "text": "b"}),
        json.dumps({"type": "result", "subtype": "success", "is_error": False, "session_id": "s"}),
    ]
    reply = parse_events(lines)
    assert reply.text == "a\n\nb"
    assert reply.session == {"session_id": "s"}


def test_parse_events_raises_on_error_result() -> None:
    with pytest.raises(RuntimeError, match="No conversation found"):
        parse_events([NOT_FOUND])
    with pytest.raises(RuntimeError, match="credit balance"):
        parse_events([json.dumps({"type": "result", "is_error": True, "result": "credit balance"})])


# run


def test_only_the_owner_may_use_it(tmp_path: Path) -> None:
    turn = make_turn(tmp_path, role="member")
    runner = Runner(turn.ctx.services.gateway, STREAM)
    with pytest.raises(PermissionError):
        asyncio.run(ClaudeCLI(ENDPOINT, runner).run(turn))
    assert runner.calls == []


def test_owner_turns_in_groups_are_refused(tmp_path: Path) -> None:
    turn = make_turn(tmp_path)
    turn.ctx.is_group = True  # group text is untrusted; Claude Code's own tools are unconfined
    runner = Runner(turn.ctx.services.gateway, STREAM)
    with pytest.raises(PermissionError):
        asyncio.run(ClaudeCLI(ENDPOINT, runner).run(turn))
    assert runner.calls == []


def test_run_serves_tools_over_http_mcp_and_cleans_up(tmp_path: Path) -> None:
    turn = make_turn(tmp_path)
    reply, runner = run_turn(turn, STREAM)
    assert reply.text == "ok"
    assert reply.session == {"session_id": SESSION}
    (call,) = runner.calls
    ((token, served),) = call["gateway"].items()
    assert len(token) >= 32  # token_urlsafe(24)
    assert [t.name for t in served.tools] == ["script", "send", "db"]  # no duplicates of built-ins
    assert served.ctx is turn.ctx
    server = {"type": "http", "url": f"{WEB_URL}/mcp/{token}"}
    assert json.loads(call["files"]["mcp.json"]) == {"mcpServers": {"attache": server}}
    first_line, rest = call["files"]["system.md"].split("\n\n", 1)
    assert first_line == (
        "Your working directory is a scratch folder. Every relative path below is relative to "
        f"{tmp_path}; use absolute paths under it."
    )
    assert rest == "You are Attaché."
    assert turn.ctx.services.gateway == {}  # token closed
    assert not call["temp"].exists()  # the turn's temp files are gone


def test_run_uses_a_stable_cwd_outside_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _, runner = run_turn(make_turn(home), STREAM)
    _, again = run_turn(make_turn(home, session={"session_id": SESSION}), STREAM)
    cwd = runner.calls[0]["cwd"]
    assert cwd == tmp_path / "home-claude"
    assert cwd.is_dir()  # kept, so every session is in one Claude Code project
    assert again.calls[0]["cwd"] == cwd
    assert home not in cwd.parents  # so <home>/CLAUDE.md is never found walking up
    assert runner.calls[0]["temp"] != cwd
    assert flag(runner.calls[0]["argv"], "--add-dir") == str(home)


def test_env_keeps_local_traffic_off_proxies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("no_proxy", raising=False)  # on Windows this is also NO_PROXY
    monkeypatch.setenv("NO_PROXY", "corp.example")
    monkeypatch.setenv("CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD", "1")
    _, runner = run_turn(make_turn(tmp_path), STREAM)
    env = runner.calls[0]["env"]
    assert env["NO_PROXY"] == env["no_proxy"] == "corp.example,127.0.0.1,localhost,::1"
    assert env["MCP_TOOL_TIMEOUT"] == "660000"
    assert "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD" not in env
    assert env["PATH"] == os.environ["PATH"]  # otherwise the caller's environment


HISTORY = [
    {"role": "user", "content": "plan my week"},
    {"role": "assistant", "content": "Done."},
    {"role": "user", "content": "Bob: is lunch still on?"},
]


def test_fresh_session_gets_a_transcript(tmp_path: Path) -> None:
    _, runner = run_turn(make_turn(tmp_path, history=HISTORY), STREAM)
    stdin = runner.calls[0]["stdin"]
    assert stdin.startswith("Earlier messages in this chat, oldest first:\n```\n")
    assert "user: plan my week\n\nassistant: Done.\n\nuser: Bob: is lunch still on?\n```" in stdin
    assert stdin.endswith("\n\nNew message:\nReply with: ok")


def test_resumed_session_gets_only_messages_since_its_last_reply(tmp_path: Path) -> None:
    turn = make_turn(tmp_path, session={"session_id": "s1"}, history=HISTORY)
    _, runner = run_turn(turn, STREAM)
    stdin = runner.calls[0]["stdin"]
    assert stdin.startswith("Messages in this chat since your last reply, oldest first:")
    assert "Bob: is lunch still on?" in stdin
    assert "plan my week" not in stdin
    caught_up = make_turn(tmp_path, session={"session_id": "s1"}, history=HISTORY[:2])
    _, runner = run_turn(caught_up, STREAM)
    assert runner.calls[0]["stdin"] == "Reply with: ok"


def test_transcript_is_capped_and_fenced_safely(tmp_path: Path) -> None:
    def stdin(*contents: str) -> str:
        history = [{"role": "user", "content": c} for c in contents]
        return prompt(make_turn(tmp_path, history=history))

    capped = stdin(*(f"message {i}" for i in range(40)))
    assert "message 10" in capped
    assert "message 9\n" not in capped
    assert 20_000 <= len(stdin("x" * 50_000)) < 20_200
    assert "\n`````\n" in stdin("see ```code``` and ````more````")
    assert stdin() == "Reply with: ok"


@pytest.mark.parametrize(
    "error",
    [
        NOT_FOUND,
        error_result("Prompt is too long"),
        error_result("Input exceeds the context window"),
        error_result("maximum context length reached"),
    ],
)
def test_stuck_session_retries_fresh_with_transcript(tmp_path: Path, error: str) -> None:
    turn = make_turn(tmp_path, session={"session_id": "old"}, history=HISTORY)
    reply, runner = run_turn(turn, [error], STREAM)
    assert reply.session == {"session_id": SESSION}
    first, second = runner.calls
    assert first["argv"][-2:] == ["--resume", "old"]
    assert "--resume" not in second["argv"]
    assert second["stdin"].startswith("Earlier messages in this chat")


def test_other_failures_do_not_retry_and_still_clean_up(tmp_path: Path) -> None:
    turn = make_turn(tmp_path, session={"session_id": "old"})
    runner = Runner(
        turn.ctx.services.gateway, ['{"type":"result","is_error":true,"result":"boom"}']
    )
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(ClaudeCLI(ENDPOINT, runner).run(turn))
    assert len(runner.calls) == 1
    assert turn.ctx.services.gateway == {}
    assert not runner.calls[0]["temp"].exists()


@pytest.mark.parametrize(
    "message",
    [
        "API Error: 400 invalid context_management value",
        "Could not load the MCP server: missing context",
    ],
)
def test_errors_that_merely_mention_context_do_not_reset(tmp_path: Path, message: str) -> None:
    turn = make_turn(tmp_path, session={"session_id": "old"})
    runner = Runner(turn.ctx.services.gateway, [error_result(message)], STREAM)
    with pytest.raises(RuntimeError, match="context"):
        asyncio.run(ClaudeCLI(ENDPOINT, runner).run(turn))
    assert len(runner.calls) == 1


def test_no_retry_once_a_tool_has_run(tmp_path: Path) -> None:
    # The tool may have sent a message or written a file; a second run would do it again.
    tool_use = {"type": "tool_use", "id": "t1", "name": "mcp__attache__send", "input": {}}
    assistant = json.dumps({"type": "assistant", "message": {"content": [tool_use]}})
    turn = make_turn(tmp_path, session={"session_id": "old"})
    runner = Runner(turn.ctx.services.gateway, [assistant, error_result("Prompt is too long")])
    with pytest.raises(RuntimeError, match="Prompt is too long"):
        asyncio.run(ClaudeCLI(ENDPOINT, runner).run(turn))
    assert len(runner.calls) == 1


# the default runner, driven with a Python child instead of the real claude


async def _collect(argv: list[str], stdin_text: str, cwd: Path, env: dict[str, str]) -> list[str]:
    return [line async for line in _spawn(argv, stdin_text, cwd, env)]


def test_default_runner_pipes_stdin_env_and_cwd(tmp_path: Path) -> None:
    script = (
        "import sys, os; print(sys.stdin.read().upper()); print(os.getcwd()); "
        "print(os.environ['X'])"
    )
    argv = [sys.executable, "-X", "utf8", "-c", script]  # claude reads stdin as UTF-8 too
    env = {**os.environ, "X": "from-env"}
    lines = asyncio.run(_collect(argv, "héllo", tmp_path, env))
    assert lines[0].strip() == "HÉLLO"
    assert Path(lines[1].strip()) == tmp_path
    assert lines[2].strip() == "from-env"


def test_default_runner_raises_with_stderr_when_no_result(tmp_path: Path) -> None:
    script = "import sys; sys.stderr.write('error: unknown option'); sys.exit(2)"
    with pytest.raises(RuntimeError, match="code 2: error: unknown option"):
        asyncio.run(_collect([sys.executable, "-c", script], "", tmp_path, dict(os.environ)))


def test_default_runner_leaves_error_results_to_the_parser(tmp_path: Path) -> None:
    script = f"import sys; print({NOT_FOUND!r}); sys.exit(1)"
    lines = asyncio.run(_collect([sys.executable, "-c", script], "", tmp_path, dict(os.environ)))
    with pytest.raises(RuntimeError, match="No conversation found"):
        parse_events(lines)


def alive(pid: int) -> bool:
    if os.name == "nt":
        found = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True)
        return str(pid).encode() in found.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning")
def test_a_turn_that_times_out_ends_what_claude_started(tmp_path: Path) -> None:
    # claude's Bash tool starts a command that outlives it unless the whole tree is killed
    marker = tmp_path / "command.pid"
    command = f"import os, time; open({str(marker)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
    claude = "import subprocess, sys, time; "
    claude += f"subprocess.Popen([sys.executable, '-c', {command!r}]); time.sleep(60)"
    argv = [sys.executable, "-c", claude]

    async def main() -> None:
        with pytest.raises(TimeoutError):  # the router's wait_for cancels the turn
            await asyncio.wait_for(_collect(argv, "", tmp_path, dict(os.environ)), 5)

    asyncio.run(main())
    pid = int(marker.read_text(encoding="utf-8"))
    for _ in range(50):  # killed at once; reaping can take a moment
        if not alive(pid):
            break
        time.sleep(0.1)
    assert not alive(pid)
