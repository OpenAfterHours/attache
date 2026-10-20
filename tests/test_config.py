from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from attache import config, skills

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
REAL_RESTRICT = config.restrict  # the autouse fixture `restricted` replaces it in every test

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


@pytest.fixture(autouse=True)
def restricted(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """The folders init_home restricts, recorded instead of changed (no whoami or icacls)."""
    folders: list[Path] = []
    monkeypatch.setattr(config, "restrict", folders.append)
    return folders


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
    assert (cfg.public_url, cfg.public_port) == ("", 8766)  # sharing is off
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
    assert cfg.limits == {
        "per_sender_hour": 20,
        "per_chat_hour": 60,
        "member_daily": 200,
        "guest_daily": 50,  # a pool of its own, so guests can't use up the members' turns
    }
    cfg.limits["member_daily"] = 1
    assert config.LIMITS["member_daily"] == 200  # each Config has its own copy


def test_channels_limits_and_approvals(home: Path) -> None:
    write(
        home,
        '[owner]\nname = "Alex"\napprovals = "slack:D0ALEXDM"\n[owner.ids]\nslack = "U0ALEX"\n'
        '[channels.slack]\nenabled = true\n[channels.signal]\nnumber = "+447700900123"\n'
        "[limits]\nper_chat_hour = 30\nguest_daily = 10\n",
    )
    cfg = config.load(home)
    assert cfg.approvals == "slack:D0ALEXDM"
    assert cfg.owner.ids == {"slack": "U0ALEX"}  # approvals is not a channel id
    assert cfg.channels == {
        "slack": {"enabled": True},
        "signal": {"enabled": False, "number": "+447700900123"},  # off until enabled
    }
    assert cfg.limits == {**config.LIMITS, "per_chat_hour": 30, "guest_daily": 10}
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
    assert (cfg.public_url, cfg.public_port) == ("https://pc.tailnet.ts.net", 8766)


def test_load_parses_everything(cfg: config.Config) -> None:
    assert (cfg.name, cfg.port, cfg.host) == ("Jeeves", 9000, "127.0.0.1")  # port coerced
    assert cfg.public_port == 9001  # the next port up
    assert cfg.owner.name == "Alex"
    assert cfg.owner.ids == {"slack": "UALEX", "signal": "+447700900000"}
    sam, kim = cfg.people["sam"], cfg.people["kim"]
    assert (sam.id, sam.name, sam.role, sam.ids) == ("sam", "Sam", "member", {"slack": "USAM"})
    assert kim.role == "guest"
    assert kim.ids == {"signal": "+447700900123"}  # from [people.kim.ids]
    local = cfg.endpoints["local"]
    assert (local.api, local.model) == ("openai-chat", "qwen3:32b")
    assert local.options == {"max_tokens": 2048}
    family = cfg.chats["slack:C0123"]
    assert (family.members, family.engage) == (["sam"], "always")
    work = cfg.chats["slack:C0456"]
    assert (work.engage, work.guests, work.endpoint) == ("mention", "ignore", None)
    assert not hasattr(cfg, "chat")  # callers use cfg.chats.get(key)


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
        ("[web]\npublic_port = []\n", r"\[web\] public_port must be a number, not \[\]"),
        ('[web]\npublic_url = "http://pc.ts.net"\n', r"\[web\] public_url must be"),  # not https
        ('[web]\npublic_url = "https://pc.ts.net/s"\n', r"\[web\] public_url must be"),  # a path
        ('[web]\npublic_url = "https://pc.ts.net/"\n', r"\[web\] public_url must be"),
        ('[web]\npublic_url = "https://"\n', r"\[web\] public_url must be"),
        ('[web]\npublic_url = "https://a b"\n', r"\[web\] public_url must be"),
        ("[web]\npublic_url = 443\n", r"\[web\] public_url must be"),
        ("[owner\n", r"attache\.toml: "),
        (
            "[limits]\nper_day = 5\n",
            r"unknown key per_day in \[limits\]; known: per_sender_hour, per_chat_hour, member",
        ),
        ("[channels.signal]\nnumber = 447700900123\n", r"\[channels\.signal\] number must be a"),
        ('[channels.slack]\nenabled = "yes"\n', r"\[channels\.slack\] enabled must be true or"),
        ("[channels.slack]\nenabled = true\nport = 8080\n", r"\[channels\.slack\] port must be"),
        ("[channels.local]\nenabled = true\n", r"\[channels\.local\] is taken"),
        ("[limits]\nper_chat_hour = 0\n", r"\[limits\] per_chat_hour must be a whole number"),
        ("[limits]\nmember_daily = true\n", r"\[limits\] member_daily must be"),  # not 1
        ("[limits]\nguest_daily = 0\n", r"\[limits\] guest_daily must be"),
        ('[limits]\nper_sender_hour = "20"\n', r"\[limits\] per_sender_hour must be"),
        ("[limits]\nper_sender_hour = 2.5\n", r"\[limits\] per_sender_hour must be"),
        ('[owner]\napprovals = "slack:D0ALEX"\n', r"\[owner\] approvals must be"),  # not enabled
        ('[owner]\napprovals = "me"\n', r"\[owner\] approvals must be"),
        ('[owner]\napprovals = "local:other"\n', r"\[owner\] approvals must be"),
        ('[owner]\napprovals = "slack:"\n[channels.slack]\nenabled = true\n', r"approvals must"),
        (  # a group chat: approvals go to your own direct chat
            '[owner]\napprovals = "slack:C1"\n[channels.slack]\nenabled = true\n'
            '[chats."slack:C1"]\nname = "Family"\n',
            r"\[owner\] approvals must be",
        ),
    ],
)
def test_load_errors_name_the_file_and_section(home: Path, text: str, message: str) -> None:
    path = write(home, text)
    with pytest.raises(ValueError, match=message) as error:
        config.load(home)
    assert str(path) in str(error.value)


def test_public_url_and_port(home: Path) -> None:
    write(home, '[web]\nport = 9000\npublic_url = "https://pc.tail-1a2b.ts.net:8443"\n')
    cfg = config.load(home)
    assert cfg.public_url == "https://pc.tail-1a2b.ts.net:8443"
    assert (cfg.port, cfg.public_port) == (9000, 9001)  # the next port up
    write(home, '[web]\npublic_url = "https://pc.ts.net"\npublic_port = "9100"\n')
    assert config.load(home).public_port == 9100  # coerced, as port is


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


def test_init_home_creates_a_home(home: Path, no_git: None, restricted: list[Path]) -> None:
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
    assert ignored == ["attache.db*", ".secret", "files/", "logs/", ".mail/", ".approvals/"]
    assert restricted == [home]  # the folder is made yours alone


def fake_commands(calls: list[list[str]], fail: str = "") -> Any:
    """subprocess.check_output for whoami and icacls; `fail` names one that fails."""

    def check_output(argv: list[str], **kwargs: Any) -> bytes:
        calls.append(argv)
        if argv[0] == fail:
            raise subprocess.CalledProcessError(5, argv)
        return b'"pc\\alex","S-1-5-21-1-2-3-1001"\r\n' if argv[0] == "whoami" else b""

    return check_output


@pytest.mark.parametrize("system", ["posix", "nt"])
def test_restrict(system: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    modes: list[tuple[Path, int]] = []
    monkeypatch.setattr(config.subprocess, "check_output", fake_commands(calls))
    monkeypatch.setattr(type(tmp_path), "chmod", lambda self, mode: modes.append((self, mode)))
    with monkeypatch.context() as patched:  # only around the call: pathlib reads os.name too
        patched.setattr(os, "name", system)
        REAL_RESTRICT(tmp_path)
    assert modes == [(tmp_path, 0o700)]  # on Windows it only clears the read-only flag
    if system == "posix":
        assert calls == []
    else:  # by SID, never by user name, and nobody else: inheritance off
        grant = ["/grant:r", "*S-1-5-21-1-2-3-1001:(OI)(CI)F", "/grant:r", "*S-1-5-18:(OI)(CI)F"]
        assert calls == [
            ["whoami", "/user", "/fo", "csv", "/nh"],
            ["icacls", str(tmp_path), "/inheritance:r", *grant],
        ]


@pytest.mark.parametrize("fail", ["whoami", "icacls", "chmod"])
def test_restrict_logs_a_failure_and_never_raises(
    fail: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[list[str]] = []

    def chmod(self: Path, mode: int) -> None:
        if fail == "chmod":
            raise PermissionError("not yours")

    monkeypatch.setattr(config.subprocess, "check_output", fake_commands(calls, fail))
    monkeypatch.setattr(type(tmp_path), "chmod", chmod)
    with caplog.at_level(logging.WARNING, logger="attache.config"), monkeypatch.context() as m:
        m.setattr(os, "name", "nt")
        REAL_RESTRICT(tmp_path)
    assert f"could not make {tmp_path} private" in caplog.text
    ran = {"chmod": [], "whoami": ["whoami"], "icacls": ["whoami", "icacls"]}[fail]
    assert [argv[0] for argv in calls] == ran  # it stops at the failure


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
