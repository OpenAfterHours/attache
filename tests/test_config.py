from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from attache import config, skills

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

TOML = """
[assistant]
name = "Jeeves"

[owner]
name = "Alex"
signal = "+447700900000"
[owner.ids]
slack = "UALEX"
telegram = ""

[routing]
own_chats = "local"
group_chats = "claude-api"

[web]
port = "9000"

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
[people.kim.ids]
signal = "+447700900123"

[chats."slack:C0123"]
name = "Family"
engage = "always"
members = ["sam"]
endpoint = "local"

[chats."slack:C0456"]
name = "Work"
"""


def write(home: Path, text: str) -> Path:
    path = home / "attache.toml"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def cfg(home: Path) -> config.Config:
    write(home, TOML)
    return config.load(home)


@pytest.fixture
def no_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """init_home without running git, for tests that check only the files."""
    monkeypatch.setattr(skills, "git", lambda home, *args: None)


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
    assert cfg.people == {}
    assert cfg.chats == {}
    assert (cfg.channels, cfg.approvals) == ({}, "local:me")
    assert cfg.limits == {"per_sender_hour": 20, "per_chat_hour": 60, "member_daily": 200}
    cfg.limits["member_daily"] = 1
    assert config.LIMITS["member_daily"] == 200  # each Config has its own copy


def test_channels_limits_and_approvals(home: Path) -> None:
    write(
        home,
        '[owner]\nname = "Alex"\napprovals = "slack:D0ALEXDM"\n[owner.ids]\nslack = "U0ALEX"\n'
        '[channels.slack]\nenabled = true\n[channels.signal]\nnumber = "+447700900123"\n'
        "[limits]\nper_chat_hour = 30\n",
    )
    cfg = config.load(home)
    assert cfg.approvals == "slack:D0ALEXDM"
    assert cfg.owner.ids == {"slack": "U0ALEX"}  # approvals is not a channel id
    assert cfg.channels == {
        "slack": {"enabled": True},
        "signal": {"enabled": False, "number": "+447700900123"},  # off until enabled
    }
    assert cfg.limits == {"per_sender_hour": 20, "per_chat_hour": 30, "member_daily": 200}
    assert cfg.person("slack", "U0ALEX") is cfg.owner
    assert cfg.person("local", "anyone") is cfg.owner


def test_template_examples_load_when_uncommented(home: Path) -> None:
    text = (config.BUNDLED / "attache.toml").read_text(encoding="utf-8")
    text = re.sub(r"^# (\[|\w+ = )", r"\1", text, flags=re.M)  # uncomment the examples
    write(home, text.replace("{owner}", "Alex").replace("{endpoint}", "claude"))
    cfg = config.load(home)
    assert cfg.endpoints["openai"].api == "openai-responses"
    assert cfg.endpoints["local"].base_url == "http://localhost:11434/v1"
    sam = cfg.people["sam"]
    assert (sam.name, sam.role, sam.ids) == ("Sam", "member", {"slack": "U0123"})
    family = cfg.chats["slack:C0123"]
    assert (family.name, family.members, family.endpoint) == ("Family", ["sam"], "claude-api")
    assert cfg.approvals == "slack:D0ALEXDM"
    assert set(cfg.owner.ids) == {"slack", "signal"}
    assert cfg.channels == {
        "slack": {"enabled": True},
        "signal": {"enabled": True, "number": "+447700900123"},
    }
    assert cfg.limits == config.LIMITS  # the examples show the defaults


def test_load_parses_everything(cfg: config.Config) -> None:
    assert (cfg.name, cfg.port, cfg.host) == ("Jeeves", 9000, "127.0.0.1")  # port coerced
    assert cfg.owner.name == "Alex"
    assert cfg.owner.ids == {"slack": "UALEX", "signal": "+447700900000"}
    sam, kim = cfg.people["sam"], cfg.people["kim"]
    assert (sam.id, sam.name, sam.role, sam.ids) == ("sam", "Sam", "member", {"slack": "USAM"})
    assert kim.role == "guest"
    assert kim.ids == {"signal": "+447700900123"}  # from [people.kim.ids]
    local = cfg.endpoints["local"]
    assert (local.api, local.model) == ("openai-chat", "qwen3:32b")
    assert local.options == {"max_tokens": 2048}
    family = cfg.chat("slack:C0123")
    assert family is not None
    assert (family.members, family.engage) == (["sam"], "always")
    work = cfg.chat("slack:C0456")
    assert work is not None
    assert (work.engage, work.guests, work.endpoint) == ("mention", "ignore", None)
    assert cfg.chat("slack:nope") is None


def test_load_accepts_a_byte_order_mark(home: Path) -> None:
    (home / "attache.toml").write_text('﻿[owner]\nname = "Alex"\n', encoding="utf-8")
    assert config.load(home).owner.name == "Alex"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[web]\nprot = 1\n", r"unknown key prot in \[web\]; known: host, port"),
        ('[routing]\nown = "x"\n', r"unknown key own in \[routing\]"),
        ('[assistant]\nnick = "x"\n', r"unknown key nick in \[assistant\]"),
        ('[chats."slack:C1"]\nengage = "always"\nmember = []\n', r'\[chats\."slack:C1"\]'),
        ('[web]\nport = "eighty"\n', r"\[web\] port must be a number, not 'eighty'"),
        ("[owner\n", r"attache\.toml: "),
        (
            "[limits]\nper_day = 5\n",
            r"unknown key per_day in \[limits\]; known: per_sender_hour, per_chat_hour, member",
        ),
        ("[channels.signal]\nnumber = 447700900123\n", r"\[channels\.signal\] number: wrong type"),
        ('[channels.slack]\nenabled = "yes"\n', r"\[channels\.slack\] enabled: wrong type"),
        ("[channels.slack]\nenabled = true\nport = 8080\n", r"\[channels\.slack\] port: "),
    ],
)
def test_load_errors_name_the_file_and_section(home: Path, text: str, message: str) -> None:
    path = write(home, text)
    with pytest.raises(ValueError, match=message) as error:
        config.load(home)
    assert str(path) in str(error.value)


def test_person(cfg: config.Config) -> None:
    assert cfg.person("local", "owner") is cfg.owner
    assert cfg.person("slack", "UALEX") is cfg.owner
    assert cfg.person("signal", "+447700900000") is cfg.owner
    assert cfg.person("slack", "USAM") is cfg.people["sam"]
    assert cfg.person("signal", "+447700900123") is cfg.people["kim"]
    assert cfg.person("signal", "USAM") is None  # ids are per channel
    assert cfg.person("slack", "UNKNOWN") is None
    assert cfg.person("telegram", "") is None  # the owner's empty telegram id matches nobody
    assert cfg.person("terminal", "owner") is None  # only "local" is implicitly the owner


def test_endpoint_for(cfg: config.Config) -> None:
    assert cfg.endpoint_for("slack:C0123", is_group=True).name == "local"  # chat override
    assert cfg.endpoint_for("slack:C0456", is_group=True).name == "claude-api"
    assert cfg.endpoint_for("local:me", is_group=False).name == "local"
    cfg.own_chats = "gone"
    with pytest.raises(ValueError, match="gone"):
        cfg.endpoint_for("local:me", is_group=False)


def test_init_home_creates_a_home(home: Path, no_git: None) -> None:
    created = config.init_home(home, 'Alex "Al" O\\Neil', endpoint="claude-api")
    names = {p.relative_to(home).as_posix() for p in created}
    files = {"attache.toml", ".gitignore", "artifacts/home.html"}
    files |= {"memory/USER.md", "memory/ATTACHE.md"}
    assert files <= names
    bundled = {f"skills/{p.name}" for p in skills.BUNDLED.glob("*/")}
    assert names - files == bundled
    assert "skills/artifact-kit" in bundled
    for folder in ("memory/chats", "files", "logs"):
        assert (home / folder).is_dir()
    assert not (home / ".secret").exists()  # app.build makes it
    page = skills.BUNDLED / "artifact-kit" / "templates" / "home.html"
    home_page = (home / "artifacts" / "home.html").read_text(encoding="utf-8")
    assert home_page == page.read_text(encoding="utf-8")
    cfg = config.load(home)
    assert cfg.owner.name == 'Alex "Al" O\\Neil'
    assert cfg.own_chats == "claude-api"
    assert "{owner}" not in (home / "memory" / "USER.md").read_text(encoding="utf-8")
    assert "Alex" in (home / "memory" / "ATTACHE.md").read_text(encoding="utf-8")
    ignored = (home / ".gitignore").read_text(encoding="utf-8").split()
    assert ignored == ["attache.db*", ".secret", "files/", "logs/"]


def test_init_home_never_overwrites(home: Path, no_git: None) -> None:
    write(home, '[owner]\nname = "Mine"\n')
    created = config.init_home(home, "Alex")
    assert home / "attache.toml" not in created
    assert config.load(home).owner.name == "Mine"
    assert config.init_home(home, "Alex") == []


@needs_git
def test_init_home_commits(home: Path) -> None:
    config.init_home(home, "Alex")
    assert [line.split(" ", 1)[1] for line in skills.log(home)] == ["attache init"]
    tracked = (skills.git(home, "ls-files") or "").split()
    assert "attache.toml" in tracked
    assert "memory/USER.md" in tracked
    assert "skills/todos/SKILL.md" in tracked
    author = skills.git(home, "log", "-1", "--format=%an <%ae>") or ""
    assert author.strip() == "Attaché <attache@localhost>"
    assert skills.git(home, "status", "--porcelain", "--", "skills", "memory") == ""
