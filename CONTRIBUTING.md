# Contributing

Attaché stays small so anyone can read the core in an afternoon and change it with confidence.

**Accepted in the core:** bug fixes, security fixes, simplifications, less code.

**Not accepted in the core:** features, integrations, channels, file formats. These should be
skills. (This rule comes from [nanoclaw](https://github.com/nanocoai/nanoclaw).)

## Adding a skill

A skill is a folder with a `SKILL.md` in the [Agent Skills](https://agentskills.io/specification)
format:

```
skills/<name>/
  SKILL.md          frontmatter: name, description, optional metadata
  scripts/*.py      optional; PEP 723 header declares dependencies, run with `uv run`
  templates/        optional; artifact templates
```

Metadata keys Attaché reads:

- `attache-roles: "member"` lets group members use the skill's scripts (owner-only otherwise).
- `attache-channel: "<command>"` marks a channel sidecar (phase 2).

Bundled skills live in `src/attache/bundled/skills/` and are copied into a user's home by
`attache init`. Each bundled skill's scripts are capped at 300 lines by `tests/test_budget.py`.

## Before you open a pull request

```
uv run ruff check . && uv run ruff format --check .
uv run pytest
```
