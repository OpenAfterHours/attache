---
icon: lucide/git-pull-request
---

# Contributing

Attaché stays small so anyone can read the core in an afternoon and change it with
confidence.

**Accepted in the core:** bug fixes, security fixes, simplifications, less code.

**Not accepted in the core:** features, integrations, channels, file formats. These should be
skills. (This rule comes from [nanoclaw](https://github.com/nanocoai/nanoclaw).)

The source is at [OpenAfterHours/attache](https://github.com/OpenAfterHours/attache). The
design is in the [proposal](../design.md); read its architecture and code budget sections
first.

## Dev setup

You need [uv](https://docs.astral.sh/uv/) and git. Then:

```bash
uv sync                                               # the dev environment
uv run pytest                                         # all tests, including the size budget
uv run ruff check . && uv run ruff format --check .   # lint and format
uv run attache --help                                 # the CLI, from source
```

To try your changes without touching your real `~/.attache`, give every command a scratch
home:

```bash
uv run attache init --home ./scratch-home --name Dev
uv run attache --home ./scratch-home
```

## Layout

```text
src/attache/
  types.py      shared dataclasses and protocols; every module talks through these
  app.py        builds Services and runs everything
  config.py     attache.toml -> Config; init_home()
  store.py      SQLite: messages, docs, tasks, sessions
  skills.py     skill discovery, prompt index, git commit/log/undo of <home>
  scheduler.py  fires due tasks
  agent.py      shared tool loop for API backends
  backends/     anthropic, openai (chat + responses), claude_cli
  tools.py      the tools a turn may use, filtered by role
  router.py     inbound message -> access, trigger, role -> turn -> reply
  channels.py   channel manager, terminal channel
  web.py        Starlette app: chat page, artifacts, data bridge, SSE, MCP endpoint
  cli.py        argparse entry point
  static/       ui.html, bridge.js
  bundled/      attache.toml template, memory templates, bundled skills (copied by init)
```

## Rules

1. **Size is a test.** `tests/test_budget.py` fails above 1,900 lines of code in
   `src/attache` (the target is about 1,850). Blank lines, comments and docstrings don't
   count, so never make code denser to fit: write the readable version. If a feature doesn't
   fit, it belongs in a skill.
2. **No new runtime dependencies** without discussion. Skill scripts declare their own with
   PEP 723 headers and run through `uv run`.
3. **Tests run offline.** No network, no API keys, no real `claude` binary. Inject fakes:
   every backend takes a `client` or `runner` argument for this.
4. **Cross-platform.** Windows, macOS and Linux. Use pathlib, `encoding="utf-8"` on every
   text read and write, argument lists for subprocesses (no shell, except in the `run` tool),
   and no POSIX-only APIs.
5. **Style.** ruff with the repo config, `from __future__ import annotations`, type hints on
   public functions, short docstrings on public functions only, and comments only for a
   non-obvious why. Plain functions and dataclasses over class hierarchies.
6. **Security model.** Changes must keep the rules in [Security](../security.md): roles and
   their tools, fenced untrusted text, sandboxed pages, loopback only, and `claude-cli` for
   the owner's own chats only.
7. **Contracts live in `types.py`** and the module docstrings. Change a public signature only
   together with every caller and its tests.

## Adding a skill

A skill is a folder with a `SKILL.md` in the
[Agent Skills](https://agentskills.io/specification) format:

```text
skills/<name>/
  SKILL.md          frontmatter: name, description, optional metadata
  scripts/*.py      optional; PEP 723 header declares dependencies, run with `uv run`
  templates/        optional; artifact templates
```

Metadata keys Attaché reads:

- `attache-roles: "member"` makes the skill available in group chats: members see its full
  text in their prompt and may run its scripts. Without it a skill is owner-only.
- `attache-channel: "<command>"` marks a channel sidecar (phase 2).

Bundled skills live in `src/attache/bundled/skills/` and are copied into a user's home by
`attache init`. Each bundled skill's scripts are capped at 300 lines of code by
`tests/test_budget.py`. The [Skills guide](../skills.md) covers writing one in full.

## The size budget

`tests/test_budget.py` fails when the core passes 1,900 lines of code. Blank lines, comments
and docstrings don't count, so write the clearest version of the code, not the shortest.
Bundled skills, static files and tests don't count toward it.

## Working on these docs

The site is built with [Zensical](https://zensical.org) from `docs/` and `zensical.toml`, in
the OpenAfterHours night-sky theme shared with the other OpenAfterHours projects. The `docs`
dependency group has what it needs:

```bash
uv run --group docs zensical serve            # preview at http://localhost:8000, live reload
uv run --group docs zensical build --strict   # the static site, in ./site, as CI builds it
```

`--strict` fails the build on a link to a missing page or heading.

- Pages are Markdown files in `docs/`. Add a new one to `nav` in `zensical.toml`.
- `docs/assets/stylesheets/oah-theme.css` holds the theme. Its brand tokens are copied from
  rwa_calculator's `tokens.css`, the single source of truth for the OpenAfterHours palette:
  change them there first.
- `docs/proposal.html` is the design proposal, a standalone HTML page that the build copies
  as it is.

Every pull request builds the site, and every push to `main` publishes it to GitHub Pages.

## Before you open a pull request

```bash
uv run ruff check . && uv run ruff format --check .
uv run pytest
```

CI runs the same checks, the tests on Windows, macOS and Linux with Python 3.11, 3.12 and
3.13, and a smoke test of the built wheel. It also sets `UV_LOCKED=1`, so after changing a
dependency, run `uv lock` and commit `uv.lock`.
