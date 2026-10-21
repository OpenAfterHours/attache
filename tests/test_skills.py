from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from attache import config, skills

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def write_skill(home: Path, folder: str, front: str, body: str = "Body.\n") -> Path:
    path = home / "skills" / folder / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{front}\n---\n{body}", encoding="utf-8")
    return path


def test_parse() -> None:
    meta, body = skills.parse("---\nname: pdf\ndescription: Make PDFs\n---\n# PDF\nText\n")
    assert meta == {"name": "pdf", "description": "Make PDFs"} and body == "# PDF\nText\n"
    meta, body = skills.parse("---\r\nname: pdf\r\nmetadata:\r\n  a: 1\r\n---\r\nBody")
    assert meta == {"name": "pdf", "metadata": {"a": 1}} and body == "Body"
    assert skills.parse("# No frontmatter\n---\nname: x\n---\n") == (
        {},
        "# No frontmatter\n---\nname: x\n---\n",
    )
    assert skills.parse("---\njust a string\n---\nBody") == ({}, "Body")


def test_discover_skips_invalid(home: Path, caplog: pytest.LogCaptureFixture) -> None:
    write_skill(home, "todos", "name: todos\ndescription: Lists and habits")
    write_skill(home, "docx", 'name: docx\ndescription: "Word files"\nmetadata:\n  x: 1')
    write_skill(home, "nodesc", "name: nodesc")
    write_skill(home, "wrongname", "name: other\ndescription: Mismatch")
    write_skill(home, "badyaml", "name: [unclosed\ndescription: x")
    (home / "skills" / "nofile").mkdir()
    with caplog.at_level(logging.WARNING, logger="attache.skills"):
        found = skills.discover(home)
    assert [s.name for s in found] == ["docx", "todos"]
    assert found[0].path == home / "skills" / "docx"
    assert found[0].meta["metadata"] == {"x": 1} and found[0].description == "Word files"
    warned = caplog.text
    assert "nodesc" in warned and "wrongname" in warned and "badyaml" in warned
    assert "nofile" not in warned
    assert skills.discover(home / "elsewhere") == []


def test_index(home: Path) -> None:
    write_skill(home, "todos", "name: todos\ndescription: >\n  Lists,\n  habits")
    write_skill(home, "pdf", "name: pdf\ndescription: Make PDFs")
    assert skills.index(skills.discover(home), home) == (
        "- **pdf**: Make PDFs (`skills/pdf/SKILL.md`)\n"
        "- **todos**: Lists, habits (`skills/todos/SKILL.md`)"
    )
    assert skills.index([], home) == ""


def test_install_bundled(home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("pdf", "todos"):
        write_skill(tmp_path, name, f"name: {name}\ndescription: d")
    bundled = tmp_path / "skills"
    (bundled / "pdf" / "scripts").mkdir()
    (bundled / "pdf" / "scripts" / "make.py").write_text("print(1)\n", encoding="utf-8")
    (bundled / "pdf" / "__pycache__").mkdir()
    write_skill(home, "todos", "name: todos\ndescription: mine")
    monkeypatch.setattr(skills, "BUNDLED", bundled)
    assert skills.install_bundled(home) == [home / "skills" / "pdf"]
    assert (home / "skills" / "pdf" / "scripts" / "make.py").exists()
    assert not (home / "skills" / "pdf" / "__pycache__").exists()
    assert "mine" in (home / "skills" / "todos" / "SKILL.md").read_text(encoding="utf-8")
    assert skills.install_bundled(home) == []
    monkeypatch.setattr(skills, "BUNDLED", tmp_path / "missing")
    assert skills.install_bundled(home) == []


def test_git_missing(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_git(*args: Any, **kwargs: Any) -> None:
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", no_git)
    assert skills.git(home, "status") is None
    assert skills.commit(home, "x") is None
    assert skills.log(home) == []
    assert skills.undo(home, "pdf") is None


def test_git_never_gets_attaches_stdin(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # On Windows, git given a piped stdin waits behind the terminal chat's reader thread.
    seen: list[dict[str, Any]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(kwargs)
        return subprocess.CompletedProcess(argv, 0, "ok\n", "")

    monkeypatch.setattr(subprocess, "run", run)
    assert skills.git(home, "status") == "ok\n"
    assert seen[0]["stdin"] is subprocess.DEVNULL and seen[0]["cwd"] == home


@needs_git
def test_git_failure_and_no_repo(home: Path) -> None:
    assert skills.git(home, "no-such-command") is None
    (home / "memory").mkdir()
    (home / "memory" / "USER.md").write_text("me", encoding="utf-8")
    assert skills.commit(home, "not a repo") is None
    assert skills.log(home) == []


@needs_git
def test_git_never_touches_a_repository_around_the_home(tmp_path: Path) -> None:
    parent, home = tmp_path / "parent", tmp_path / "parent" / "home"
    write_skill(home, "pdf", "name: pdf\ndescription: v1")
    assert skills.git(parent, "init", "-b", "main") is not None  # a repository around the home
    assert skills.commit(home, "add pdf") is None  # the home has none of its own: nothing done
    assert skills.log(home) == [] and skills.undo(home, "pdf") is None
    assert skills.git(parent, "status", "--porcelain") == "?? home/\n"  # nothing staged there
    assert skills.git(home, "init", "-b", "main") is not None  # init still makes the home's own
    assert "worktree" not in (home / ".git" / "config").read_text(encoding="utf-8")
    assert skills.commit(home, "add pdf") is not None and len(skills.log(home)) == 1
    assert skills.log(parent) == []  # still no commit in the outer repository


@pytest.fixture
def repo(home: Path) -> Path:
    skills.git(home, "init", "-b", "main")
    (home / "attache.toml").write_text("", encoding="utf-8")
    (home / "files").mkdir()
    (home / "files" / "big.bin").write_text("not tracked", encoding="utf-8")
    return home


@needs_git
def test_commit_and_log(repo: Path) -> None:
    write_skill(repo, "pdf", "name: pdf\ndescription: v1")
    sha = skills.commit(repo, "add pdf")
    assert sha and len(sha) >= 7
    assert skills.commit(repo, "nothing changed") is None
    (repo / "memory").mkdir()
    (repo / "memory" / "USER.md").write_text("Alex", encoding="utf-8")
    assert skills.commit(repo, "remember Alex: café") is not None
    assert [line.split(" ", 1)[1] for line in skills.log(repo)] == [
        "remember Alex: café",
        "add pdf",
    ]
    assert skills.log(repo, "pdf") == [f"{sha} add pdf"]
    assert len(skills.log(repo, limit=1)) == 1
    assert "files/big.bin" not in (skills.git(repo, "ls-files") or "")


@needs_git
def test_undo_restores_previous_version(repo: Path) -> None:
    md = write_skill(repo, "pdf", "name: pdf\ndescription: v1")
    skills.commit(repo, "add pdf")
    write_skill(repo, "pdf", "name: pdf\ndescription: v2")
    (repo / "skills" / "pdf" / "extra.md").write_text("new file", encoding="utf-8")
    write_skill(repo, "todos", "name: todos\ndescription: t")
    skills.commit(repo, "change pdf, add todos")
    assert skills.undo(repo, "pdf") is not None
    assert "v1" in md.read_text(encoding="utf-8")
    assert not (repo / "skills" / "pdf" / "extra.md").exists()
    assert (repo / "skills" / "todos" / "SKILL.md").exists()  # other skills untouched
    assert skills.log(repo)[0].endswith(" undo: pdf")
    assert skills.git(repo, "status", "--porcelain", "--", "skills") == ""


@needs_git
def test_undo_never_deletes_a_skill(repo: Path) -> None:
    write_skill(repo, "pdf", "name: pdf\ndescription: v1")
    skills.commit(repo, "first")
    write_skill(repo, "todos", "name: todos\ndescription: t")
    skills.commit(repo, "add todos")
    before = skills.log(repo)
    assert skills.undo(repo, "todos") is None  # added by its latest commit: nothing earlier
    assert skills.undo(repo, "pdf") is None  # only in the root commit
    assert skills.undo(repo, "never-existed") is None
    assert (repo / "skills" / "todos" / "SKILL.md").exists()
    assert (repo / "skills" / "pdf" / "SKILL.md").exists()
    assert skills.log(repo) == before


@needs_git
def test_undo_keeps_bundled_skills_on_a_fresh_home(home: Path) -> None:
    config.init_home(home, "Alex")
    assert (home / "skills" / "todos" / "SKILL.md").exists()
    assert skills.undo(home, "todos") is None
    assert (home / "skills" / "todos" / "SKILL.md").exists()
    assert len(skills.log(home)) == 1


@needs_git
def test_undo_commits_uncommitted_edits_first(repo: Path) -> None:
    md = write_skill(repo, "pdf", "name: pdf\ndescription: v1")
    skills.commit(repo, "add pdf")
    write_skill(repo, "pdf", "name: pdf\ndescription: v2")
    skills.commit(repo, "pdf v2")
    write_skill(repo, "pdf", "name: pdf\ndescription: v3, not committed yet")
    assert skills.undo(repo, "pdf") is not None
    assert "v2" in md.read_text(encoding="utf-8")  # back to before the latest change
    subjects = [line.split(" ", 1)[1] for line in skills.log(repo)]
    assert subjects[:2] == ["undo: pdf", "before undo: pdf"]
    kept = skills.git(repo, "show", "HEAD~1:skills/pdf/SKILL.md") or ""
    assert "v3, not committed yet" in kept  # still in history


@needs_git
def test_undo_cannot_reach_outside_skills(repo: Path) -> None:
    user = repo / "memory" / "USER.md"
    user.parent.mkdir()
    user.write_text("v1", encoding="utf-8")
    skills.commit(repo, "memory v1")
    user.write_text("v2", encoding="utf-8")
    skills.commit(repo, "memory v2")
    with pytest.raises(ValueError, match="not a skill name"):
        skills.undo(repo, "../memory")
    assert user.read_text(encoding="utf-8") == "v2"


@pytest.mark.parametrize("name", ["..", "Pdf", "a/b", "a\\b", "-x", "", "x" * 65])
def test_undo_rejects_bad_names_before_running_git(
    home: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(skills, "git", lambda home, *args: calls.append(args))
    with pytest.raises(ValueError, match="not a skill name"):
        skills.undo(home, name)
    assert calls == []
    assert skills.SKILL_NAME.fullmatch("pdf-tools-2")
