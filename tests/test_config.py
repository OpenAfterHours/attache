from __future__ import annotations

import re
import shutil
import tomllib
from pathlib import Path

import pytest

from attache import config, skills

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

TOML = """
[assistant]
name = "Jeeves"

[owner]
name = "Alex"
[owner.ids]
slack = "UALEX"
signal = ""

[routing]
own_chats = "local"
group_chats = "claude-api"

[web]
port = 9000

[endpoints.local]
api = "openai-chat"
base_url = "http://localhost:11434/v1"
model = "qwen3:32b"
max_tokens = 2048

[endpoints.claude-api]
api = "anthropic"
model = "claude-sonnet-5-5"
key_env = "ANTHROPIC_API_KEY"

[people.sam]
name = "Sam"
slack = "USAM"

[people.kim]
name = "Kim"
role = "guest"
signal = "+447700900123"

[chats."slack:C0123"]
name = "Family"
engage = "always"
members = ["sam"]
endpoint = "local"

[chats."slack:C0456"]
name = "Work"
"""


@pytest.fixture
def cfg(home: Path) -> config.Config:
    (home / "attache.toml").write_text(TOML, encoding="utf-8")
    return config.load(home)


def test_home_dir_uses_env(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert config.home_dir() == home
    monkeypatch.delenv("ATTACHE_HOME")
    assert config.home_dir() == Path.home() / ".attache"


def test_missing_file_gives_template_defaults(home: Path) -> None:
    cfg = config.load()
    assert cfg.home == home
    assert (cfg.owner.id, cfg.owner.name, cfg.owner.role) == ("owner", "you", "owner")
    assert cfg.owner.ids == {}  # the template's empty slack id is dropped
    assert (cfg.own_chats, cfg.group_chats) == ("claude", "claude-api")
    assert (cfg.host, cfg.port, cfg.name) == ("127.0.0.1", 8765, "Attaché")
    assert set(cfg.endpoints) == {"claude", "claude-api"}
    assert cfg.endpoints["claude"].api == "claude-cli"
    api = cfg.endpoints["claude-api"]
    assert (api.api, api.model, api.key_env, api.options) == (
        "anthropic",
        "claude-sonnet-5-5",
        "ANTHROPIC_API_KEY",
        {},
    )
    assert cfg.people == {} and cfg.chats == {}


def test_template_examples_parse_when_uncommented() -> None:
    text = (config.BUNDLED / "attache.toml").read_text(encoding="utf-8")
    text = re.sub(r"^# (\[|\w+ = )", r"\1", text, flags=re.M)
    raw = tomllib.loads(text.replace("{owner}", "Alex").replace("{endpoint}", "claude"))
    assert raw["endpoints"]["openai"]["api"] == "openai-responses"
    assert raw["endpoints"]["local"]["base_url"] == "http://localhost:11434/v1"
    assert raw["people"]["sam"] == {"name": "Sam", "role": "member", "slack": "U0123"}
    assert raw["chats"]["slack:C0123"]["members"] == ["sam"]


def test_load_parses_everything(cfg: config.Config) -> None:
    assert cfg.name == "Jeeves" and cfg.port == 9000 and cfg.host == "127.0.0.1"
    assert cfg.owner.name == "Alex" and cfg.owner.ids == {"slack": "UALEX"}
    sam, kim = cfg.people["sam"], cfg.people["kim"]
    assert (sam.id, sam.name, sam.role, sam.ids) == ("sam", "Sam", "member", {"slack": "USAM"})
    assert kim.role == "guest" and kim.ids == {"signal": "+447700900123"}
    local = cfg.endpoints["local"]
    assert (local.api, local.base_url, local.model) == ("openai-chat", local.base_url, "qwen3:32b")
    assert local.options == {"max_tokens": 2048}
    family = cfg.chat("slack:C0123")
    assert family and family.members == ["sam"] and family.engage == "always"
    work = cfg.chat("slack:C0456")
    assert work and (work.engage, work.guests, work.endpoint) == ("mention", "ignore", None)
    assert cfg.chat("slack:nope") is None


def test_person(cfg: config.Config) -> None:
    assert cfg.person("terminal", "anything") is cfg.owner
    assert cfg.person("web", "") is cfg.owner
    assert cfg.person("slack", "UALEX") is cfg.owner
    assert cfg.person("slack", "USAM") is cfg.people["sam"]
    assert cfg.person("signal", "+447700900123") is cfg.people["kim"]
    assert cfg.person("signal", "USAM") is None  # ids are per channel
    assert cfg.person("slack", "UNKNOWN") is None
    assert cfg.person("signal", "") is None  # the owner's empty signal id matches nobody


def test_endpoint_for(cfg: config.Config) -> None:
    assert cfg.endpoint_for("slack:C0123", is_group=True).name == "local"  # chat override
    assert cfg.endpoint_for("slack:C0456", is_group=True).name == "claude-api"
    assert cfg.endpoint_for("terminal:local", is_group=False).name == "local"
    cfg.own_chats = "gone"
    with pytest.raises(ValueError, match="gone"):
        cfg.endpoint_for("terminal:local", is_group=False)


def test_init_home_creates_a_home(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(skills, "BUNDLED", home.parent / "no-bundled-skills")
    created = config.init_home(home, 'Alex "Al" O\\Neil', endpoint="claude-api")
    names = {p.relative_to(home).as_posix() for p in created}
    assert names == {"attache.toml", "memory/USER.md", "memory/ATTACHE.md", ".secret", ".gitignore"}
    for folder in ("memory/chats", "skills", "artifacts", "files", "logs"):
        assert (home / folder).is_dir()
    assert not (home / "artifacts" / "home.html").exists()
    cfg = config.load(home)
    assert cfg.owner.name == 'Alex "Al" O\\Neil' and cfg.own_chats == "claude-api"
    assert "{owner}" not in (home / "memory" / "USER.md").read_text(encoding="utf-8")
    assert "Alex" in (home / "memory" / "ATTACHE.md").read_text(encoding="utf-8")
    assert re.fullmatch(r"[0-9a-f]{64}", (home / ".secret").read_text(encoding="utf-8"))
    ignored = (home / ".gitignore").read_text(encoding="utf-8").split()
    assert ignored == ["attache.db*", ".secret", "files/", "logs/"]


def test_init_home_never_overwrites(home: Path) -> None:
    (home / "attache.toml").write_text('[owner]\nname = "Mine"\n', encoding="utf-8")
    created = config.init_home(home, "Alex")
    assert home / "attache.toml" not in created
    assert config.load(home).owner.name == "Mine"
    secret = (home / ".secret").read_text(encoding="utf-8")
    assert config.init_home(home, "Alex") == []
    assert (home / ".secret").read_text(encoding="utf-8") == secret


def test_init_home_copies_skills_and_home_page(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundled = home.parent / "bundled"
    page = bundled / "artifact-kit" / "templates" / "home.html"
    page.parent.mkdir(parents=True)
    page.write_text("<h1>Home</h1>", encoding="utf-8")
    monkeypatch.setattr(skills, "BUNDLED", bundled)
    created = config.init_home(home, "Alex")
    assert home / "skills" / "artifact-kit" in created
    assert (home / "artifacts" / "home.html").read_text(encoding="utf-8") == "<h1>Home</h1>"


@needs_git
def test_init_home_commits(home: Path) -> None:
    config.init_home(home, "Alex")
    assert [line.split(" ", 1)[1] for line in skills.log(home)] == ["attache init"]
    tracked = (skills.git(home, "ls-files") or "").split()
    assert "attache.toml" in tracked and "memory/USER.md" in tracked
    assert ".secret" not in tracked
    author = skills.git(home, "log", "-1", "--format=%an <%ae>") or ""
    assert author.strip() == "Attaché <attache@localhost>"
