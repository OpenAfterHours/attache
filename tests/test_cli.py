from __future__ import annotations

import builtins
import io
import logging
import shutil
import socket
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from attache import app, channels, cli, config

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
LOCAL = """
[web]
port = {port}

[routing]
own_chats = "local"
group_chats = "{group}"

[endpoints.local]
api = "openai-chat"
base_url = "http://localhost:11434/v1"
model = "qwen3:32b"

[endpoints.api]
api = "anthropic"
model = "m"
key_env = "ATTACHE_TEST_KEY"
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def write_config(home: Path, port: int, group: str = "local") -> None:
    (home / "attache.toml").write_text(LOCAL.format(port=port, group=group), encoding="utf-8")


@needs_git
def test_init(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home = tmp_path / "h"
    assert cli.main(["init", "--home", str(home), "--name", "Alex"]) == 0
    out = capsys.readouterr().out
    assert f"Home: {home}" in out and "created attache.toml" in out and "Next:" in out
    assert 'name = "Alex"' in (home / "attache.toml").read_text(encoding="utf-8")
    assert cli.main(["--home", str(home), "init"]) == 0  # again: nothing new
    assert "created" not in capsys.readouterr().out


def test_skills(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (home / "skills" / "pdf").mkdir(parents=True)
    (home / "skills" / "pdf" / "SKILL.md").write_text(
        "---\nname: pdf\ndescription: Make PDFs.\n---\n", encoding="utf-8"
    )
    assert cli.main(["skills", "list"]) == 0  # --home comes from $ATTACHE_HOME
    assert "pdf: Make PDFs." in capsys.readouterr().out
    assert cli.main(["skills"]) == 0 and "pdf" in capsys.readouterr().out
    logged: list[Any] = []
    monkeypatch.setattr(cli.skills, "log", lambda home, name: logged.append(name) or ["abc fix"])
    assert cli.main(["skills", "log"]) == 0 and cli.main(["skills", "log", "pdf"]) == 0
    assert logged == [None, "pdf"] and "abc fix" in capsys.readouterr().out
    monkeypatch.setattr(cli.skills, "undo", lambda home, name: None)
    assert cli.main(["skills", "undo", "pdf"]) == 1
    for bad in (["skills", "undo"], ["skills", "bogus"]):
        with pytest.raises(SystemExit) as exit:
            cli.main(bad)
        assert exit.value.code == 2


def test_skill_runs_the_skills_own_script(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = home / "skills" / "service" / "scripts" / "service.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('hi')\n", encoding="utf-8")
    ran: list[list[str]] = []
    monkeypatch.setattr(cli.subprocess, "run", lambda argv: ran.append(argv) or Done(3))
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")
    # Every argument after the name is the script's, options included: argparse never sees them.
    assert cli.main(["skill", "service", "install", "--dry-run"]) == 3  # its exit code
    assert ran == [["uv", "run", str(script), "install", "--dry-run"]]
    # SystemExit with a message: Python prints that one line on stderr and exits with code 1.
    for bad in (["skill"], ["skill", "nope"], ["skill", ".."], ["skill", "../service"]):
        with pytest.raises(SystemExit, match="new bundled skills arrive with `attache init`"):
            cli.main(bad)
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    with pytest.raises(SystemExit) as exit:
        cli.main(["skill", "service", "status"])
    assert str(exit.value) == "uv is not installed: see https://docs.astral.sh/uv/"
    assert len(ran) == 1


class Done:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


def test_doctor(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["doctor"]) == 1  # no attache.toml yet
    out = capsys.readouterr().out
    assert "✗ no attache.toml" in out and "web port" not in out
    write_config(home, free_port())
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "✓ endpoint local" in out and "web port" in out and "free" in out and "✗" not in out
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    assert cli.main(["doctor"]) == 1 and "✗ git installed" in capsys.readouterr().out


def test_doctor_checks_enabled_channels(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")
    enabled = ["slack", "signal", "irc", "xmpp", "nostr"]
    blocks = [f"[channels.{name}]\nenabled = true\n" for name in enabled]
    blocks.append("[channels.matrix]\nenabled = false\n")
    (home / "attache.toml").write_text(
        LOCAL.format(port=free_port(), group="local") + "\n" + "".join(blocks), "utf-8"
    )
    skill = "---\nname: {0}\ndescription: The {0} channel.\n{1}---\n"
    script = "metadata:\n  attache-channel: {}\n"
    for name, meta in [
        ("slack", script.format("channel.py")),  # a plain name, and the file is there: fine
        ("signal", ""),  # names no script
        ("irc", script.format("../channel.py")),  # not a plain file name
        ("xmpp", script.format("missing.py")),  # no such file
    ]:
        (home / "skills" / name).mkdir(parents=True)
        (home / "skills" / name / "SKILL.md").write_text(skill.format(name, meta), "utf-8")
        (home / "skills" / name / "channel.py").write_text("print('hi')\n", "utf-8")
    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line[2:].startswith("channel ")]
    marks = {line.split()[2].rstrip(":"): line[0] for line in lines}
    # nostr has no skill at all; matrix is not enabled, so it is not checked
    assert marks == {"slack": "✓", "signal": "✗", "irc": "✗", "xmpp": "✗", "nostr": "✗"}
    assert "✓ channel slack: skills/slack names its sidecar script" in out
    started = [side.name for side in channels.sidecars(config.load(home), lambda *a: False)]
    assert started == ["slack"]  # doctor and sidecars() agree


def test_doctor_warnings_and_notes(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.delenv("ATTACHE_TEST_KEY", raising=False)
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        write_config(home, busy.getsockname()[1], group="api")
        assert cli.main(["doctor"]) == 0  # neither is a failure
    out = capsys.readouterr().out
    assert "! endpoint api needs ATTACHE_TEST_KEY" in out
    assert "i web port" in out and "may already be running" in out
    (home / "attache.toml").write_text("[web\nport = 1", encoding="utf-8")
    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "✗ attache.toml:" in out and "web port" not in out  # the parse error, no port check
    (home / "attache.toml").write_text('[web]\nhost = "0.0.0.0"\n', encoding="utf-8")
    assert cli.main(["doctor"]) == 1 and "✗ [web] host" in capsys.readouterr().out


def test_help_and_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit:
        cli.main(["--help"])
    out = capsys.readouterr().out
    assert exit.value.code == 0 and "usage: attache" in out and "skills list" in out
    assert "mcp" not in out
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert capsys.readouterr().out.startswith("attache ")


@needs_git
def test_bare_attache_sets_up_then_chats(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runs: list[tuple[Path, bool]] = []

    async def fake_run(where: Path, *, terminal: bool) -> None:
        runs.append((where, terminal))

    def no_terminal(prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr(app, "run", fake_run)
    monkeypatch.setattr(builtins, "input", no_terminal)
    monkeypatch.setenv("USERNAME", "Robin")
    assert cli.main([]) == 0
    assert runs == [(home, True)]
    assert 'name = "Robin"' in (home / "attache.toml").read_text("utf-8")  # the default name
    assert cli.main(["run"]) == 0 and runs[-1] == (home, False)


def test_run_without_a_home_or_with_a_bad_config(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["run", "--home", str(home / "missing")]) == 1
    (home / "attache.toml").write_text("[web\n", encoding="utf-8")
    assert cli.main(["chat"]) == 1
    err = capsys.readouterr().err
    assert err.count("\n") == 2 and "attache.toml:" in err and "Traceback" not in err


@pytest.mark.parametrize("stdout", [None, io.StringIO()])  # StringIO has no reconfigure()
def test_stdin_from_a_pipe_is_read_as_utf8(
    stdout: io.StringIO | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = io.BytesIO("café, Ágnes\n".encode())
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(raw, encoding="cp1252"))
    if stdout:
        monkeypatch.setattr(sys, "stdout", stdout)  # a stdout that can't change keeps stdin's
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert sys.stdin.encoding == "utf-8" and sys.stdin.readline() == "café, Ágnes\n"


def test_run_logs_attache_news_but_only_library_warnings(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured: list[dict[str, Any]] = []

    async def fake_run(where: Path, *, terminal: bool) -> None: ...

    monkeypatch.setattr(app, "run", fake_run)
    monkeypatch.setattr(cli.logging, "basicConfig", lambda **kw: configured.append(kw))
    write_config(home, free_port())
    assert cli.main(["run"]) == 0
    assert configured == [{"level": logging.WARNING}]  # the root logger, so httpx and the rest
    assert logging.getLogger("attache.channels").getEffectiveLevel() == logging.INFO


@pytest.fixture(autouse=True)
def default_logging() -> Iterator[None]:
    yield
    logging.getLogger("attache").setLevel(logging.NOTSET)  # as it was before _run set INFO
