# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Start Attaché at login, in your own session, and keep it running.

    service.py install|uninstall|status [--dry-run]

- Windows: a Task Scheduler task "Attache" that runs at logon, only while you are logged on
  (/IT) and without elevated rights (/RL LIMITED), which Outlook's COM automation needs.
  Install also starts it now (/Run), and uninstall ends it (/End).
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
from dataclasses import dataclass
from pathlib import Path

TASK = "Attache"  # the Task Scheduler task
LABEL = "ai.attache"  # the LaunchAgent
UNIT = "attache"  # the systemd user unit
TR_LIMIT = 261  # the longest command schtasks accepts in /TR
INSTALL = "install it with `uv tool install attache` first"
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


def quoted(text: str) -> str:
    """One argument for a systemd unit file: in double quotes, with "%" doubled."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{escaped}"'


def windows(action: str, command: list[str], _home: Path) -> list[Step]:
    if action == "status":
        return [Step(["schtasks", "/Query", "/TN", TASK, "/V", "/FO", "LIST"])]
    if action == "uninstall":
        stop = Step(["schtasks", "/End", "/TN", TASK], may_fail=True)  # when it is running
        return [stop, Step(["schtasks", "/Delete", "/TN", TASK, "/F"])]
    line = subprocess.list2cmdline(command)  # quoted the way schtasks reads /TR
    if len(line) > TR_LIMIT:
        raise SystemExit(f"error: the command is longer than schtasks allows: {line}")
    task = ["/SC", "ONLOGON", "/RL", "LIMITED", "/IT", "/TN", TASK, "/TR", line, "/F"]
    # /Run starts it now, as launchctl bootstrap and systemctl --now do
    return [Step(["schtasks", "/Create", *task]), Step(["schtasks", "/Run", "/TN", TASK])]


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
    text = UNIT_FILE.format(command=" ".join(map(quoted, command)), path=path)
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
        step.file.write_text(step.text, encoding="utf-8")
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
