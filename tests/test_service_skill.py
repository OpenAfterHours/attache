"""The service skill's installer, on every system, without registering anything.

The script's `subprocess` and `shutil` are replaced with fakes for every test, so no task,
LaunchAgent or unit is ever created on the machine running the tests; files go to tmp_path.
"""

from __future__ import annotations

import importlib.util
import plistlib
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from xml.etree import ElementTree

import pytest

from attache import skills

SCRIPT = skills.BUNDLED / "service" / "scripts" / "service.py"
SYSTEMS = ["win32", "darwin", "linux"]
TASK_NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def load(path: Path = SCRIPT) -> ModuleType:
    spec = importlib.util.spec_from_file_location("attache_service", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up while the file runs
    spec.loader.exec_module(module)
    return module


class Fakes:
    """What the script may touch: commands (recorded, never run) and `attache` on PATH."""

    def __init__(self, tmp_path: Path) -> None:
        self.calls: list[list[str]] = []
        self.codes: dict[str, int] = {}  # exit code by command, such as {"bootout": 3}
        self.tool = tmp_path / "local" / "bin"  # where `uv tool install` puts attache
        self.cache = tmp_path / "AppData" / "Local" / "uv" / "cache" / "archive-v0" / "x" / "bin"
        self.on_path = {self.tool: self.tool / "attache.exe"}

        self.task_xml = b""  # the file `schtasks /Create /XML` was given, as it was then

    def run(self, argv: list[str], **options: Any) -> SimpleNamespace:
        self.calls.append(argv)
        if "/XML" in argv:
            self.task_xml = Path(argv[argv.index("/XML") + 1]).read_bytes()
        return SimpleNamespace(returncode=self.codes.get(argv[1] if len(argv) > 1 else "", 0))

    def which(self, name: str, path: str | None = None) -> str | None:
        found = self.on_path.get(Path(path)) if path and name == "attache" else None
        return str(found) if found else None


@pytest.fixture
def fakes(tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch) -> Fakes:
    fake = Fakes(tmp_path)
    monkeypatch.setenv("PATH", str(fake.tool))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "user"))
    monkeypatch.setattr("os.getuid", lambda: 501, raising=False)  # macOS's, on any system
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "temp"))
    monkeypatch.setenv("USERDOMAIN", "PC")
    monkeypatch.setenv("USERNAME", "alex")
    return fake


@pytest.fixture
def service(fakes: Fakes, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    module = load()
    commands = SimpleNamespace(run=fakes.run, list2cmdline=subprocess.list2cmdline)
    monkeypatch.setattr(module, "subprocess", commands)
    monkeypatch.setattr(module, "shutil", SimpleNamespace(which=fakes.which))
    return module


def test_skill_is_owner_only_and_names_the_commands() -> None:
    meta, body = skills.parse((SCRIPT.parents[1] / "SKILL.md").read_text(encoding="utf-8"))
    assert meta["name"] == "service" and "metadata" not in meta
    for text in (
        "attache skill service install",
        'script {skill: "service", script: "service.py"',
        "--dry-run",
        "uv tool install attache",
    ):
        assert text in body, text


@pytest.mark.parametrize("system", SYSTEMS)
def test_dry_run_does_nothing(
    system: str, service: ModuleType, fakes: Fakes, tmp_path: Path, capsys: Any
) -> None:
    for action in ("install", "uninstall", "status"):
        assert service.main([action, "--dry-run"], system=system) == 0
        out = capsys.readouterr().out
        assert out.startswith("would ") and "Attaché" not in out.splitlines()[-1]
    assert fakes.calls == []
    for folder in ("user", "config", "temp"):
        assert not (tmp_path / folder).exists(), folder


def task_settings(xml: bytes) -> dict[str, str]:
    """The task XML's values that matter, by element path."""
    task = ElementTree.fromstring(xml)
    paths = {
        "trigger user": "t:Triggers/t:LogonTrigger/t:UserId",
        "user": "t:Principals/t:Principal/t:UserId",
        "logon": "t:Principals/t:Principal/t:LogonType",
        "rights": "t:Principals/t:Principal/t:RunLevel",
        "time limit": "t:Settings/t:ExecutionTimeLimit",
        "on battery": "t:Settings/t:DisallowStartIfOnBatteries",
        "command": "t:Actions/t:Exec/t:Command",
        "arguments": "t:Actions/t:Exec/t:Arguments",
    }
    return {name: task.find(path, TASK_NS).text for name, path in paths.items()}


def test_windows_task_from_xml(
    service: ModuleType, fakes: Fakes, home: Path, tmp_path: Path, capsys: Any
) -> None:
    write, create, delete, start = service.plan("install", "win32")
    xml = tmp_path / "temp" / "attache-task.xml"
    assert (write.file, write.encoding) == (xml, "utf-16")
    # from XML, not /SC ONLOGON: that is any user's logon, which needs an administrator
    assert create.run == ["schtasks", "/Create", "/XML", str(xml), "/TN", "Attache", "/F"]
    assert (delete.file, delete.text) == (xml, None)
    assert start.run == ["schtasks", "/Run", "/TN", "Attache"]
    assert task_settings(write.text.encode("utf-16")) == {
        "trigger user": "PC\\alex",  # this user's logon only, which a standard user may set
        "user": "PC\\alex",
        "logon": "InteractiveToken",  # only while logged on, as /IT: what Outlook COM needs
        "rights": "LeastPrivilege",  # not elevated, as /RL LIMITED
        "time limit": "PT0S",  # never stopped (Task Scheduler's default is 72 hours)
        "on battery": "false",  # laptops on battery start it too
        "command": str(fakes.tool / "attache.exe"),
        "arguments": subprocess.list2cmdline(["run", "--home", str(home.resolve())]),
    }
    stop, remove = service.plan("uninstall", "win32")
    assert stop.may_fail and remove.run == ["schtasks", "/Delete", "/TN", "Attache", "/F"]
    assert service.plan("status", "win32")[0].run[:4] == ["schtasks", "/Query", "/TN", "Attache"]

    assert service.main(["install"], system="win32") == 0
    assert fakes.calls == [create.run, start.run]
    assert fakes.task_xml.startswith(b"\xff\xfe")  # UTF-16, as its declaration says
    assert task_settings(fakes.task_xml)["logon"] == "InteractiveToken"
    assert not xml.exists()  # removed once registered
    assert "Attaché is running, and starts at login." in capsys.readouterr().out


def test_windows_task_escapes_and_quotes(
    service: ModuleType,
    fakes: Fakes,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = tmp_path / "Program Files" / "uv tools"
    fakes.on_path = {folder: folder / "attache.exe"}
    monkeypatch.setenv("PATH", str(folder))
    monkeypatch.setenv("USERDOMAIN", "<R&D>")  # XML-escaped in the file
    monkeypatch.setenv("ATTACHE_HOME", str(tmp_path / "my home"))
    write, *_ = service.plan("install", "win32")
    settings = task_settings(write.text.encode("utf-16"))
    assert settings["user"] == "<R&D>\\alex"
    assert settings["command"] == str(folder / "attache.exe")  # its own element: no quotes
    assert settings["arguments"] == f'run --home "{(tmp_path / "my home").resolve()}"'
    monkeypatch.delenv("USERNAME")
    with pytest.raises(SystemExit, match="USERNAME"):
        service.plan("install", "win32")


def test_macos_launch_agent(service: ModuleType, fakes: Fakes, tmp_path: Path, home: Path) -> None:
    write, unload, load_agent = service.plan("install", "darwin")
    plist = tmp_path / "user" / "Library" / "LaunchAgents" / "ai.attache.plist"
    assert write.file == plist
    agent = plistlib.loads(write.text.encode("utf-8"))
    exe = str(fakes.tool / "attache.exe")
    assert agent["Label"] == "ai.attache"
    assert agent["ProgramArguments"] == [exe, "run", "--home", str(home.resolve())]
    assert agent["RunAtLoad"] is True and agent["KeepAlive"] is True
    assert agent["EnvironmentVariables"] == {"PATH": str(fakes.tool)}  # launchd's PATH is bare
    assert agent["StandardOutPath"] == str(home.resolve() / "logs" / "service.log")
    assert unload.run == ["launchctl", "bootout", "gui/501/ai.attache"] and unload.may_fail
    assert load_agent.run == ["launchctl", "bootstrap", "gui/501", str(plist)]

    fakes.codes["bootout"] = 3  # nothing loaded yet: fine
    assert service.main(["install"], system="darwin") == 0
    assert plist.read_text(encoding="utf-8") == write.text
    assert fakes.calls == [unload.run, load_agent.run]
    assert service.main(["uninstall"], system="darwin") == 0
    assert not plist.exists() and fakes.calls[-1] == unload.run


def test_linux_user_unit(service: ModuleType, fakes: Fakes, tmp_path: Path, home: Path) -> None:
    write, reload, start = service.plan("install", "linux")
    unit = tmp_path / "config" / "systemd" / "user" / "attache.service"
    assert write.file == unit
    exe = fakes.tool / "attache.exe"
    lines = write.text.splitlines()
    assert f'ExecStart="{exe}" "run" "--home" "{home.resolve()}"'.replace("\\", "\\\\") in lines
    assert f'Environment="PATH={fakes.tool}"'.replace("\\", "\\\\") in lines
    assert "Restart=on-failure" in lines and "WantedBy=default.target" in lines
    assert reload.run == ["systemctl", "--user", "daemon-reload"]
    assert start.run == ["systemctl", "--user", "enable", "--now", "attache"]

    assert service.main(["install"], system="linux") == 0
    assert unit.read_text(encoding="utf-8") == write.text
    assert fakes.calls == [reload.run, start.run]
    assert service.main(["uninstall"], system="linux") == 0
    assert not unit.exists()
    assert fakes.calls[2:] == [
        ["systemctl", "--user", "disable", "--now", "attache"],
        ["systemctl", "--user", "daemon-reload"],
    ]
    assert service.plan("status", "linux2")[0].run[:3] == ["systemctl", "--user", "status"]


def test_unit_file_quoting(service: ModuleType) -> None:
    assert service.quoted('C:\\a "b" 100% $HOME') == '"C:\\\\a \\"b\\" 100%% $HOME"'
    # ExecStart= expands $NAME, Environment= does not
    assert service.quoted("$HOME/x", command=True) == '"$$HOME/x"'


def test_a_dollar_in_the_home_stays_literal(
    service: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ATTACHE_HOME", str(tmp_path / "pay$day"))
    write, *_ = service.plan("install", "linux")
    exec_start = next(line for line in write.text.splitlines() if line.startswith("ExecStart="))
    assert "pay$$day" in exec_start and "pay$day" not in exec_start


def test_the_uvx_copy_is_refused(
    service: ModuleType, fakes: Fakes, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fakes.on_path = {fakes.cache: fakes.cache / "attache.exe"}
    monkeypatch.setenv("PATH", str(fakes.cache))
    with pytest.raises(SystemExit, match="uv tool install attache"):
        service.plan("install", "win32")
    # the installed copy wins even when uvx put its own first on PATH
    fakes.on_path[fakes.tool] = fakes.tool / "attache.exe"
    monkeypatch.setenv("PATH", f"{fakes.cache}{service.os.pathsep}{fakes.tool}")
    assert service.find_attache() == fakes.tool / "attache.exe"
    # a cache moved with UV_CACHE_DIR is still uv's cache
    moved = tmp_path / "elsewhere" / "bin"
    fakes.on_path = {moved: moved / "attache.exe"}
    monkeypatch.setenv("PATH", str(moved))
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "elsewhere"))
    with pytest.raises(SystemExit, match="uvx's copy"):
        service.find_attache()
    monkeypatch.setenv("PATH", "")
    with pytest.raises(SystemExit, match="not on PATH"):
        service.plan("install", "linux")
    assert service.plan("status", "linux")  # status and uninstall need no attache
    assert service.plan("uninstall", "darwin")


def test_uv_cache_paths(service: ModuleType) -> None:
    cached = [
        Path("C:/Users/you/AppData/Local/uv/cache/archive-v0/abc/Scripts/attache.exe"),
        Path("/home/you/.cache/uv/archive-v0/abc/bin/attache"),
    ]
    installed = [
        Path("C:/Users/you/.local/bin/attache.exe"),
        Path("/home/you/.local/share/uv/tools/attache/bin/attache"),
    ]
    assert all(service.in_uv_cache(path) for path in cached)
    assert not any(service.in_uv_cache(path) for path in installed)


def test_a_failed_step_stops_the_rest(service: ModuleType, fakes: Fakes, capsys: Any) -> None:
    fakes.codes["/Create"] = 1  # say, "Access is denied"
    assert service.main(["install"], system="win32") == 1
    assert len(fakes.calls) == 1  # /Run never ran
    assert "failed (exit code 1)" in capsys.readouterr().err


def test_other_systems_are_refused(service: ModuleType) -> None:
    with pytest.raises(SystemExit, match="no login service"):
        service.plan("install", "freebsd14")


def test_home_is_the_one_the_skill_is_installed_in(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skills.install_bundled(home)
    monkeypatch.delenv("ATTACHE_HOME")
    installed = load(home / "skills" / "service" / "scripts" / "service.py")
    assert installed.home() == home.resolve()
