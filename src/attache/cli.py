"""Attaché, your personal assistant. With no command: set up a home if needed, then chat.

Commands: init [--name N] [--endpoint E], chat, run (no terminal chat), doctor, skills list |
log [NAME] | undo NAME. --home (or $ATTACHE_HOME) picks the home folder everywhere."""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import socket
import sys
from contextlib import suppress
from pathlib import Path

from attache import __version__, config, mcp, skills


def main(argv: list[str] | None = None) -> int:
    """Run the `attache` command and return a process exit code."""
    with suppress(AttributeError, ValueError, OSError):  # Windows consoles and pipes
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser("attache", description=__doc__)
    ap.add_argument("cmd", nargs="?", choices=["init", "chat", "run", "skills", "doctor", "mcp"])
    ap.add_argument("args", nargs="*", help="for skills: list | log [NAME] | undo NAME")
    for option in ("--home", "--name", "--endpoint", "--url", "--token"):  # mcp: --url, --token
        ap.add_argument(option)
    ap.add_argument("--version", action="version", version=f"attache {__version__}")
    args = ap.parse_args(argv)
    cmd, home = args.cmd, Path(args.home or config.home_dir()).expanduser()
    toml, user = home / "attache.toml", os.environ.get("USERNAME") or os.environ.get("USER")
    if cmd == "mcp":
        return mcp.serve(args.url, args.token) or 0
    if cmd == "init" or (cmd is None and not toml.exists()):
        name = args.name or (cmd is None and input(f"Your name [{user or 'you'}]: ").strip())
        made = config.init_home(home, name or user or "you", args.endpoint or "claude")
        print(f"Home: {home}", *(f"  created {p.relative_to(home)}" for p in made), sep="\n")
        print("Next: describe yourself in memory/USER.md, check attache.toml, run `attache`.")
        if cmd == "init":
            return 0
    if cmd == "skills":
        action, name, *_ = [*(args.args or ["list"]), None]
        if action not in ("list", "log"):  # undo, or a typo
            sha = name and skills.undo(home, name)
            print(f"Undid the last change to {name}: commit {sha}" if sha else "Nothing to undo.")
            return 0 if sha else 1
        found = [f"{s.name}: {s.description}" for s in skills.discover(home)]
        print("\n".join(found if action == "list" else skills.log(home, name)) or "Nothing yet.")
        return 0
    if cmd == "doctor":
        cfg = None
        with suppress(Exception):
            cfg = config.load(home) if toml.is_file() else None
        checks = [(f"home folder {home}", home.is_dir()), ("attache.toml parses", cfg is not None)]
        for e in cfg.endpoints.values() if cfg else []:
            need = e.options.get("bin", "claude") if e.api == "claude-cli" else e.key_env
            ok = shutil.which(need) if e.api == "claude-cli" else not need or os.environ.get(need)
            checks.append((f"endpoint {e.name}: needs {need or 'nothing'}", bool(ok)))
        checks += [(f"{tool} installed", bool(shutil.which(tool))) for tool in ("git", "uv")]
        free = False  # the same bind check as `attache run`
        with suppress(OSError, AttributeError), socket.create_server((cfg.host, cfg.port)):
            free = True
        checks.append(("web port free", free))
        print(*(f"{'✓' if ok else '✗'} {label}" for label, ok in checks), sep="\n")
        return 0 if all(ok for _, ok in checks) else 1
    if not toml.exists():
        print(f"No Attaché home at {home}: run `attache init` first.", file=sys.stderr)
        return 1
    from attache import app  # the web stack; `attache mcp` starts faster without it

    with suppress(KeyboardInterrupt):
        asyncio.run(app.run(home, terminal=cmd != "run"))
    return 0
