"""The tools a turn may use, filtered by the sender's role. Owner: runtime. Budget ~180 lines.

Every tool returns a string (JSON text for structured results). Errors are returned as
"error: ..." strings by agent.run_tool, so tools may simply raise.

  read_file  {path}                         owner   path inside <home>
  write_file {path, content}                owner   creates parent folders
  edit_file  {path, old, new}               owner   `old` must occur exactly once
  run        {command, timeout?=60}         owner   shell, cwd <home>, output capped
  script     {skill, script, args?=[]}      owner,  `uv run <home>/skills/<skill>/scripts/<script>`
                                            member  with cwd = this chat's files folder. Members
                                                    only if the skill's metadata["attache-roles"]
                                                    lists "member"
  send       {text, files?=[]}              owner,  to this chat only; members' files must be
                                            member  inside this chat's files folder
  db         {op, collection, id?, data?}   owner,  op: list | get | put | delete
                                            member
  artifact   {name, html, collections?=[]}  owner,  writes <home>/artifacts/<slug>.html
                                            member
  schedule   {op, text?, at?, in_minutes?,  owner,  op: add | list | cancel; `at` is an ISO local
              cron?, kind?, id?}            member  datetime. Members: kind "message" only, in
                                                    this chat, and cancel only their own tasks

db delegates to store.db_access (chat=None for the owner, the chat key for members), which
holds the rules shared with the web bridge.
artifact registers {"name", "collections", "chat_key", "by"} in collection "_artifacts" (id =
slug) and returns its URL, f"{services.web_url}/a/{slug}". Artifacts made in a group chat are
scoped to that chat's shared docs.
"""

from __future__ import annotations

from pathlib import Path

from attache.types import Tool, TurnContext


def slug(text: str) -> str:
    """Filesystem-safe name: lowercase letters, digits and hyphens, at most 60 characters."""
    raise NotImplementedError


def chat_files(ctx: TurnContext) -> Path:
    """<home>/files/<slug(chat_key)>/, created on first use."""
    raise NotImplementedError


def resolve(ctx: TurnContext, path: str) -> Path:
    """Absolute path for `path` (relative to <home>), refusing anything outside <home>."""
    raise NotImplementedError


def build(ctx: TurnContext) -> list[Tool]:
    """Every tool whose roles include ctx.role, bound to nothing: tools read ctx at call time."""
    raise NotImplementedError
