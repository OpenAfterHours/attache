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
import unicodedata
from asyncio.subprocess import DEVNULL, PIPE, STDOUT
from datetime import datetime
from pathlib import Path
from typing import Any

from attache import channels, scheduler, skills, store, web
from attache.types import Outbound, Tool, TurnContext

OWNER, MEMBER, BOTH = frozenset({"owner"}), frozenset({"member"}), frozenset({"owner", "member"})
# Unicode categories quote() escapes: controls, format characters (bidi controls, zero-width
# characters, the BOM), lone surrogates, and the line and paragraph separators.
HIDDEN = {"Cc", "Cf", "Cs", "Zl", "Zp"}
OUTPUT_CAP = 20_000  # characters of command output kept: the end, where errors are
SHELL = "cmd.exe" if os.name == "nt" else "sh"
# What file tools may not touch, in any case and at any depth (and attache.db*): .mail holds
# mail logins, and .approvals the owner's approvals for mail sends (see ask).
PRIVATE = {".secret", ".git", ".claude", "claude.md", ".mail", ".approvals"}
# Script arguments that are data, never paths, so they may hold / and : without owner rights:
# web addresses, and inline JSON (members pass docx, xlsx and pptx specs that way).
DATA = ("http://", "https://", "{")
NUM, ARRAY = {"type": "number"}, {"type": "array", "items": {"type": "string"}}
TYPES = {"timeout": NUM, "in_minutes": NUM, "all": {"type": "boolean"}, "data": {"type": "object"}}
TYPES |= {"args": ARRAY, "files": ARRAY, "options": ARRAY}  # every other argument is a string
TOOLS: list[Tool] = []


def slug(text: str) -> str:
    """Filesystem-safe name: lowercase letters, digits and hyphens, at most 60 characters."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60].strip("-") or "untitled"


def chat_slug(chat_key: str) -> str:
    """A folder name for a chat: readable, and distinct even when two keys slug alike."""
    digest = hashlib.sha256(chat_key.encode()).hexdigest()[:8]
    return f"{slug(chat_key)[:40].strip('-')}-{digest}"


def quote(text: str) -> str:
    """Someone else's text as one JSON string on one line: inside its quotes, nothing can pose
    as a header or a label, or close a <<< >>> fence. Every invisible or line-breaking
    character (HIDDEN) is written as its JSON escape, so the text shows exactly what the model
    reads; json.dumps alone would keep these raw."""
    # json.dumps(c) is the ASCII escape, as a surrogate pair beyond U+FFFF
    escapes = {ord(c): json.dumps(c)[1:-1] for c in text if unicodedata.category(c) in HIDDEN}
    return json.dumps(text, ensure_ascii=False).translate(escapes)


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
    """Run a command (a string runs in the shell); JSON {exit_code, output}. On timeout
    channels.kill_tree kills the whole process tree and reaps it, so its pipes close."""
    # start_new_session: on POSIX its own process group, which kill_tree kills as one
    kw = {"cwd": cwd, "stdin": DEVNULL, "stdout": PIPE, "stderr": STDOUT, "start_new_session": True}
    if isinstance(cmd, str):
        proc = await asyncio.create_subprocess_shell(cmd, **kw)
    else:
        proc = await asyncio.create_subprocess_exec(*cmd, **kw)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        await channels.kill_tree(proc)
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
    JSON {exit_code, output}. Without owner rights, args are file names in that folder, web
    addresses or inline JSON ({...}): nothing else with /, \\, : or .. in it."""
    name, file = str(args["skill"]), str(args["script"])
    for part in (name, file):  # plain names only: no drive, folder, ".." or ":"
        if not re.fullmatch(r"[\w.-]+", part) or part in (".", ".."):
            raise PermissionError(f"{part!r}: give a plain name, without folders")
    skill = next((s for s in skills.discover(ctx.services.home) if s.name == name), None)
    path = skill.path / "scripts" / file if skill else None
    if path is None or not path.is_file():
        raise FileNotFoundError(f"skill {name!r} has no script {file!r}")
    owner = access(ctx.role, ctx.is_group) == "owner"
    if not owner and not for_members(skill):
        raise PermissionError(f"skill {name!r} is not available in this chat")
    extra = [str(arg) for arg in args.get("args") or []]
    for arg in extra:  # without owner rights, nothing that could name a path, but DATA
        if not owner and re.search(r"[/\\:]|\.\.", arg) and not arg.startswith(DATA):
            raise PermissionError(f"{arg!r}: give a file name from this chat's folder, or a URL")
    if not (uv := shutil.which("uv")):
        raise RuntimeError("uv is not installed: see https://docs.astral.sh/uv/")
    return await _exec([uv, "run", str(path), *extra], chat_files(ctx), 600)


@_tool("text files?", BOTH)
async def send(args: dict[str, Any], ctx: TurnContext) -> str:
    """Send a message to this chat now, with optional files (paths relative to home; in shared
    chats, only files from this chat's files folder)."""
    owner = access(ctx.role, ctx.is_group) == "owner"
    folder = ctx.services.home if owner else chat_files(ctx)
    files = [resolve(ctx, f) for f in args.get("files") or []]
    for path in files:
        if not path.is_relative_to(folder.resolve()) or not path.is_file():
            raise PermissionError(f"{path.name}: not a file in {folder}")
    text = str(args.get("text") or "")
    await ctx.services.channels.send(Outbound(ctx.chat_key, text, [str(p) for p in files]))
    shown = text or f"(sent {', '.join(p.name for p in files)})"  # kept in the chat's history
    ctx.services.store.add_message(ctx.chat_key, "assistant", shown)
    return "sent"


@_tool("op collection id? data?", BOTH | {"page"})  # "page": a page's turn (page_turn)
async def db(args: dict[str, Any], ctx: TurnContext) -> str:
    """Documents in named collections, e.g. lists and trackers. op: list (all docs) | get (by
    id) | put (replace the doc with `data`; no id creates one) | delete (by id)."""
    # Without owner rights, only the docs shared with this chat; a page's turn, only its
    # collections too.
    chat = None if access(ctx.role, ctx.is_group) == "owner" else ctx.chat_key
    if ctx.collections is not None and args["collection"] not in ctx.collections:
        raise PermissionError(f"this page uses only {', '.join(sorted(ctx.collections))}")
    call = (args["op"], args["collection"], args.get("id"), args.get("data"))
    found = store.db_access(ctx.services.store, *call, by=ctx.sender.id, chat=chat)
    return json.dumps(found, default=str, ensure_ascii=False)


@_tool("name html", BOTH)
async def artifact(args: dict[str, Any], ctx: TurnContext) -> str:
    """Publish a full HTML page (a list, board, chart or form) and get its URL. A page that
    reads or writes db collections names them in its head: <meta name="attache-collections"
    content="todos,notes">; the attache bridge then keeps it live. In a shared chat, when
    sharing is on, the result also has a "link" anyone can open for 7 days."""
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
    url = f"{services.web_url}/#page={name}"  # opens in Attaché's window
    links = {"url": url, "snapshot": web.snapshot_url(services, name)}  # snapshot: for render
    if shared and services.config.public_url:  # a chat's page; the owner's own stay private
        links["link"] = web.share_url(services, name)
    return json.dumps(links)


async def _ask(services: Any, chat_key: str, question: str, options: list[str]) -> Any:
    """Put a question to the owner in `chat_key`. Once a surface has shown it, it is kept in
    that chat's history, and the result is a task giving the choice (None: no answer in time).
    None at once when nobody saw it, which is then never kept, so a later "Approve" typed
    there is just a message."""
    answer = await services.channels.ask(chat_key, "owner", question, options)
    if answer is not None:
        services.store.add_message(chat_key, "assistant", question)
    return answer


@_tool("question options? approves?")
async def ask(args: dict[str, Any], ctx: TurnContext) -> str:
    """Ask the owner a question in this chat and wait for the answer; options default to Yes
    and No. Returns the chosen option, "no answer", or "couldn't reach the owner just now".
    approves: the 8-64 letter and digit fingerprint a skill gave for an action, such as a mail
    send; when the owner picks the first option, .approvals/<approves> is written for it."""
    options = list(args.get("options") or ["Yes", "No"])
    approves = str(args.get("approves") or "")
    if approves and not re.fullmatch(r"[A-Za-z0-9]{8,64}", approves):
        raise ValueError("approves must be 8 to 64 letters and digits")
    answer = await _ask(ctx.services, ctx.chat_key, args["question"], options)
    if answer is None:  # no surface showed the question
        return "couldn't reach the owner just now"
    choice = await answer
    if approves and choice == options[0]:  # the core's mark, which the skill checks and removes
        (ctx.services.home / ".approvals").mkdir(exist_ok=True)
        (ctx.services.home / ".approvals" / approves).touch()
    return choice or "no answer"


@_tool("summary", MEMBER)
async def request(args: dict[str, Any], ctx: TurnContext) -> str:
    """Ask the owner to approve something only they can do, such as changing a skill or how a
    shared list works. The summary says what and why, in under 300 characters. Returns once
    the owner has been asked, or couldn't be; their answer comes to this chat later."""
    services, summary = ctx.services, str(args["summary"])
    if len(summary) > 300:
        raise ValueError("keep the summary under 300 characters")
    chat, owner = services.config.chats.get(ctx.chat_key), services.config.owner.name
    who, where = f"{ctx.sender.name} ({ctx.role})", (chat and chat.name) or ctx.chat_key
    until = datetime.now().timestamp() + services.channels.ask_seconds
    # Kept as a doc, so a restart can ask again (resume_requests); the summary is kept raw.
    doc = {"member_chat": ctx.chat_key, "sender_id": ctx.sender.id, "summary": summary}
    doc |= {"sender_name": ctx.sender.name, "who": who, "where": where, "until": until}
    doc = services.store.doc_put("_requests", None, doc)
    answer = await _ask_about(services, doc)
    if answer is None:  # no surface showed the question, so it was not kept either
        services.store.doc_delete("_requests", doc["id"])
        return f"Couldn't reach {owner} just now; try again later."
    services.channels.spawn(_follow_up(services, doc, answer))  # the chat need not wait
    return f"Asked {owner}; the answer will come to this chat."


async def _ask_about(services: Any, doc: dict[str, Any]) -> Any:
    """Ask the owner, in their approvals chat, about a stored request: as _ask. The summary is
    quoted as one JSON string, the way the router fences group messages, so the owner approves
    exactly the text the owner turn will get, as data."""
    question = f"{doc['sender_name']} in {doc['where']} asks: {quote(doc['summary'])}"
    return await _ask(services, services.config.approvals, question, ["Approve", "Decline"])


async def _follow_up(services: Any, doc: dict[str, Any], answer: Any) -> None:
    """Wait for the owner's choice on a stored request (answer None: no answer), act on it,
    tell the member's chat, and forget the request. An approval is checked against the config
    as it is now: in a group, the requester must still be one of its members."""
    choice = await answer if answer else None
    who, where = doc["who"], doc["where"]
    verb = {"Approve": "approved", "Decline": "declined"}.get(choice, "did not answer")
    notice = f"{services.config.owner.name} {verb} the request from {doc['sender_name']}."
    chat = services.config.chats.get(doc["member_chat"])
    if choice == "Approve" and chat and doc["sender_id"] not in [*chat.members, "owner"]:
        notice = "declined: no longer a member here"
    elif choice == "Approve":  # the turn gets the summary quoted afresh, fenced as data
        text = f"You approved this request from {who} in {where}. Do it, within your rules:"
        text += f"\n<<<\n[{who}]: {quote(doc['summary'])}\n>>>"
        now = datetime.now().timestamp()
        services.store.task_add(services.config.approvals, text, now, kind="turn", by="owner")
        services.scheduler.wake()
    services.store.add_message(doc["member_chat"], "assistant", notice)
    await services.channels.send(Outbound(doc["member_chat"], notice))
    services.store.doc_delete("_requests", doc["id"])


async def resume_requests(services: Any, retry: float = 30.0) -> None:
    """Called once at start, in a spawned task (so stopping cancels it): ask the owner again
    about each request still open. Right after a start no sidecar may be up yet and no page
    open, so while nothing can show the question it tries again every `retry` seconds; only
    once its time is up does the member get "did not answer"."""
    for doc in services.store.doc_list("_requests"):
        answer = None
        while answer is None and doc["until"] > datetime.now().timestamp():
            if (answer := await _ask_about(services, doc)) is None:
                await asyncio.sleep(retry)
        services.channels.spawn(_follow_up(services, doc, answer))


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
        saved.task_end(int(args["id"]), "cancelled")
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
    if (at is None) == (cron is None):
        raise ValueError("give exactly one of `at`, `in_minutes` or `cron`")
    due = scheduler.next_cron(cron, datetime.now().timestamp()) if cron else at
    task = saved.task_add(ctx.chat_key, args["text"], due, kind=kind, cron=cron, by=ctx.sender.id)
    ctx.services.scheduler.wake()  # the loop may be asleep until a later task
    return json.dumps(task)


def build(ctx: TurnContext) -> list[Tool]:
    """Every tool the turn's rights allow (access()); tools read ctx at call time."""
    return [t for t in TOOLS if access(ctx.role, ctx.is_group) in t.roles]
