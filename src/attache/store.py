"""One SQLite file for messages, documents, scheduled tasks and backend sessions.

Owner: foundation. Budget ~120 lines. Thread-safe enough for one process: one connection,
check_same_thread=False, a lock around writes, WAL mode.

Documents are JSON objects in named collections. Reserved keys the store maintains:
"id", "_updated" (epoch seconds), "_by" (who wrote it). "_shared" (a list of chat keys) is
written by callers and read by `doc_list(shared_with=...)`. Collections starting with "_"
are internal (e.g. "_artifacts").

Task dicts: {"id", "chat_key", "text", "kind" ("message" | "turn"), "due" (epoch seconds),
"cron" (str | None), "status" ("pending" | "done" | "cancelled"), "by", "created"}.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

DocListener = Callable[[str, str, "dict[str, Any] | None"], None]  # (collection, id, doc|None)


class Store:
    def __init__(self, path: Path) -> None:
        self.on_change: list[DocListener] = []  # called after every doc_put / doc_delete
        raise NotImplementedError

    # messages
    def add_message(
        self, chat_key: str, role: str, text: str, sender_id: str = "", sender_name: str = ""
    ) -> int:
        """role is "user" or "assistant"."""
        raise NotImplementedError

    def history(self, chat_key: str, limit: int = 30) -> list[dict[str, Any]]:
        """Last `limit` messages, oldest first:
        {"role", "text", "sender_id", "sender_name", "ts"}."""
        raise NotImplementedError

    # documents
    def doc_list(self, collection: str, *, shared_with: str | None = None) -> list[dict[str, Any]]:
        """All docs, oldest first. With shared_with, only docs whose "_shared" holds that key."""
        raise NotImplementedError

    def doc_get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def doc_put(
        self, collection: str, doc_id: str | None, data: dict[str, Any], by: str = ""
    ) -> dict[str, Any]:
        """Replace (not merge) the doc. doc_id None makes a new short id. Returns the stored doc."""
        raise NotImplementedError

    def doc_delete(self, collection: str, doc_id: str) -> bool:
        raise NotImplementedError

    def collections(self) -> list[str]:
        raise NotImplementedError

    # tasks
    def task_add(
        self,
        chat_key: str,
        text: str,
        due: float,
        *,
        kind: str = "message",
        cron: str | None = None,
        by: str = "",
    ) -> dict[str, Any]:
        raise NotImplementedError

    def task_due(self, now: float) -> list[dict[str, Any]]:
        """Pending tasks with due <= now, earliest first."""
        raise NotImplementedError

    def task_next(self) -> float | None:
        """Due time of the earliest pending task."""
        raise NotImplementedError

    def task_reschedule(self, task_id: int, due: float) -> None:
        raise NotImplementedError

    def task_finish(self, task_id: int) -> None:
        raise NotImplementedError

    def task_cancel(self, task_id: int) -> bool:
        raise NotImplementedError

    def task_list(self, chat_key: str | None = None) -> list[dict[str, Any]]:
        """Pending tasks, earliest first, optionally for one chat."""
        raise NotImplementedError

    # backend sessions, e.g. a Claude Code session id per chat
    def session_get(self, chat_key: str, endpoint: str) -> dict[str, Any]:
        raise NotImplementedError

    def session_set(self, chat_key: str, endpoint: str, data: dict[str, Any]) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


def db_access(
    store: Store,
    op: str,
    collection: str,
    doc_id: str | None = None,
    data: dict[str, Any] | None = None,
    *,
    by: str = "",
    chat: str | None = None,
) -> Any:
    """The one place the document rules live; the db tool and the web bridge both call it.

    op is list | get | put | delete. chat=None is owner access: any collection, but collections
    starting with "_" are read-only. With chat set (a member, or a group artifact): only docs
    whose "_shared" holds that chat key; put may update those (keeping "_shared") or add new
    docs (stored with "_shared": [chat]); delete is refused. The read-only virtual collection
    "_tasks" lists pending tasks (for that chat only when chat is set).
    Raises PermissionError when refused, KeyError for a missing doc.
    """
    raise NotImplementedError
