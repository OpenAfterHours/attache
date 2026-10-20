"""The bundled skills are valid Agent Skills, and the artifact templates are self-contained."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from attache import skills

KIT = skills.BUNDLED / "artifact-kit"
FOLDERS = sorted(p for p in skills.BUNDLED.iterdir() if p.is_dir() and p.name != "__pycache__")
TEMPLATES = ["home.html", "board.html"]
EXTERNAL = re.compile(r"""\b(?:src|href|action)\s*=\s*["']?\s*(?:https?:)?//""", re.I)


def test_the_core_skills_are_bundled() -> None:
    names = {p.name for p in FOLDERS}
    assert {"todos", "reminders", "skill-writer", "artifact-kit"} <= names


@pytest.mark.parametrize("folder", FOLDERS, ids=lambda p: p.name)
def test_skill_frontmatter(folder: Path) -> None:
    text = (folder / "SKILL.md").read_text(encoding="utf-8")
    meta, body = skills.parse(text)
    assert meta["name"] == folder.name
    assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", meta["name"]) and len(meta["name"]) <= 64
    description = " ".join(str(meta["description"]).split())
    assert 60 <= len(description) <= 1024
    assert "when" in description.lower()  # says when to use it, not only what it does
    assert body.lstrip().startswith("# ")
    assert len(text.splitlines()) < 120


@pytest.mark.parametrize("name", ["todos", "reminders"])
def test_member_skills(name: str) -> None:
    meta, body = skills.parse((skills.BUNDLED / name / "SKILL.md").read_text(encoding="utf-8"))
    assert meta["metadata"]["attache-roles"] == "member"
    if name == "todos":  # members get the body too, but not pages, links or file tools
        assert "## In group chats" in body and "plain text" in body


@pytest.mark.parametrize("folder", FOLDERS, ids=lambda p: p.name)
def test_skills_match_the_tools(folder: Path) -> None:
    text = (folder / "SKILL.md").read_text(encoding="utf-8")
    assert "attache.ask" not in text and '"collections"' not in text  # neither exists any more
    assert "`collections:" not in text and "id: 1" not in text  # schedule ids are strings


def test_installed_skills_are_discovered(home: Path) -> None:
    skills.install_bundled(home)
    assert [s.name for s in skills.discover(home)] == [p.name for p in FOLDERS]


@pytest.mark.parametrize("name", TEMPLATES)
def test_templates_inline_the_tokens_and_load_nothing(name: str) -> None:
    page = (KIT / "templates" / name).read_text(encoding="utf-8")
    assert (KIT / "tokens.css").read_text(encoding="utf-8").strip() in page
    assert not EXTERNAL.search(page)
    assert "@import" not in page
    urls = re.findall(r"url\(([^)]*)\)", page)  # only the fonts Attaché serves itself
    assert all(re.fullmatch(r'"/fonts/[a-z0-9-]+\.woff2"', url) for url in urls), urls
    assert "innerHTML" not in page and "localStorage" not in page
    assert '<meta name="viewport"' in page and "<title>" in page
    assert re.search(r'<meta name="attache-collections" content="[a-z_,-]+">', page)


def test_home_declares_its_collections_and_updates_live() -> None:
    page = (KIT / "templates" / "home.html").read_text(encoding="utf-8")
    meta = re.search(r'<meta name="attache-collections" content="([^"]*)">', page)
    assert meta and meta[1].split(",") == ["todos", "_tasks"]
    assert 'attache.on("todos"' in page
    assert 'attache.db.list("_tasks")' in page
    assert "No to-dos yet" in page  # the empty state says how to add the first one


def test_board_is_adaptable() -> None:
    page = (KIT / "templates" / "board.html").read_text(encoding="utf-8")
    for setting in ("const COLLECTION =", "const COLUMN =", "const COLUMNS =", "const TITLE ="):
        assert setting in page
    assert "attache.on(COLLECTION" in page


def test_tokens_are_dark_by_default_with_a_light_scheme() -> None:
    css = (KIT / "tokens.css").read_text(encoding="utf-8")
    base, light = css.split("@media (prefers-color-scheme: light)", 1)
    kit = base[base.index("/* The kit's tokens") :]
    defaults = dict(re.findall(r"(--[a-z0-9-]+):\s*([^;]+);", kit))
    overrides = dict(re.findall(r"(--[a-z0-9-]+):\s*([^;]+);", light.split("}", 1)[0]))
    assert "color-scheme: dark light" in kit
    assert (
        defaults["--bg"] == "var(--oah-slate-900)" and defaults["--text"] == "var(--oah-slate-100)"
    )
    assert (
        defaults["--accent"] == "var(--oah-orange)"
        and defaults["--ok"] == "var(--oah-terminal-green)"
    )
    assert defaults["--display"] == "var(--font-display)"  # Space Grotesk headings
    assert len(overrides) >= 8 and set(overrides) <= set(defaults)
