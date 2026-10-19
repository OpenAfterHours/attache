"""The tools a turn may use, filtered by role. Each tool's docstring is its description for the
model. Tools return strings (JSON for structured results) and may simply raise: agent.run_tool
turns exceptions into "error: ..." text.

An owner turn in a group chat acts with member rights (see access()): a group is exposed to
other people's messages, so it never gets files, the shell, or the owner's private documents.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import unicodedata
from asyncio.subprocess import DEVNULL, PIPE, STDOUT
from datetime import datetime
from pathlib import Path
from typing import Any

from attache import skills, store
from attache.types import Outbound, Tool, TurnContext

OWNER, BOTH = frozenset({"owner"}), frozenset({"owner", "member"})
OUTPUT_CAP = 20_000  # characters of command output kept: the end, where errors are
SHELL = "cmd.exe" if os.name == "nt" else "sh"
PRIVATE = {".secret", ".git", ".claude", "claude.md"}  # and attache.db*; any case, any depth
NUM, ARRAY = {"type": "number"}, {"type": "array", "items": {"type": "string"}}
TYPES = {"timeout": NUM, "in_minutes": NUM, "all": {"type": "boolean"}, "data": {"type": "object"}}
TYPES |= {"args": ARRAY, "files": ARRAY}  # every other argument is a string
TOOLS: list[Tool] = []


def slug(text: str) -> str:
    """Filesystem-safe name: lowercase letters, digits and hyphens, at most 60 characters."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60].strip("-") or "untitled"


def chat_slug(chat_key: str) -> str:
    """A folder name for a chat: readable, and distinct even when two keys slug alike."""
    digest = hashlib.sha256(chat_key.encode("utf-8")).hexdigest()[:8]
    return f"{slug(chat_key)[:40].strip('-')}-{digest}"


def chat_files(ctx: TurnContext) -> Path:
    """<home>/files/<chat_slug(chat_key)>/, created on first use."""
    path = ctx.services.home / "files" / chat_slug(ctx.chat_key)
    path.mkdir(parents=True, exist_ok=True)
    return path


def access(role: str, is_group: bool) -> str:
    """The rights a turn acts with: an owner turn in a group gets member rights."""
    return "member" if role == "owner" and is_group else role


def resolve(ctx: TurnContext, path: str) -> Path:
    """Absolute path for `path` (relative to <home>). Refused before any filesystem call:
    absolute paths, drive letters (also "C:foo"), UNC and other paths starting with \\ or /, and
    any ":". Refused after resolving: anything outside <home> (also through symlinks and
    junctions), and the files Attaché keeps to itself."""
    if not path or ":" in path or path.startswith(("/", "\\")):
        raise PermissionError(f"{path!r}: use a path relative to the home folder")
    home = ctx.services.home.resolve()
    full = (home / path).resolve()
    if not full.is_relative_to(home):
        raise PermissionError(f"{path} is outside the home folder")
    for part in full.relative_to(home).parts:
        name = part.lower().rstrip(". ")  # Windows ignores trailing dots and spaces
        if name in PRIVATE or name.startswith("attache.db"):
            raise PermissionError(f"{path}: Attaché keeps that file to itself")
    return full


def for_members(skill: skills.Skill) -> bool:
    """True when the skill's metadata "attache-roles" (a list, or a string of names) has
    "member"."""
    meta = skill.meta.get("metadata")
    roles = meta.get("attache-roles") if isinstance(meta, dict) else None
    roles = roles.replace(",", " ").split() if isinstance(roles, str) else roles
    return isinstance(roles, list) and "member" in roles


def _tool(fields: str, roles: frozenset[str] = OWNER, description: str = "") -> Any:
    """Register a tool. fields: argument names; a trailing "?" marks an optional one."""
    names = fields.replace("?", "").split()
    required = [field for field in fields.split() if not field.endswith("?")]
    properties = {name: dict(TYPES.get(name, {"type": "string"})) for name in names}
    schema = {"type": "object", "properties": properties, "required": required}

    def register(fn: Any) -> Any:
        text = " ".join((description or fn.__doc__ or "").split())
        TOOLS.append(Tool(fn.__name__, text, schema, fn, roles))
        return fn

    return register


async def _exec(cmd: str | list[str], cwd: Path, timeout: float) -> str:
    """Run a command (a string runs in the shell); JSON {exit_code, output}. On timeout the
    whole process tree is killed, then reaped so its pipes close."""
    # start_new_session: on POSIX its own process group, killed as one; ignored on Windows
    kw = {"cwd": cwd, "stdin": DEVNULL, "stdout": PIPE, "stderr": STDOUT, "start_new_session": True}
    if isinstance(cmd, str):
        proc = await asyncio.create_subprocess_shell(cmd, **kw)
    else:
        proc = await asyncio.create_subprocess_exec(*cmd, **kw)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
        raise TimeoutError(f"stopped after {timeout:g} seconds") from None
    text = out.decode("utf-8", "replace")[-OUTPUT_CAP:]
    return json.dumps({"exit_code": proc.returncode, "output": text}, ensure_ascii=False)


@_tool("path")
async def read_file(args: dict[str, Any], ctx: TurnContext) -> str:
    """Read a text file. Paths are relative to the home folder."""
    return resolve(ctx, args["path"]).read_text(encoding="utf-8")


@_tool("path content")
async def write_file(args: dict[str, Any], ctx: TurnContext) -> str:
    """Create or overwrite a text file, making parent folders as needed."""
    path = resolve(ctx, args["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"wrote {path.write_text(args['content'], encoding='utf-8')} characters"


@_tool("path old new")
async def edit_file(args: dict[str, Any], ctx: TurnContext) -> str:
    """Replace the text `old` with `new` in a file. `old` must occur exactly once."""
    path = resolve(ctx, args["path"])
    text = path.read_text(encoding="utf-8")
    if text.count(args["old"]) != 1:
        raise ValueError(f"`old` must occur exactly once in {args['path']}")
    path.write_text(text.replace(args["old"], args["new"]), encoding="utf-8")
    return f"edited {args['path']}"


RUN = f"Run a {SHELL} command in the home folder; timeout in seconds, default 60, at most 600."


@_tool("command timeout?", description=RUN + " Returns JSON {exit_code, output}.")
async def run(args: dict[str, Any], ctx: TurnContext) -> str:
    timeout = min(float(args.get("timeout") or 60), 600)
    return await _exec(args["command"], ctx.services.home, timeout)


@_tool("skill script args?", BOTH)
async def script(args: dict[str, Any], ctx: TurnContext) -> str:
    """Run skills/<skill>/scripts/<script> with `uv run`, in this chat's files folder. Returns
    JSON {exit_code, output}."""
    name, file = str(args["skill"]), str(args["script"])
    for part in (name, file):  # plain names only: no drive, folder, ".." or ":"
        if not re.fullmatch(r"[\w.-]+", part) or part in (".", ".."):
            raise PermissionError(f"{part!r}: give a plain name, without folders")
    skill = next((s for s in skills.discover(ctx.services.home) if s.name == name), None)
    path = skill.path / "scripts" / file if skill else None
    if path is None or not path.is_file():
        raise FileNotFoundError(f"skill {name!r} has no script {file!r}")
    if access(ctx.role, ctx.is_group) != "owner" and not for_members(skill):
        raise PermissionError(f"skill {name!r} is not available in this chat")
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is not installed: see https://docs.astral.sh/uv/")
    argv = [uv, "run", str(path), *map(str, args.get("args") or [])]
    return await _exec(argv, chat_files(ctx), 600)


@_tool("text files?", BOTH)
async def send(args: dict[str, Any], ctx: TurnContext) -> str:
    """Send a message to this chat now, with optional files (paths relative to home; in shared
    chats, only files from this chat's files folder)."""
    owner = access(ctx.role, ctx.is_group) == "owner"
    folder = (ctx.services.home if owner else chat_files(ctx)).resolve()
    files = [resolve(ctx, f) for f in args.get("files") or []]
    for path in files:
        if not path.is_relative_to(folder) or not path.is_file():
            raise PermissionError(f"{path.name}: not a file in {folder}")
    text = str(args.get("text") or "")
    await ctx.services.channels.send(Outbound(ctx.chat_key, text, [str(p) for p in files]))
    if text:  # so it is in the chat's history
        ctx.services.store.add_message(ctx.chat_key, "assistant", text)
    return "sent"


@_tool("op collection id? data?", BOTH)
async def db(args: dict[str, Any], ctx: TurnContext) -> str:
    """Documents in named collections, e.g. lists and trackers. op: list (all docs) | get (by
    id) | put (replace the doc with `data`; no id creates one) | delete (by id)."""
    chat = None if access(ctx.role, ctx.is_group) == "owner" else ctx.chat_key
    request = (args["op"], args["collection"], args.get("id"), args.get("data"))
    found = store.db_access(ctx.services.store, *request, by=ctx.sender.id, chat=chat)
    return json.dumps(found, default=str, ensure_ascii=False)


@_tool("name html", BOTH)
async def artifact(args: dict[str, Any], ctx: TurnContext) -> str:
    """Publish a full HTML page (a list, board, chart or form) and get its URL. A page that
    reads or writes db collections names them in its head: <meta name="attache-collections"
    content="todos,notes">; the attache bridge then keeps it live."""
    services, name = ctx.services, slug(args["name"])
    record = services.store.doc_get("_artifacts", name)
    # A page published by a member, or in a group, may use only this chat's shared docs.
    shared = access(ctx.role, ctx.is_group) != "owner"
    taken = (services.home / "artifacts" / f"{name}.html").exists()
    if shared and taken and (record or {}).get("chat_key") != ctx.chat_key:
        raise PermissionError(f"an artifact named {name!r} exists; choose another name")
    await write_file({"path": f"artifacts/{name}.html", "content": args["html"]}, ctx)
    if shared:
        services.store.doc_put("_artifacts", name, {"chat_key": ctx.chat_key}, by=ctx.sender.id)
    return json.dumps({"url": f"{services.web_url}/#page={name}"})  # opens in Attaché's window


@_tool("op text? at? in_minutes? cron? kind? id? all?", BOTH)
async def schedule(args: dict[str, Any], ctx: TurnContext) -> str:
    """Reminders and later work in this chat. op add: text plus one of `at` (ISO date-time,
    local unless it has an offset), `in_minutes` or `cron` (5 fields, local time); kind
    "message" (default) sends the text then, "turn" runs it as a request. op list: pending
    tasks (all=true: every chat). op cancel: by id."""
    op, owner, saved = args["op"], access(ctx.role, ctx.is_group) == "owner", ctx.services.store
    if op == "list":
        return json.dumps(saved.task_list(None if owner and args.get("all") else ctx.chat_key))
    if op == "cancel":
        chat = None if owner else ctx.chat_key
        mine = [t["id"] for t in saved.task_list(chat) if owner or t["by"] == ctx.sender.id]
        if int(args["id"]) not in mine:
            raise KeyError(f"no pending task {args['id']} that you can cancel")
        saved.task_cancel(int(args["id"]))
        return f"cancelled task {args['id']}"
    if op != "add":
        raise ValueError(f"unknown op {op!r}: use add, list or cancel")
    kind = args.get("kind") or "message"  # "" counts as absent, here and for at and cron
    if kind not in ("message", "turn") or (kind == "turn" and not owner):
        raise PermissionError(f"kind {kind!r} is not allowed here; use kind 'message'")
    at = datetime.fromisoformat(args["at"]).timestamp() if args.get("at") else None  # naive: local
    if at is None and args.get("in_minutes") not in (None, ""):
        at = datetime.now().timestamp() + 60 * float(args["in_minutes"])
    cron = args.get("cron") or None
    scheduler = ctx.services.scheduler
    task = scheduler.add(ctx.chat_key, args["text"], at=at, cron=cron, kind=kind, by=ctx.sender.id)
    return json.dumps(task)


def build(ctx: TurnContext) -> list[Tool]:
    """Every tool the turn's rights allow (access()); tools read ctx at call time."""
    return [t for t in TOOLS if access(ctx.role, ctx.is_group) in t.roles]
