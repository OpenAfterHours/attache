# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Start Attaché at login, in your own session, and keep it running.

    service.py install|uninstall|status [--dry-run]

- Windows: a Task Scheduler task "Attache", registered from XML (`schtasks /Create /XML`) with
  a logon trigger for you alone, which a standard user may create (`/SC ONLOGON` means any
  user's logon, and needs an administrator). It runs only while you are logged on
  (InteractiveToken, as /IT) and without elevated rights (LeastPrivilege, as /RL LIMITED),
  which Outlook's COM automation needs; also on battery, and with no time limit. Install also
  starts it now (/Run), and uninstall ends it (/End).
- macOS: a LaunchAgent, ~/Library/LaunchAgents/ai.attache.plist, with RunAtLoad and KeepAlive.
- Linux: a systemd user unit, ~/.config/systemd/user/attache.service, enabled with
  `systemctl --user enable --now attache`.

The service runs `attache run --home <home>` with the `attache` that `uv tool install attache`
put on PATH. The copy `uvx attache` runs lives in uv's cache, which can be cleared at any
time, so it is refused. --dry-run prints the commands it would run and the files it would
write, and does neither.
"""

from __future__ import annotations

import argparse
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

TASK = "Attache"  # the Task Scheduler task
LABEL = "ai.attache"  # the LaunchAgent
UNIT = "attache"  # the systemd user unit
INSTALL = "install it with `uv tool install attache` first"
# Settings left out keep Task Scheduler's defaults. The ones given differ from them: start on
# battery, keep running on battery, no 72-hour time limit, and restart after a failure.
TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Attaché, your personal assistant</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{user}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
    </Exec>
  </Actions>
</Task>
"""
UNIT_FILE = """[Unit]
Description=Attaché, your personal assistant

[Service]
ExecStart={command}
Environment={path}
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
"""


@dataclass
class Step:
    """Run a command, or write a file (text None: delete it)."""

    run: list[str] | None = None
    file: Path | None = None
    text: str | None = None
    encoding: str = "utf-8"
    may_fail: bool = False  # such as unloading an agent that was never loaded


def home() -> Path:
    """The Attaché home: $ATTACHE_HOME, else the home this copy of the skill was installed
    into (<home>/skills/service/scripts/service.py)."""
    env = os.environ.get("ATTACHE_HOME")
    return Path(env).expanduser().resolve() if env else Path(__file__).resolve().parents[3]


def in_uv_cache(path: Path) -> bool:
    """True for a copy in uv's cache, where `uvx attache` runs it from."""
    cache = os.environ.get("UV_CACHE_DIR")
    if cache and path.resolve().is_relative_to(Path(cache).resolve()):
        return True
    # uv's default caches: %LOCALAPPDATA%\uv\cache on Windows, ~/.cache/uv elsewhere
    parts = [part.lower() for part in path.parts]
    return "uv" in parts and any(part in ("cache", ".cache") for part in parts)


def find_attache() -> Path:
    """The first `attache` on PATH outside uv's cache; SystemExit saying how to get one."""
    found = []
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        if folder and (exe := shutil.which("attache", path=folder)):
            found.append(Path(exe))
    for exe in found:
        if not in_uv_cache(exe):
            return exe
    if found:
        raise SystemExit(f"error: the only attache found is uvx's copy in uv's cache; {INSTALL}")
    raise SystemExit(f"error: attache is not on PATH; {INSTALL}")


def quoted(text: str, command: bool = False) -> str:
    """One value for a systemd unit file: in double quotes, with "%" doubled. In a command
    "$" is doubled too, because ExecStart= expands $NAME (Environment= does not)."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if command:
        escaped = escaped.replace("$", "$$")
    return f'"{escaped}"'


def windows_user() -> str:
    """DOMAIN\\name: the account the task runs as, and whose logon starts it."""
    name, domain = os.environ.get("USERNAME"), os.environ.get("USERDOMAIN")
    if not name:
        raise SystemExit("error: USERNAME is not set, so the task has no user")
    return f"{domain}\\{name}" if domain else name


def windows(action: str, command: list[str], _home: Path) -> list[Step]:
    if action == "status":
        return [Step(["schtasks", "/Query", "/TN", TASK, "/V", "/FO", "LIST"])]
    if action == "uninstall":
        stop = Step(["schtasks", "/End", "/TN", TASK], may_fail=True)  # when it is running
        return [stop, Step(["schtasks", "/Delete", "/TN", TASK, "/F"])]
    task = TASK_XML.format(
        user=escape(windows_user()),
        command=escape(command[0]),
        arguments=escape(subprocess.list2cmdline(command[1:])),  # quoted as Windows reads them
    )
    xml = Path(tempfile.gettempdir()) / "attache-task.xml"
    return [
        Step(file=xml, text=task, encoding="utf-16"),  # what the declaration says, as exported
        Step(["schtasks", "/Create", "/XML", str(xml), "/TN", TASK, "/F"]),
        Step(file=xml),  # deleted once registered
        Step(["schtasks", "/Run", "/TN", TASK]),  # now, as launchctl bootstrap and --now do
    ]


def macos(action: str, command: list[str], home: Path) -> list[Step]:
    plist = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    domain = f"gui/{os.getuid()}"  # the logged-in user's own session
    unload = Step(["launchctl", "bootout", f"{domain}/{LABEL}"], may_fail=True)
    if action == "status":
        return [Step(["launchctl", "print", f"{domain}/{LABEL}"])]
    if action == "uninstall":
        return [unload, Step(file=plist)]
    log = str(home / "logs" / "service.log")
    agent = {
        "Label": LABEL,
        "ProgramArguments": command,
        "RunAtLoad": True,
        "KeepAlive": True,
        # launchd's own PATH lacks ~/.local/bin and Homebrew, where uv, git and claude live
        "EnvironmentVariables": {"PATH": os.environ.get("PATH", "")},
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }
    load = Step(["launchctl", "bootstrap", domain, str(plist)])  # starts it now (RunAtLoad)
    return [Step(file=plist, text=plistlib.dumps(agent).decode("utf-8")), unload, load]


def linux(action: str, command: list[str], _home: Path) -> list[Step]:
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    unit = config / "systemd" / "user" / f"{UNIT}.service"
    systemctl = ["systemctl", "--user"]
    if action == "status":
        return [Step([*systemctl, "status", UNIT, "--no-pager"])]
    if action == "uninstall":
        disable = Step([*systemctl, "disable", "--now", UNIT], may_fail=True)
        return [disable, Step(file=unit), Step([*systemctl, "daemon-reload"])]
    # systemd's user PATH lacks ~/.local/bin, where uv tool and uv's installer put uv
    path = quoted(f"PATH={os.environ.get('PATH', '')}")
    exec_start = " ".join(quoted(part, command=True) for part in command)
    text = UNIT_FILE.format(command=exec_start, path=path)
    reload, start = [*systemctl, "daemon-reload"], [*systemctl, "enable", "--now", UNIT]
    return [Step(file=unit, text=text), Step(reload), Step(start)]


SYSTEMS = {"win32": windows, "darwin": macos, "linux": linux}


def plan(action: str, system: str) -> list[Step]:
    """The steps for `action` on `system` (a sys.platform value)."""
    maker = SYSTEMS.get("linux" if system.startswith("linux") else system)
    if maker is None:
        raise SystemExit(f"error: no login service for {system}; run `attache run` yourself")
    place = home()
    command = [str(find_attache()), "run", "--home", str(place)] if action == "install" else []
    return maker(action, command, place)


def describe(step: Step, system: str) -> str:
    if step.run:
        join = subprocess.list2cmdline if system == "win32" else shlex.join
        return f"run: {join(step.run)}"
    if step.text is None:
        return f"delete: {step.file}"
    return f"write: {step.file}\n" + "\n".join(f"    {line}" for line in step.text.splitlines())


def do(step: Step) -> int:
    """Carry out one step; the command's exit code (0 when it may fail)."""
    if step.run:
        code = subprocess.run(step.run).returncode  # its output goes straight to the terminal
        return 0 if step.may_fail else code
    if step.text is None:
        step.file.unlink(missing_ok=True)
    else:
        step.file.parent.mkdir(parents=True, exist_ok=True)
        step.file.write_text(step.text, encoding=step.encoding)
    return 0


def main(argv: list[str] | None = None, system: str = sys.platform) -> int:
    parser = argparse.ArgumentParser(prog="attache skill service", description=__doc__)
    parser.formatter_class = argparse.RawDescriptionHelpFormatter
    parser.add_argument("action", choices=["install", "uninstall", "status"])
    parser.add_argument("--dry-run", action="store_true", help="print the steps; do nothing")
    args = parser.parse_args(argv)
    for step in plan(args.action, system):
        # flush: a command's own output, written straight to the terminal, comes after this
        print(("would " if args.dry_run else "") + describe(step, system), flush=True)
        if not args.dry_run and (code := do(step)) != 0:
            print(f"error: that failed (exit code {code})", file=sys.stderr)
            return code
    if not args.dry_run and args.action != "status":
        done = {
            "install": "is running, and starts at login",
            "uninstall": "has stopped, and no longer starts at login",
        }
        print(f"Attaché {done[args.action]}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
