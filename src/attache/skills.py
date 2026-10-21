"""Find skills, build the prompt index, and keep <home> under git.

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
TRACKED = ("skills", "memory", "artifacts", "attache.toml")  # what commit() stages
AUTHOR = ("-c", "user.name=Attaché", "-c", "user.email=attache@localhost")  # no git config needed
# Run from <home>, every git command names <home>'s own repository, so a home with no .git of
# its own (one inside another repository, say) fails rather than commit or undo in the outer one.
GIT = ("git", "--git-dir=.git", "--work-tree=.")
# Never Attaché's own stdin: on Windows, git given a piped one (Git Bash, an IDE's console)
# waits at start behind the terminal chat's reader thread, so every commit would hang.
QUIET = {"stdin": subprocess.DEVNULL, "capture_output": True, "encoding": "utf-8"}
FRONTMATTER = re.compile(r"---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
SKILL_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


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
    if not isinstance(meta, dict):
        meta = {}
    return meta, text[match.end() :]


def discover(home: Path) -> list[Skill]:
    """Every valid skill under <home>/skills, sorted by name. Invalid ones are skipped."""
    found = []
    for md in (home / "skills").glob("*/SKILL.md"):
        try:
            meta, _ = parse(md.read_text(encoding="utf-8"))
        except (OSError, ValueError, yaml.YAMLError):
            meta = {}
        if not meta.get("description") or meta.get("name") != md.parent.name:
            logger.warning("skipping %s: needs a description and a name matching its folder", md)
            continue
        found.append(Skill(md.parent.name, str(meta["description"]), md.parent, meta))
    return sorted(found, key=lambda skill: skill.name)


def index(skills: list[Skill], home: Path) -> str:
    """Markdown list for the system prompt: name, description, SKILL.md path relative to home."""
    lines = []
    for skill in skills:
        description = " ".join(skill.description.split())  # one line, even if YAML folded it
        where = (skill.path / "SKILL.md").relative_to(home).as_posix()
        lines.append(f"- **{skill.name}**: {description} (`{where}`)")
    return "\n".join(lines)


def install_bundled(home: Path) -> list[Path]:
    """Copy src/attache/bundled/skills/* into <home>/skills where missing. Returns created paths."""
    (home / "skills").mkdir(parents=True, exist_ok=True)
    created = []
    for source in sorted(BUNDLED.glob("*/")):
        target = home / "skills" / source.name
        if not target.exists():
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
            created.append(target)
    return created


def git(home: Path, *args: str) -> str | None:
    """Run git in <home>'s own repository (GIT); stdout, or None when git is missing, the
    repository doesn't exist yet, or the command fails."""
    try:
        result = subprocess.run([*GIT, *args], cwd=home, **QUIET)
    except OSError:  # no git binary, or no such folder
        return None
    if result.returncode != 0:
        return None
    return result.stdout


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
    sha = git(home, "rev-parse", "--short", "HEAD")
    return sha.strip() if sha else None


def log(home: Path, name: str | None = None, limit: int = 20) -> list[str]:
    """One line per commit ("<sha> <subject>"), newest first, optionally for one skill."""
    args = ["log", f"-n{limit}", "--format=%h %s"]
    if name:
        args += ["--", f"skills/{name}"]
    out = git(home, *args)
    return out.splitlines() if out else []


def undo(home: Path, name: str) -> str | None:
    """Put skills/<name> back as it was before its latest commit, and commit "undo: <name>".

    Uncommitted changes are committed first, so undo never loses work. Returns None, leaving
    every file as it was, when the skill has no earlier version: undo never deletes a skill.
    Raises ValueError for a name that is not a skill name.
    """
    if not SKILL_NAME.fullmatch(name):
        raise ValueError(f"not a skill name: {name!r} (lowercase letters, digits and hyphens)")
    path = f"skills/{name}"
    commit(home, f"before undo: {name}")
    sha = (git(home, "log", "-n1", "--format=%H", "--", path) or "").strip()
    if not sha or git(home, "cat-file", "-e", f"{sha}~1:{path}") is None:
        return None  # never committed, or added by its latest commit (e.g. the first one)
    git(home, "rm", "-r", "-q", "-f", "--", path)  # drops files the latest commit added
    git(home, "checkout", f"{sha}~1", "--", path)
    return commit(home, f"undo: {name}")
