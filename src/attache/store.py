"""One SQLite file for messages, documents, scheduled tasks and backend sessions.

Owner: foundation. Budget ~150 lines. Thread-safe enough for one process: one connection,
check_same_thread=False, a lock around writes, WAL mode.

Documents are JSON objects in named collections. Reserved keys the store maintains:
"id", "_updated" (epoch seconds), "_by" (who wrote it). "_shared" (a list of chat keys) is
written by callers and read by `doc_list(shared_with=...)`. Collections starting with "_"
are internal (e.g. "_artifacts").

Task dicts: {"id", "chat_key", "text", "kind" ("message" | "turn"), "due" (epoch seconds),
"cron" (str | None), "status" ("pending" | "done" | "cancelled"), "by", "created"}.
"""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

DocListener = Callable[[str, str, "dict[str, Any] | None"], None]  # (collection, id, doc|None)
logger = logging.getLogger(__name__)
SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, chat_key, role, text, sender_id,
  sender_name, ts);
CREATE INDEX IF NOT EXISTS messages_chat ON messages (chat_key, id);
CREATE TABLE IF NOT EXISTS docs (collection, id, json, created, updated,
  PRIMARY KEY (collection, id));
CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, chat_key, text, kind, due, cron, status,
  "by", created);
CREATE TABLE IF NOT EXISTS sessions (chat_key, endpoint, json, PRIMARY KEY (chat_key, endpoint));
"""
UPSERT_DOC = """INSERT INTO docs VALUES (?, ?, ?, ?, ?) ON CONFLICT (collection, id)
DO UPDATE SET json = excluded.json, updated = excluded.updated"""
PENDING = "SELECT * FROM tasks WHERE status = 'pending'"


class Store:
    def __init__(self, path: Path) -> None:
        self.on_change: list[DocListener] = []  # called after every doc_put / doc_delete
        self._lock = threading.Lock()
        # Autocommit: every write is a single statement, so no transaction is ever left open.
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)

    def _write(self, sql: str, *args: Any) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, args)

    def _rows(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return [dict(row) for row in self._db.execute(sql, args).fetchall()]

    def _value(self, sql: str, *args: Any) -> Any:
        row = self._db.execute(sql, args).fetchone()
        return row[0] if row else None

    def add_message(
        self, chat_key: str, role: str, text: str, sender_id: str = "", sender_name: str = ""
    ) -> int:
        """role is "user" or "assistant"."""
        args = (chat_key, role, text, sender_id, sender_name, time.time())
        return self._write("INSERT INTO messages VALUES (NULL, ?, ?, ?, ?, ?, ?)", *args).lastrowid

    def history(self, chat_key: str, limit: int = 30) -> list[dict[str, Any]]:
        """Last `limit` messages, oldest first:
        {"role", "text", "sender_id", "sender_name", "ts"}."""
        sql = "SELECT role, text, sender_id, sender_name, ts FROM messages WHERE chat_key = ?"
        return self._rows(sql + " ORDER BY id DESC LIMIT ?", chat_key, limit)[::-1]

    def doc_list(self, collection: str, *, shared_with: str | None = None) -> list[dict[str, Any]]:
        """All docs, oldest first. With shared_with, only docs whose "_shared" holds that key."""
        sql = "SELECT json FROM docs WHERE collection = ? ORDER BY created, rowid"
        docs = [json.loads(r["json"]) for r in self._rows(sql, collection)]
        return docs if shared_with is None else [d for d in docs if _shared(d, shared_with)]

    def doc_get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        sql = "SELECT json FROM docs WHERE collection = ? AND id = ?"
        return json.loads(self._value(sql, collection, doc_id) or "null")

    def doc_put(
        self, collection: str, doc_id: str | None, data: dict[str, Any], by: str = ""
    ) -> dict[str, Any]:
        """Replace (not merge) the doc. doc_id None makes a new short id. Returns the stored doc."""
        new = not doc_id
        while not doc_id or (new and self.doc_get(collection, doc_id)):
            doc_id = secrets.token_hex(4)
        now = time.time()
        doc = {**data, "id": doc_id, "_updated": now, "_by": by}
        self._write(UPSERT_DOC, collection, doc_id, json.dumps(doc), now, now)
        self._notify(collection, doc_id, doc)
        return doc

    def doc_delete(self, collection: str, doc_id: str) -> bool:
        cur = self._write("DELETE FROM docs WHERE collection = ? AND id = ?", collection, doc_id)
        if cur.rowcount:
            self._notify(collection, doc_id, None)
        return cur.rowcount > 0

    def _notify(self, collection: str, doc_id: str, doc: dict[str, Any] | None) -> None:
        for listener in list(self.on_change):
            try:
                listener(collection, doc_id, doc)
            except Exception:
                logger.debug("doc listener failed", exc_info=True)

    def collections(self) -> list[str]:
        sql = "SELECT DISTINCT collection FROM docs ORDER BY collection"
        return [r["collection"] for r in self._rows(sql)]

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
        args = (chat_key, text, kind, due, cron, by, time.time())
        cur = self._write("INSERT INTO tasks VALUES (NULL, ?, ?, ?, ?, ?, 'pending', ?, ?)", *args)
        return self._rows("SELECT * FROM tasks WHERE id = ?", cur.lastrowid)[0]

    def task_due(self, now: float) -> list[dict[str, Any]]:
        """Pending tasks with due <= now, earliest first."""
        return self._rows(PENDING + " AND due <= ? ORDER BY due, id", now)

    def task_next(self) -> float | None:
        """Due time of the earliest pending task."""
        return self._value("SELECT MIN(due) FROM tasks WHERE status = 'pending'")

    def task_reschedule(self, task_id: int, due: float) -> None:
        self._write("UPDATE tasks SET due = ? WHERE id = ?", due, task_id)

    def task_finish(self, task_id: int) -> None:
        self._write("UPDATE tasks SET status = 'done' WHERE id = ?", task_id)

    def task_cancel(self, task_id: int) -> bool:
        sql = "UPDATE tasks SET status = 'cancelled' WHERE id = ? AND status = 'pending'"
        return self._write(sql, task_id).rowcount > 0

    def task_list(self, chat_key: str | None = None) -> list[dict[str, Any]]:
        """Pending tasks, earliest first, optionally for one chat."""
        sql = PENDING + " AND (? IS NULL OR chat_key = ?) ORDER BY due, id"
        return self._rows(sql, chat_key, chat_key)

    # backend sessions, e.g. a Claude Code session id per chat
    def session_get(self, chat_key: str, endpoint: str) -> dict[str, Any]:
        sql = "SELECT json FROM sessions WHERE chat_key = ? AND endpoint = ?"
        return json.loads(self._value(sql, chat_key, endpoint) or "{}")

    def session_set(self, chat_key: str, endpoint: str, data: dict[str, Any]) -> None:
        sql = "INSERT OR REPLACE INTO sessions VALUES (?, ?, ?)"
        self._write(sql, chat_key, endpoint, json.dumps(data))

    def close(self) -> None:
        with self._lock:
            self._db.close()


def _shared(doc: dict[str, Any], chat: str) -> bool:
    return chat in (doc["_shared"] if isinstance(doc.get("_shared"), list) else [])


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
    if op not in ("list", "get", "put", "delete"):
        raise ValueError(f"unknown op {op!r}: use list, get, put or delete")
    if op in ("put", "delete") and collection.startswith("_"):
        raise PermissionError(f"collection {collection} is read-only")
    if op == "delete" and chat is not None:
        raise PermissionError("only the owner can delete documents")
    if op == "list" and collection == "_tasks":
        return store.task_list(chat)
    if op == "list":
        return store.doc_list(collection, shared_with=chat)
    doc = store.doc_get(collection, doc_id) if doc_id else None
    if doc is not None and chat is not None and not _shared(doc, chat):
        raise PermissionError(f"{collection}/{doc_id} is not shared with this chat")
    if doc is None and op != "put":
        raise KeyError(f"no document {collection}/{doc_id}")
    if op != "put":
        return doc if op == "get" else store.doc_delete(collection, str(doc_id))
    data = dict(data or {})
    if chat is not None:
        data["_shared"] = doc["_shared"] if doc else [chat]
    elif doc and "_shared" in doc:
        data.setdefault("_shared", doc["_shared"])  # the owner unshares with "_shared": []
    return store.doc_put(collection, doc_id, data, by=by)
