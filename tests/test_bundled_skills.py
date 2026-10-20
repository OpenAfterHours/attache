"""The bundled skills are valid Agent Skills, and the artifact templates are self-contained."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from attache import skills

KIT = skills.BUNDLED / "artifact-kit"
FOLDERS = sorted(p for p in skills.BUNDLED.iterdir() if p.is_dir() and p.name != "__pycache__")
TEMPLATES = ["home.html", "board.html"]
EXTERNAL = re.compile(r"""\b(?:src|href|action)\s*=\s*["']?\s*(?:https?:)?//""", re.I)


def test_the_core_skills_are_bundled() -> None:
    names = {p.name for p in FOLDERS}
    assert {"todos", "reminders", "skill-writer", "artifact-kit", "render"} <= names


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


@pytest.mark.parametrize("name", ["todos", "reminders", "render"])
def test_member_skills(name: str) -> None:
    meta, body = skills.parse((skills.BUNDLED / name / "SKILL.md").read_text(encoding="utf-8"))
    assert meta["metadata"]["attache-roles"] == "member"
    if name == "todos":  # members get the body too: lists as text, pictures, and `request`
        assert "## In group chats" in body and "plain text" in body
        assert "`_shared`" in body and "render" in body and "`request" in body
    if name == "render":  # the snapshot flow, end to end
        for step in ("`snapshot`", '"render.py"', "send {", "Never send the `snapshot` link"):
            assert step in body, step


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


# the render skill's script, without a real browser


def load_render() -> ModuleType:
    path = skills.BUNDLED / "render" / "scripts" / "render.py"
    spec = importlib.util.spec_from_file_location("attache_render", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SNAPSHOT = "http://127.0.0.1:8765/a/home?t=abc&r=def"


def test_render_script_has_a_pep_723_header_and_no_dependencies() -> None:
    text = (skills.BUNDLED / "render" / "scripts" / "render.py").read_text(encoding="utf-8")
    header = re.search(r"# /// script\n(.*?)# ///", text, re.S)
    assert header and "dependencies = []" in header[1]


def test_render_arguments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    render = load_render()
    monkeypatch.chdir(tmp_path)
    args = render.parse([SNAPSHOT, "../../elsewhere/page.png", "--width", "390", "--height", "844"])
    assert args.out == tmp_path / "page.png"  # always into the current folder
    assert (args.width, args.height, args.wait) == (390, 844, 4)
    for bad in (
        ["file:///C:/Users/you/.ssh/id_rsa", "x.png"],  # only web pages
        ["https://example.com/", "x.png"],  # only this machine
        ["http://127.0.0.1.attacker.example/", "x.png"],
        [SNAPSHOT, "page.html"],  # only a picture or a PDF
    ):
        with pytest.raises(SystemExit):
            render.parse(bad)


def test_render_finds_edge_or_chrome(monkeypatch: pytest.MonkeyPatch) -> None:
    render = load_render()
    on_path = {"google-chrome": "/usr/bin/google-chrome", "msedge": "C:/Edge/msedge.exe"}
    monkeypatch.setattr(render.shutil, "which", on_path.get)
    assert render.find_browser() == "C:/Edge/msedge.exe"  # Edge first
    monkeypatch.setattr(render.shutil, "which", lambda name: None)
    monkeypatch.setattr(render, "PATHS", [])
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert render.find_browser() is None


def test_render_builds_an_offline_headless_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    render = load_render()
    monkeypatch.chdir(tmp_path)
    png = render.command("edge", render.parse([SNAPSHOT, "page.png"]), "C:/tmp/profile")
    assert png[0] == "edge" and png[-1] == SNAPSHOT
    for flag in (
        "--headless=new",
        "--user-data-dir=C:/tmp/profile",
        "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1",
        "--timeout=4000",
        f"--screenshot={tmp_path / 'page.png'}",
        "--window-size=1280,900",
    ):
        assert flag in png, flag
    assert not any(arg.startswith("--virtual-time-budget") for arg in png)  # hangs on SSE pages
    pdf = render.command("edge", render.parse([SNAPSHOT, "page.pdf", "--wait", "8"]), "p")
    assert f"--print-to-pdf={tmp_path / 'page.pdf'}" in pdf and "--timeout=8000" in pdf


def test_render_checks_the_page_first(monkeypatch: pytest.MonkeyPatch) -> None:
    render = load_render()
    seen = []

    class Opener:
        def open(self, request: Any, timeout: float) -> Any:
            seen.append(request.get_header("Sec-fetch-dest"))
            raise render.urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(render.urllib.request, "build_opener", lambda *handlers: Opener())
    problem = render.check(SNAPSHOT)
    assert problem and problem.startswith("the page answered 403") and seen == ["document"]


def test_render_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    render = load_render()
    chat = tmp_path / "home" / "files" / "slack-c0123"
    chat.mkdir(parents=True)
    monkeypatch.chdir(chat)
    monkeypatch.setattr(render, "find_browser", lambda: None)
    assert render.main([SNAPSHOT, "page.png"]) == 2
    assert "no Edge or Chrome" in capsys.readouterr().err

    def browser(argv: list[str], **options: Any) -> SimpleNamespace:  # writes what it is asked
        shot = next(arg for arg in argv if arg.startswith("--screenshot="))
        Path(shot.split("=", 1)[1]).write_bytes(b"\x89PNG")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(render, "find_browser", lambda: "edge")
    monkeypatch.setattr(render.subprocess, "run", browser)
    monkeypatch.setattr(render, "check", lambda url: "the page answered 403")
    assert render.main([SNAPSHOT, "page.png"]) == 1  # no picture of an error page
    assert "403" in capsys.readouterr().err and not (chat / "page.png").exists()
    monkeypatch.setattr(render, "check", lambda url: None)
    assert render.main([SNAPSHOT, "page.png"]) == 0
    assert capsys.readouterr().out.strip() == "files/slack-c0123/page.png"  # what `send` takes
    monkeypatch.setattr(
        render.subprocess, "run", lambda argv, **o: SimpleNamespace(stdout="", stderr="crashed")
    )
    assert render.main([SNAPSHOT, "again.png"]) == 1
    assert "crashed" in capsys.readouterr().err
