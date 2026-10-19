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

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from attache.types import Endpoint, Reply, Turn

OWNER_BUILTINS = ["Read", "Write", "Edit", "Glob", "Grep", "Bash", "WebFetch", "WebSearch"]


class ClaudeCLI:
    def __init__(self, endpoint: Endpoint, runner: Any = None) -> None:
        """runner(argv, stdin_text, cwd) -> async iterable of stdout lines; tests pass a fake."""
        raise NotImplementedError

    def command(self, turn: Turn, mcp_config: Path, system_file: Path) -> list[str]:
        raise NotImplementedError

    async def run(self, turn: Turn) -> Reply:
        raise NotImplementedError


def parse_events(lines: Iterable[str]) -> Reply:
    """Reply.text from the final "result" event (fall back to joined assistant text blocks),
    Reply.session = {"session_id": ...}, Reply.usage = {"cost_usd": ..., ...}. A result with
    is_error true raises RuntimeError with its message. Non-JSON lines are ignored."""
    raise NotImplementedError
