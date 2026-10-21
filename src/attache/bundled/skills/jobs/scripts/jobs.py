# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Jobs: commands the owner set up in <home>/skills/jobs/jobs.toml, which the people a job
lists in `who` may start (the owner may start any). Each command prints one JSON object, on
the last line, and exits 0 or 1:

    jobs.py list          {"ok": true, "jobs": [{"name", "label", "running", "since"}]}
    jobs.py start NAME    {"ok": true, "text": "The VA run finished in 14 min."}
    jobs.py setup         copies jobs.example.toml to jobs.toml when there is none

A refusal or a failure is {"ok": false, "text": ..., "owner": ...} with exit code 1: `text` is
for the person who asked, `owner` for the owner alone (when a background run fails, the core
sends it to them). Neither ever holds what the job printed: that goes to its log,
<home>/logs/jobs/NAME-YYYYmmdd-HHMMSS.log, which nothing here reads. The last 20 are kept.

Who is asking comes from what the core sets, ATTACHE_ROLE and ATTACHE_SENDER, never from the
arguments. Without ATTACHE_ROLE, it is the owner, running this in their own terminal. Anyone
else's model reads a foreground run's output, so there `owner` is left out, and `start` runs
only in the background (ATTACHE_BACKGROUND=1, which the core sets on background runs alone).
`setup` is the owner's.

`start` runs the job's argument list without a shell and waits for it (the core runs it in
the background). One run per job at a time: the lock is a system lock on
<home>/logs/jobs/NAME.lock, which holds the pid and start time while it runs. The system lets
go of it when this process ends, however it ends, so a lock is never stale. At the time limit,
or when Attaché stops this run, the job's whole process tree is killed.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Any

if os.name == "nt":
    import msvcrt
else:
    import fcntl

HERE = Path(__file__).resolve().parent  # <home>/skills/jobs/scripts
PLAIN = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")  # a job's name, which names its files
KEEP = 20  # logs kept per job
# The longest time limit: the core stops a background script at 12 hours (720 minutes), and
# this must get there first, to kill the job and say so itself.
MAX_MINUTES = 690
LOGS = Path("logs", "jobs")  # in the home: the locks and the logs
LOCK_AT = 1 << 20  # Windows: the byte locked, past the text, which a lock would hide
NOT_YOURS = "That isn't a job you can start."
Json = dict[str, Any]


class JobError(Exception):
    """A refusal or a failure: `text` for the person who asked, `owner` for the owner alone."""

    def __init__(self, text: str, owner: str | None = None) -> None:
        self.text, self.owner = text, owner


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # one JSON object, not usage text and exit 2
        raise JobError(f"{self.prog}: {message}")

    def print_help(self, file: Any = None) -> None:  # -h: the usage, as the JSON's text
        raise JobError(self.format_help())


def caller() -> tuple[str, bool]:
    """(who is asking, whether with the owner's rights), from what the core sets."""
    role = os.environ.get("ATTACHE_ROLE")
    owner = role is None or role == "owner"  # None: the owner's own terminal
    return os.environ.get("ATTACHE_SENDER") or ("owner" if owner else ""), owner


def background() -> bool:
    """Whether the core runs this in the background, so that only it reads the result."""
    return os.environ.get("ATTACHE_BACKGROUND") == "1"


def strings(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(each, str) and each for each in value)


def checked(name: str, job: Any) -> Json:
    """A job's settings with the defaults filled in; JobError when they aren't valid."""
    defaults = {"label": name, "who": [], "timeout_minutes": 120, "cwd": "."}
    job = {**defaults, **job} if isinstance(job, dict) else {}
    limit = job.get("timeout_minutes")
    # who as a list: `in` on a string would match part of a name
    lists = strings(job.get("command")) and job["command"] and strings(job["who"])
    texts = isinstance(job.get("label"), str) and isinstance(job.get("cwd"), str)
    timed = type(limit) in (int, float) and 0 < limit <= MAX_MINUTES
    if not (PLAIN.fullmatch(name) and lists and texts and timed):
        need = f"a lowercase name, command and who as lists, timeout_minutes to {MAX_MINUTES}"
        raise JobError("The jobs can't be read just now.", f"jobs.toml: [{name}] needs {need}")
    return job


def load(home: Path) -> dict[str, Json]:
    """The jobs in jobs.toml, checked."""
    path = home / "skills" / "jobs" / "jobs.toml"
    if not path.is_file():
        raise JobError("No jobs are set up.", "no jobs.toml: run `attache skill jobs setup`")
    jobs = tomllib.loads(path.read_text(encoding="utf-8-sig"))  # Notepad may write a BOM
    return {name: checked(name, job) for name, job in jobs.items()}


def title(job: Json) -> str:
    """The job's label to start a sentence: "The VA run"."""
    return job["label"][:1].upper() + job["label"][1:]


def take(path: Path) -> int | None:
    """Lock a job: the lock file's descriptor, to hold while it runs, or None when another run
    holds it. The system lets go when this process ends, however it ends."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
    try:
        if os.name == "nt":
            os.lseek(fd, LOCK_AT, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def let_go(fd: int) -> None:
    os.ftruncate(fd, 0)  # not running: see since()
    if os.name == "nt":  # now: Windows may take its time over a lock left to close
        os.lseek(fd, LOCK_AT, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    os.close(fd)


def since(path: Path) -> str | None:
    """When the job's run started (ISO, local time), or None when it isn't running. A run
    empties its lock file as it ends; one that was killed leaves the text, but not the lock."""
    with contextlib.suppress(OSError, ValueError):
        started = json.loads(path.read_text(encoding="utf-8"))["since"]
        if (fd := take(path)) is None:
            return started
        let_go(fd)
    return None


def prune(logs: Path, name: str) -> None:
    """Keep the job's newest KEEP logs; their names sort by time."""
    for old in sorted(logs.glob(f"{name}-{'[0-9]' * 8}-{'[0-9]' * 6}.log"))[:-KEEP]:
        with contextlib.suppress(OSError):  # still open in a process that is going
            old.unlink()


def kill(proc: subprocess.Popen) -> None:
    """Kill the job and everything it started, and reap it. On POSIX it leads its own
    session, so its process group holds all of them."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    else:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
    proc.kill()  # in case the tree kill missed it
    proc.wait()


def wait(proc: subprocess.Popen, seconds: float) -> bool:
    """Wait for the job; whether it ended by itself. It is killed, tree and all, at the time
    limit, on an interruption, and on POSIX once this process's parent is gone: Attaché
    stopping a run kills its process group, which run() left, so as to get here."""
    parent, deadline = os.getppid(), time.monotonic() + seconds
    try:
        while proc.poll() is None:
            if time.monotonic() > deadline or (os.name != "nt" and os.getppid() != parent):
                return False
            time.sleep(0.25)
        return True
    finally:
        if proc.poll() is None:
            kill(proc)


def run(home: Path, name: str, job: Json, who: str, fd: int) -> Json:
    """Run a locked job to its end, its output into a new log."""
    now = datetime.now()
    record = {"pid": os.getpid(), "since": now.isoformat(timespec="seconds")}
    os.ftruncate(fd, 0)  # the text a killed run left
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, json.dumps(record).encode("utf-8"))
    log = home / LOGS / f"{name}-{now:%Y%m%d-%H%M%S}.log"
    where = f"started by {who}. Log: {log.relative_to(home).as_posix()}"
    if os.name != "nt" and "ATTACHE_ROLE" in os.environ:  # under Attaché: see wait()
        # Fails only for a session leader, which a script under uv never is: loud, if ever.
        os.setpgid(0, 0)
    with log.open("ab") as out:  # "a": a run in the same second keeps the last one's log
        prune(log.parent, name)
        began = time.monotonic()
        try:
            # First, as Windows looks for programs in the current folder: this runs in the
            # chat's files folder, where others may put files.
            os.chdir(home / job["cwd"])
            program = shutil.which(job["command"][0]) or job["command"][0]
            proc = subprocess.Popen(
                [program, *job["command"][1:]],
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # POSIX: kill() kills its process group
            )
        except OSError as error:
            owner = f"{name} couldn't start: {error}, {where}"
            raise JobError(f"{title(job)} failed.", owner) from None
        finished = wait(proc, job["timeout_minutes"] * 60)
    seconds = time.monotonic() - began
    took = f"{seconds:.0f} sec" if seconds < 60 else f"{seconds / 60:.0f} min"
    if finished and proc.returncode == 0:
        return {"ok": True, "text": f"{title(job)} finished in {took}."}
    what = f"exited with code {proc.returncode}" if finished else "was stopped"
    text = f"{title(job)} failed." if finished else f"{title(job)} was stopped after {took}."
    raise JobError(text, f"{name} {what} after {took}, {where}")


# The commands: each takes (home, args) and returns its JSON object.


def list_jobs(home: Path, args: argparse.Namespace) -> Json:
    """The jobs this caller may start."""
    who, owner = caller()
    found = []
    for name, job in load(home).items():
        if owner or who in job["who"]:
            at = since(home / LOGS / f"{name}.lock")
            found.append({"name": name, "label": job["label"], "running": bool(at), "since": at})
    return {"ok": True, "jobs": found}


def start(home: Path, args: argparse.Namespace) -> Json:
    who, owner = caller()
    if not owner and not background():  # before anything: their model would read the result
        raise JobError("Start it in the background.")
    name = args.name
    jobs = load(home)
    if name not in jobs:  # every name in jobs is plain, so an odd one stops here, never echoed
        what = f"{name!r}, which isn't in jobs.toml" if PLAIN.fullmatch(name) else "a bad name"
        raise JobError(NOT_YOURS, f"{who} asked to start {what}")
    if not owner and who not in jobs[name]["who"]:
        raise JobError(NOT_YOURS, f"{who} asked to start {name!r}, but its who doesn't list them")
    lock = home / LOGS / f"{name}.lock"
    if (fd := take(lock)) is None:
        started = since(lock)
        at = f" (since {datetime.fromisoformat(started):%H:%M})" if started else ""
        return {"ok": True, "text": f"{title(jobs[name])} is already running{at}."}
    try:
        return run(home, name, jobs[name], who, fd)
    finally:
        let_go(fd)


def setup(home: Path, args: argparse.Namespace) -> Json:
    if not caller()[1]:
        raise JobError("Only the owner can set up jobs.")
    target = home / "skills" / "jobs" / "jobs.toml"
    created = not target.exists()
    if created:
        shutil.copyfile(HERE.parent / "jobs.example.toml", target)
    return {"ok": True, "path": str(target), "created": created}


COMMANDS = {"list": list_jobs, "start": start, "setup": setup}


def parser() -> Parser:
    top = Parser(prog="jobs.py", description="Jobs commands; each prints one JSON object.")
    sub = top.add_subparsers(dest="command", required=True)
    commands = {name: sub.add_parser(name) for name in COMMANDS}
    commands["start"].add_argument("name")  # a name alone: anything more is refused
    return top


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # labels in any script, even down a Windows pipe
    # $ATTACHE_HOME, which Attaché sets; else the home this script is installed in
    home = Path(os.environ.get("ATTACHE_HOME") or HERE.parents[2]).resolve()
    try:
        args = parser().parse_args(argv)
        result = COMMANDS[args.command](home, args)
    except JobError as error:
        result = {"ok": False, "text": error.text, "owner": error.owner}
    except (Exception, KeyboardInterrupt) as error:  # whatever happens, the last line is JSON
        what = f"jobs.py failed for {caller()[0]}: {type(error).__name__}: {error}"
        result = {"ok": False, "text": "That couldn't be done just now.", "owner": what}
    if not caller()[1] and not background():  # their model reads this: `owner` isn't for it
        result.pop("owner", None)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
