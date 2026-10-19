"""`attache` command line. Owner: runtime. Budget ~90 lines. argparse only.

  attache                 init if there is no home yet (asks for your name), then chat
  attache init [--name N] [--endpoint E] [--home PATH]
  attache chat            run everything plus the terminal REPL
  attache run             run everything without the REPL (web, scheduler, channels)
  attache skills list | log [NAME] | undo NAME
  attache doctor          check: home exists, config parses, each endpoint's key env var or
                          `claude` binary, git, uv, web port free; one line per check, ✓ or ✗
  attache mcp --url U --token T     internal: stdio MCP server used by the claude-cli backend

--home (or $ATTACHE_HOME) picks the home folder everywhere. Returns a process exit code.
"""

from __future__ import annotations


def main(argv: list[str] | None = None) -> int:
    raise NotImplementedError
