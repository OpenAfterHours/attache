"""The tools a turn may use, by role. Each tool's docstring is its description for the model."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import unicodedata
from asyncio.subprocess import PIPE, STDOUT
from datetime import datetime
from pathlib import Path
from typing import Any

from attache import skills, store
from attache.types import Outbound, Tool, TurnContext

BOTH = frozenset({"owner", "member"})
NUM, ARRAY = {"type": "number"}, {"type": "array", "items": {"type": "string"}}
TYPES = {"timeout": NUM, "in_minutes": NUM, "all": {"type": "boolean"}, "args": ARRAY}
TYPES |= {"files": ARRAY, "collections": ARRAY, "data": {"type": "object"}}  # others: string
TOOLS: list[Tool] = []  # filled by @_tool("arg optional_arg?", roles)


def slug(text: str) -> str:
    """Filesystem-safe name: lowercase letters, digits and hyphens, at most 60 characters."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60].strip("-") or "untitled"


def chat_files(ctx: TurnContext) -> Path:
    """<home>/files/<slug(chat_key)>/, created on first use."""
    (path := ctx.services.home / "files" / slug(ctx.chat_key)).mkdir(parents=True, exist_ok=True)
    return path


def resolve(ctx: TurnContext, path: str) -> Path:
    """Absolute path for `path` (relative to <home>), refusing anything outside <home>."""
    if not (full := ((home := ctx.services.home.resolve()) / path).resolve()).is_relative_to(home):
        raise PermissionError(f"{path} is outside the home folder")
    return full


def for_members(skill: skills.Skill) -> bool:
    """True when the skill's metadata "attache-roles" (a string or a list) names "member"."""
    return "member" in str((skill.meta.get("metadata") or {}).get("attache-roles"))


def _tool(fields: str, roles: frozenset[str] = frozenset({"owner"})) -> Any:
    props = {n: dict(TYPES.get(n, {"type": "string"})) for n in fields.replace("?", "").split()}
    schema = {"type": "object", "properties": props, "required": re.findall(r"\w+(?!\S)", fields)}
    return lambda fn: TOOLS.append(Tool(fn.__name__, str(fn.__doc__), schema, fn, roles)) or fn


async def _exec(spawn: Any, cmd: list[str], cwd: Path, timeout: float) -> str:
    proc = await spawn(*cmd, cwd=cwd, stdout=PIPE, stderr=STDOUT)
    try:
        out = (await asyncio.wait_for(proc.communicate(), timeout))[0].decode("utf-8", "replace")
    except TimeoutError:
        proc.kill()
        raise TimeoutError(f"stopped after {timeout:g} seconds") from None
    return json.dumps({"exit_code": proc.returncode, "output": out[-20_000:]}, ensure_ascii=False)


@_tool("path")
async def read_file(args: dict[str, Any], ctx: TurnContext) -> str:
    """Read a text file. Paths are relative to the home folder."""
    return resolve(ctx, args["path"]).read_text(encoding="utf-8")


@_tool("path content")
async def write_file(args: dict[str, Any], ctx: TurnContext) -> str:
    """Create or overwrite a text file, making parent folders as needed."""
    (path := resolve(ctx, args["path"])).parent.mkdir(parents=True, exist_ok=True)
    return f"wrote {path.write_text(args['content'], encoding='utf-8')} characters"


@_tool("path old new")
async def edit_file(args: dict[str, Any], ctx: TurnContext) -> str:
    """Replace the text `old` with `new` in a file. `old` must occur exactly once."""
    text = (path := resolve(ctx, args["path"])).read_text(encoding="utf-8")
    if (count := text.count(args["old"])) != 1:
        raise ValueError(f"`old` occurs {count} times in {args['path']}; it must occur once")
    path.write_text(text.replace(args["old"], args["new"]), encoding="utf-8")
    return f"edited {args['path']}"


@_tool("command timeout?")
async def run(args: dict[str, Any], ctx: TurnContext) -> str:
    """Run a shell command in the home folder; timeout in seconds (default 60)."""
    timeout, shell = float(args.get("timeout") or 60), asyncio.create_subprocess_shell
    return await _exec(shell, [args["command"]], ctx.services.home, timeout)


@_tool("skill script args?", BOTH)
async def script(args: dict[str, Any], ctx: TurnContext) -> str:
    """Run skills/<skill>/scripts/<script> with `uv run`, in this chat's files folder."""
    name, file = str(args["skill"]), str(args["script"])
    skill = next((s for s in skills.discover(ctx.services.home) if s.name == name), None)
    path = skill.path / "scripts" / file if skill else None
    if re.search(r"[/\\]|\.\.", name + file) or path is None or not path.is_file():
        raise FileNotFoundError(f"skill {name!r} has no script {file!r}")
    if ctx.role != "owner" and not for_members(skill):
        raise PermissionError(f"skill {name!r} is not available in this chat")
    if not (uv := shutil.which("uv")):
        raise RuntimeError("uv is not installed: see https://docs.astral.sh/uv/")
    argv = [uv, "run", str(path), *map(str, args.get("args") or [])]
    return await _exec(asyncio.create_subprocess_exec, argv, chat_files(ctx), 600)


@_tool("text files?", BOTH)
async def send(args: dict[str, Any], ctx: TurnContext) -> str:
    """Send a message to this chat now, with optional files (paths relative to home)."""
    files, sv = [resolve(ctx, f) for f in args.get("files") or []], ctx.services
    folder = (sv.home if ctx.role == "owner" else chat_files(ctx)).resolve()  # members: this chat's
    if bad := [p.name for p in files if not (p.is_file() and p.is_relative_to(folder))]:
        raise FileNotFoundError(f"cannot send {bad}: no such file in {folder}")
    await sv.channels.send(Outbound(ctx.chat_key, args.get("text") or "", [*map(str, files)]))
    return "sent"


@_tool("op collection id? data?", BOTH)
async def db(args: dict[str, Any], ctx: TurnContext) -> str:
    """Documents in collections. op: list, get (id), put (data replaces it; no id: new), delete."""
    chat = None if ctx.role == "owner" else ctx.chat_key
    call = (ctx.services.store, args["op"], args["collection"], args.get("id"), args.get("data"))
    return json.dumps(store.db_access(*call, by=ctx.sender.id, chat=chat), ensure_ascii=False)


@_tool("name html collections?", BOTH)
async def artifact(args: dict[str, Any], ctx: TurnContext) -> str:
    """Publish an HTML page and get its URL. Its script uses the named db collections, live."""
    name, html, sv = slug(args["name"]), args["html"], ctx.services
    path, old = sv.home / "artifacts" / f"{name}.html", sv.store.doc_get("_artifacts", name)
    if ctx.role != "owner" and path.exists() and (old or {}).get("chat_key") != ctx.chat_key:
        raise PermissionError(f"an artifact named {name!r} exists; choose another name")
    await write_file({"path": f"artifacts/{name}.html", "content": html}, ctx)  # makes the folder
    title = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    chat = None if ctx.role == "owner" and not ctx.is_group else ctx.chat_key  # else shared docs
    record = {"name": args["name"], "title": title.group(1).strip() if title else args["name"]}
    record |= {"collections": args.get("collections") or [], "chat_key": chat, "by": ctx.sender.id}
    sv.store.doc_put("_artifacts", name, record, by=ctx.sender.id)
    return json.dumps({"url": f"{sv.web_url}/a/{name}"})


@_tool("op text? at? in_minutes? cron? kind? id? all?", BOTH)
async def schedule(args: dict[str, Any], ctx: TurnContext) -> str:
    """Reminders in this chat. add: text, plus at (ISO, local unless offset), in_minutes or cron;
    kind "turn" runs it as a request, else it is sent. list (all=true: every chat). cancel: id."""
    op, owner, st, by = args["op"], ctx.role == "owner", ctx.services.store, ctx.sender.id
    scope, add, key = None if owner else ctx.chat_key, ctx.services.scheduler.add, ctx.chat_key
    if op == "list":
        return json.dumps(st.task_list(None if owner and args.get("all") else key))
    if op == "cancel":
        if int(args["id"]) not in [t["id"] for t in st.task_list(scope) if owner or t["by"] == by]:
            raise KeyError(f"no pending task {args['id']} that you can cancel")
        st.task_cancel(int(args["id"]))
        return f"cancelled task {args['id']}"
    kind = args.get("kind") or "message"
    if op != "add" or kind not in ("message", "turn") or (kind == "turn" and not owner):
        raise ValueError(f"cannot {op} a {kind!r} task here")
    at = datetime.fromisoformat(args["at"]).timestamp() if args.get("at") else None  # naive: local
    if at is None and args.get("in_minutes") is not None:
        at = datetime.now().timestamp() + 60 * float(args["in_minutes"])
    return json.dumps(add(key, args["text"], at=at, cron=args.get("cron"), kind=kind, by=by))


def build(ctx: TurnContext) -> list[Tool]:
    """Every tool whose roles include ctx.role. Tools read ctx at call time."""
    return [t for t in TOOLS if ctx.role in t.roles]
