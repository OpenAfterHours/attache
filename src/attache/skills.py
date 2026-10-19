"""Find skills, build the prompt index, and keep <home> under git. Owner: foundation. ~80 lines.

A skill is <home>/skills/<name>/SKILL.md with YAML frontmatter (Agent Skills standard:
`name`, `description`, optional `metadata`). Only names and descriptions reach the prompt;
the agent reads the full file when it needs it.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)
BUNDLED = Path(__file__).parent / "bundled" / "skills"
TRACKED = ("skills", "memory", "artifacts", "attache.toml")
AUTHOR = ("-c", "user.name=Attaché", "-c", "user.email=attache@localhost")
FRONTMATTER = re.compile(r"---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)


@dataclass
class Skill:
    name: str
    description: str
    path: Path  # the skill's folder
    meta: dict[str, Any] = field(default_factory=dict)  # the whole frontmatter


def parse(text: str) -> tuple[dict[str, Any], str]:
    """Split SKILL.md text into (frontmatter, body). No frontmatter gives ({}, text)."""
    match = FRONTMATTER.match(text)
    if not match:
        return {}, text
    meta = yaml.safe_load(match.group(1))
    return (meta if isinstance(meta, dict) else {}), text[match.end() :]


def discover(home: Path) -> list[Skill]:
    """Every valid skill under <home>/skills, sorted by name. Invalid ones are skipped."""
    found = []
    for md in (home / "skills").glob("*/SKILL.md"):
        try:
            meta = parse(md.read_text(encoding="utf-8"))[0]
        except (OSError, ValueError, yaml.YAMLError):
            meta = {}
        if not meta.get("description") or meta.get("name") != md.parent.name:
            logger.warning("skipping %s: needs a description and a name matching its folder", md)
            continue
        found.append(Skill(md.parent.name, str(meta["description"]), md.parent, meta))
    return sorted(found, key=lambda s: s.name)


def index(skills: list[Skill], home: Path) -> str:
    """Markdown list for the system prompt: name, description, SKILL.md path relative to home."""
    return "\n".join(
        f"- **{s.name}**: {' '.join(s.description.split())} "
        f"(`{(s.path / 'SKILL.md').relative_to(home).as_posix()}`)"
        for s in skills
    )


def install_bundled(home: Path) -> list[Path]:
    """Copy src/attache/bundled/skills/* into <home>/skills where missing. Returns created paths."""
    (home / "skills").mkdir(parents=True, exist_ok=True)
    created = []
    for src in sorted(BUNDLED.glob("*/")):
        dest = home / "skills" / src.name
        if not dest.exists():
            shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__"))
            created.append(dest)
    return created


def git(home: Path, *args: str) -> str | None:
    """Run git in <home>; stdout, or None when git is missing or the command fails."""
    try:
        out = subprocess.run(
            ["git", *args], cwd=home, capture_output=True, text=True, encoding="utf-8"
        )
    except OSError:  # no git binary, or no such folder
        return None
    return None if out.returncode else out.stdout


def commit(home: Path, message: str) -> str | None:
    """Stage skills/, memory/, artifacts/ and attache.toml; commit if anything changed.

    Returns the short sha, or None when there was nothing to commit or git is unavailable.
    """
    paths = [p for p in TRACKED if (home / p).exists()]  # git add fails on a missing path
    if not paths or git(home, "add", "-A", "--", *paths) is None:
        return None
    if git(home, "diff", "--cached", "--quiet") is not None:  # exit 0: nothing staged
        return None
    if git(home, *AUTHOR, "commit", "-q", "-m", message) is None:
        return None
    return (git(home, "rev-parse", "--short", "HEAD") or "").strip() or None


def log(home: Path, name: str | None = None, limit: int = 20) -> list[str]:
    """One line per commit ("<sha> <subject>"), newest first, optionally for one skill."""
    only = ["--", f"skills/{name}"] if name else []
    return (git(home, "log", f"-n{limit}", "--format=%h %s", *only) or "").splitlines()


def undo(home: Path, name: str) -> str | None:
    """Put skills/<name> back as it was before its latest commit, and commit "undo: <name>"."""
    path = f"skills/{name}"
    sha = (git(home, "log", "-n1", "--format=%H", "--", path) or "").strip()
    if not sha:
        return None
    git(home, "rm", "-r", "-q", "-f", "--", path)  # also drops files that commit added
    git(home, "checkout", f"{sha}~1", "--", path)  # fails if the skill is new: it stays removed
    return commit(home, f"undo: {name}")
