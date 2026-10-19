from __future__ import annotations

import builtins
import shutil
import socket
from pathlib import Path
from typing import Any

import pytest

from attache import app, cli

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
LOCAL = """
[web]
port = {port}

[routing]
own_chats = "local"
group_chats = "local"

[endpoints.local]
api = "openai-chat"
base_url = "http://localhost:11434/v1"
model = "qwen3:32b"
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@needs_git
def test_init(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home = tmp_path / "h"
    assert cli.main(["init", "--home", str(home), "--name", "Alex"]) == 0
    out = capsys.readouterr().out
    assert f"Home: {home}" in out and "created attache.toml" in out and "Next:" in out
    assert 'name = "Alex"' in (home / "attache.toml").read_text(encoding="utf-8")
    assert cli.main(["--home", str(home), "init"]) == 0  # again: nothing new
    assert "created" not in capsys.readouterr().out


def test_skills_list_and_undo(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (home / "skills" / "pdf").mkdir(parents=True)
    (home / "skills" / "pdf" / "SKILL.md").write_text(
        "---\nname: pdf\ndescription: Make PDFs.\n---\n", encoding="utf-8"
    )
    assert cli.main(["skills", "list"]) == 0  # --home comes from $ATTACHE_HOME
    assert "pdf: Make PDFs." in capsys.readouterr().out
    assert cli.main(["skills"]) == 0 and "pdf" in capsys.readouterr().out
    assert cli.main(["skills", "undo"]) == 1  # needs a name
    assert cli.main(["skills", "bogus"]) == 1


def test_skills_log(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(cli.skills, "log", lambda home, name: calls.append(name) or ["abc fix"])
    assert cli.main(["skills", "log"]) == 0 and cli.main(["skills", "log", "pdf"]) == 0
    assert calls == [None, "pdf"] and "abc fix" in capsys.readouterr().out


def test_doctor(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["doctor"]) == 1  # no attache.toml yet
    assert "✗ attache.toml parses" in capsys.readouterr().out
    (home / "attache.toml").write_text(LOCAL.format(port=free_port()), encoding="utf-8")
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "✓ endpoint local: needs nothing" in out and "✓ web port free" in out and "✗" not in out
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    assert cli.main(["doctor"]) == 1 and "✗ git installed" in capsys.readouterr().out


def test_doctor_checks_keys_binaries_and_port(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        toml = LOCAL.format(port=busy.getsockname()[1])
        toml += '\n[endpoints.api]\napi = "anthropic"\nkey_env = "ATTACHE_TEST_KEY"\n'
        toml += '\n[endpoints.cli]\napi = "claude-cli"\nbin = "no-such-claude"\n'
        (home / "attache.toml").write_text(toml, encoding="utf-8")
        monkeypatch.delenv("ATTACHE_TEST_KEY", raising=False)
        assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "✗ endpoint api: needs ATTACHE_TEST_KEY" in out
    assert "✗ endpoint cli: needs no-such-claude" in out and "✗ web port free" in out


def test_help_and_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit:
        cli.main(["--help"])
    assert exit.value.code == 0 and "usage: attache" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert capsys.readouterr().out.startswith("attache ")


@needs_git
def test_bare_attache_sets_up_then_chats(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runs: list[tuple[Path, bool]] = []

    async def fake_run(where: Path, *, terminal: bool) -> None:
        runs.append((where, terminal))

    monkeypatch.setattr(app, "run", fake_run)
    monkeypatch.setattr(builtins, "input", lambda prompt: "Robin")
    assert cli.main([]) == 0
    assert runs == [(home, True)] and 'name = "Robin"' in (home / "attache.toml").read_text("utf-8")
    assert cli.main(["run"]) == 0 and runs[-1] == (home, False)


def test_chat_without_home_and_mcp(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert cli.main(["run", "--home", str(home / "missing")]) == 1
    calls: list[Any] = []
    monkeypatch.setattr(cli.mcp, "serve", lambda url, token: calls.append((url, token)))
    assert cli.main(["mcp", "--url", "http://127.0.0.1:8765", "--token", "t"]) == 0
    assert calls == [("http://127.0.0.1:8765", "t")]
