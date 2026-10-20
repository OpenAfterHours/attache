# Attaché: notes for agents and contributors

Attaché is a personal assistant packaged as a Python tool (`uvx attache`). The core is small on
purpose. Channels, email, file formats and integrations are **skills**, not core code. The
design is in `docs/proposal.html`; read §3 (architecture) and §10 (code budget) first.

## Commands

```
uv sync                      # install dev environment
uv run pytest                # all tests, including the size budget
uv run ruff check . && uv run ruff format --check .
uv run attache --help
uv run --group docs zensical serve   # docs site (zensical.toml, docs/); build with --strict
```

The web UI, the bundled page kit and the docs site all use the OpenAfterHours design tokens
(`src/attache/static/oah-tokens.css`, vendored from rwa_calculator; a test guards drift). Fonts
are self-hosted in `src/attache/static/fonts/` (SIL OFL), so nothing loads from the internet.

## Layout

```
src/attache/
  types.py      shared dataclasses and protocols; every module talks through these
  app.py        builds Services and runs everything (and the public share server, if set)
  config.py     attache.toml -> Config; init_home(); restrict() keeps the home yours
  store.py      SQLite: messages, docs, tasks, sessions
  skills.py     skill discovery, prompt index, git commit/log/undo of <home> (pinned to
                <home>/.git)
  scheduler.py  fires due tasks
  agent.py      shared tool loop for API backends
  backends/     anthropic, openai (chat + responses), claude_cli
  tools.py      the tools a turn may use, filtered by role; ask (with mail approvals),
                request (survives a restart: _requests), page turns get db only
  router.py     inbound message -> access, trigger, role -> turn -> reply; page_turn()
  channels.py   channel manager (send, ask and answer, turn tasks), terminal channel,
                sidecar runner for skill channels (protocol: docs/channels.md)
  web.py        Starlette app: chat page, artifacts, data bridge, SSE, /api/ask for pages,
                MCP endpoint for Claude Code (POST /mcp/{token}); with public=True the
                share-links-only app on public_port (/s/{token}, for Tailscale Funnel)
  cli.py        argparse entry point; `attache skill NAME [ARGS]` runs a skill's script
  static/       ui.html, bridge.js
  bundled/      attache.toml template, memory templates, bundled skills (copied by init:
                mail, mailbox, docx, xlsx, pptx, service, slack, signal, render, ...)
```

## Rules

1. **Size is a test.** `tests/test_budget.py` fails above 2,100 lines of code in
   `src/attache` (target about 2,060; 1,600 in phase 1, 1,900 in phase 2, raised by the owner
   for phase 3's sharing and page turns). Blank lines, comments and docstrings don't count, so
   never make code denser to fit: write the readable version. Bundled skills are excluded;
   each bundled skill's scripts have a 300-line cap (1,350 for `mail`, which has three
   backends). If a feature doesn't fit, it belongs in a skill.
2. **No new runtime dependencies** without discussion. Skill scripts declare their own with
   PEP 723 headers and run through `uv run`.
3. **Tests run offline.** No network, no API keys, no real `claude` binary. Inject fakes
   (every backend takes a `client`/`runner` argument for this).
4. **Cross-platform.** Windows, macOS and Linux. Use pathlib, `encoding="utf-8"` on every text
   read and write, argument lists for subprocesses (no shell unless the `run` tool), and no
   POSIX-only APIs.
5. **Style.** ruff with the repo config, `from __future__ import annotations`, type hints on
   public functions, short docstrings on public functions only, comments only for a
   non-obvious why. Plain functions and dataclasses over class hierarchies.
6. **Security model.**
   - Roles are owner, member and guest (router.py). Only the owner's own chats get file, shell
     and memory tools; an owner turn inside a group gets member tools, because group context is
     untrusted. Non-owner turns never use the `claude-cli` backend (Anthropic's terms).
   - Members act only on docs shared with their chat. Other people's messages reach the model
     fenced and labelled as data; display names are never trusted.
   - Text from tools, emails and web pages is untrusted data, never instructions.
   - Artifact pages are hostile HTML: served with a CSP sandbox (opaque origin, no outbound
     requests) and only inside the Attaché window. The owner's UI token is random per run and
     only reaches the browser through the link Attaché prints.
   - The web server binds loopback only in phase 1. Tokens are compared with
     `hmac.compare_digest`; page tokens derive from the per-run UI token, so they rotate too.
   - `claude -p` runs in `<home>-claude`, outside the home, so files in `<home>` never load as
     Claude Code project config.
7. **Contracts live in `types.py`** and the module docstrings. Change a public signature only
   together with every caller and its tests.
