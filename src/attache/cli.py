"""Attaché, your personal assistant.

  attache                  set up a home if there is none (asks your name), then chat
  attache init [--name N] [--endpoint E]
  attache chat             run everything plus the terminal chat
  attache run              run everything without the terminal chat (web, scheduler, channels)
  attache skills list | log [NAME] | undo NAME
  attache skill NAME [ARGS...]  run skills/NAME/scripts/NAME.py with uv, e.g.
                           `attache skill mail login gmail`, `attache skill service install`
  attache doctor           check the setup, one line per check: ✓ fine, ! needed only for
                           group chats, i a note, ✗ a problem (then the exit code is 1)

--home (or $ATTACHE_HOME) picks the home folder everywhere; for `attache skill`, which hands
every other argument to the script, only $ATTACHE_HOME does. main() returns an exit code.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
from contextlib import suppress
from pathlib import Path

from attache import __version__, channels, config, credentials, skills

NEXT = """Next:
  1. Tell Attaché about yourself in {home}/memory/USER.md.
  2. Check the endpoints in {home}/attache.toml ("claude" uses your Claude Code login).
  3. Run `attache` to chat here, or `attache run` for the web page alone."""


def main(argv: list[str] | None = None) -> int:
    """Run the `attache` command and return a process exit code."""
    # Windows consoles and pipes: UTF-8. Apart, so a stdout that can't change keeps stdin's.
    with suppress(AttributeError, ValueError, OSError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    with suppress(AttributeError, ValueError, OSError):
        if not sys.stdin.isatty():  # a console already reads Unicode; a pipe or file may not
            sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["skill"]:  # before argparse, so options like --dry-run reach the script
        return _skill(config.home_dir().expanduser(), argv[1:])
    raw = argparse.RawDescriptionHelpFormatter
    parser = argparse.ArgumentParser("attache", description=__doc__, formatter_class=raw)
    parser.add_argument("command", nargs="?", choices=["init", "chat", "run", "skills", "doctor"])
    parser.add_argument("args", nargs="*", help="for skills: list | log [NAME] | undo NAME")
    parser.add_argument("--home", type=Path, help="the home folder (default ~/.attache)")
    parser.add_argument("--name", help="init: your name (default: your user name)")
    parser.add_argument("--endpoint", default="claude", help="init: endpoint for your own chats")
    parser.add_argument("--version", action="version", version=f"attache {__version__}")
    args = parser.parse_args(argv)
    home = (args.home or config.home_dir()).expanduser()
    user = os.environ.get("USERNAME") or os.environ.get("USER") or "you"
    if args.command == "init" or (args.command is None and not (home / "attache.toml").exists()):
        name = args.name or user
        if args.command is None:
            with suppress(EOFError):  # nobody to ask: keep the default
                name = input(f"Your name [{user}]: ").strip() or user
        made = config.init_home(home, name, args.endpoint)
        print(f"Home: {home}", *(f"  created {p.relative_to(home)}" for p in made), sep="\n")
        print(NEXT.format(home=home))
        if args.command:
            return 0
    if args.command == "skills":
        action, name = [*args.args, None, None][:2]
        if action in (None, "list"):
            print("\n".join(f"{s.name}: {s.description}" for s in skills.discover(home)) or "None.")
        elif action == "log":
            print("\n".join(skills.log(home, name)) or "No changes yet.")
        elif action == "undo" and name:
            sha = skills.undo(home, name)
            print(f"Undid the last change to {name} ({sha})." if sha else "Nothing to undo.")
            return 0 if sha else 1
        else:
            parser.error("use: attache skills list | log [NAME] | undo NAME")
        return 0
    return _doctor(home) if args.command == "doctor" else _run(home, terminal=args.command != "run")


def _skill(home: Path, argv: list[str]) -> int:
    """Run a skill's own script, such as the owner's one-time `mail login`, in this terminal."""
    name, *rest = argv or [""]
    script = home / "skills" / name / "scripts" / f"{name}.py"
    if not shutil.which("uv"):  # SystemExit with a message: that one line, and exit code 1
        raise SystemExit("uv is not installed: see https://docs.astral.sh/uv/")
    if not channels.PLAIN_NAME.fullmatch(name) or not script.is_file():
        hint = "new bundled skills arrive with `attache init`, which never overwrites"
        raise SystemExit(f"No skills/{name}/scripts/{name}.py in {home}: {hint}.")
    # ATTACHE_CLI: how to start this same Attaché, for a skill that runs one (the tui skill)
    env = {
        **os.environ,
        "ATTACHE_CLI": json.dumps([sys.executable, "-m", "attache"]),
        "ATTACHE_PACKAGE_ROOT": str(Path(__file__).resolve().parent.parent),
    }
    return subprocess.run(["uv", "run", str(script), *rest], env=env).returncode


def _doctor(home: Path) -> int:
    checks, cfg = [("✓" if home.is_dir() else "✗", f"home folder {home}")], None
    try:
        cfg = config.load(home) if (home / "attache.toml").is_file() else None
        checks.append(("✓", "attache.toml parses") if cfg else ("✗", "no attache.toml"))
    except Exception as e:
        checks.append(("✗", f"attache.toml: {e}"))
    routed = {cfg.own_chats, cfg.group_chats} if cfg else set()  # the endpoints chats reach
    routed |= {c.endpoint for c in cfg.chats.values() if c.endpoint} if cfg else set()
    for name in sorted(routed):
        ep = cfg.endpoints.get(name)
        if ep is None:
            need, ok = "an [endpoints] block", False
        elif ep.api == "claude-cli":
            need = ep.options.get("bin", "claude")
            ok = shutil.which(need) is not None
        elif ep.key_ref is not None:
            try:
                credentials.api_key(ep)
                need, ok = "", True
            except credentials.CredentialError as e:
                need, ok = str(e).removeprefix(f"endpoint '{name}': "), False
        else:
            need, ok = ep.key_env, not ep.key_env or bool(os.environ.get(ep.key_env))
        mark = "✓" if ok else "✗" if name == cfg.own_chats else "!"  # "!": only groups need it
        detail = " (keyring credential available)" if ok and ep and ep.key_ref else ""
        if not ok:
            detail = f": {need}" if ep and ep.key_ref else f" needs {need}"
        checks.append((mark, f"endpoint {name}{detail}"))
    checks += [("✓" if shutil.which(tool) else "✗", f"{tool} installed") for tool in ("git", "uv")]
    if cfg:
        found = {s.name: s for s in skills.discover(home)}
        for name in [n for n, settings in cfg.channels.items() if settings.get("enabled")]:
            # the test sidecars() makes (uv is checked above), so the two always agree
            ok = name in found and channels.channel_script(found[name]) is not None
            mark = "✓" if ok else "✗"
            checks.append((mark, f"channel {name}: skills/{name} names its sidecar script"))
        from attache import app  # the web stack, for the same bind check as `attache run`

        try:
            app.listen(cfg.host, cfg.port).close()
            checks.append(("✓", f"web port {cfg.port} free"))
        except OSError:
            checks.append(("i", f"web port {cfg.port} in use: Attaché may already be running"))
        except SystemExit as e:
            checks.append(("✗", str(e)))
    print("\n".join(f"{mark} {text}" for mark, text in checks))
    return 1 if any(mark == "✗" for mark, _ in checks) else 0


def _run(home: Path, *, terminal: bool) -> int:
    if not (home / "attache.toml").exists():
        print(f"No Attaché home at {home}: run `attache init` first.", file=sys.stderr)
        return 1
    try:
        config.load(home)
    except (ValueError, TypeError) as e:  # a bad attache.toml: one line, not a traceback
        print(f"attache.toml: {e}", file=sys.stderr)
        return 1
    # Attaché's own news (a sidecar ready, its log lines, restarts), but only warnings from the
    # libraries, such as httpx's line for every request.
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("attache").setLevel(logging.INFO)
    from attache import app  # the web stack; `attache skills` and `init` start faster without

    with suppress(KeyboardInterrupt):
        asyncio.run(app.run(home, terminal=terminal))
    return 0
