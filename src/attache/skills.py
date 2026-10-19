"""Find skills, build the prompt index, and keep <home> under git. Owner: foundation. ~80 lines.

A skill is <home>/skills/<name>/SKILL.md with YAML frontmatter (Agent Skills standard:
`name`, `description`, optional `metadata`). Only names and descriptions reach the prompt;
the agent reads the full file when it needs it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Skill:
    name: str
    description: str
    path: Path  # the skill's folder
    meta: dict[str, Any] = field(default_factory=dict)  # the whole frontmatter


def parse(text: str) -> tuple[dict[str, Any], str]:
    """Split SKILL.md text into (frontmatter, body). No frontmatter gives ({}, text)."""
    raise NotImplementedError


def discover(home: Path) -> list[Skill]:
    """Every valid skill under <home>/skills, sorted by name. Invalid ones are skipped."""
    raise NotImplementedError


def index(skills: list[Skill], home: Path) -> str:
    """Markdown list for the system prompt: name, description, SKILL.md path relative to home."""
    raise NotImplementedError


def install_bundled(home: Path) -> list[Path]:
    """Copy src/attache/bundled/skills/* into <home>/skills where missing. Returns created paths."""
    raise NotImplementedError


def git(home: Path, *args: str) -> str | None:
    """Run git in <home>; stdout, or None when git is missing or the command fails."""
    raise NotImplementedError


def commit(home: Path, message: str) -> str | None:
    """Stage skills/, memory/, artifacts/ and attache.toml; commit if anything changed.

    Returns the short sha, or None when there was nothing to commit or git is unavailable.
    """
    raise NotImplementedError


def log(home: Path, name: str | None = None, limit: int = 20) -> list[str]:
    """One line per commit ("<sha> <subject>"), newest first, optionally for one skill."""
    raise NotImplementedError


def undo(home: Path, name: str) -> str | None:
    """Put skills/<name> back as it was before its latest commit, and commit "undo: <name>"."""
    raise NotImplementedError
