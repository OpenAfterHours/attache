"""One SQLite file for messages, documents, scheduled tasks and backend sessions.

A chat starts afresh with new_chat(): a marker row (role "new") in messages, after which
history() begins. Earlier messages stay in the file.

Documents are JSON objects in named collections. The store maintains "id", "_updated" (epoch
seconds) and "_by" (who wrote it). Callers write "_shared", a list of chat keys. Collections
starting with "_" are internal (e.g. "_artifacts").

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

# Column order matters: the INSERTs below list values in this order.
SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, chat_key TEXT, role TEXT,
    text TEXT, sender_id TEXT, sender_name TEXT, ts REAL);
CREATE INDEX IF NOT EXISTS messages_by_chat ON messages (chat_key, id);
CREATE TABLE IF NOT EXISTS docs (collection TEXT, id TEXT, json TEXT, created REAL,
    updated REAL, PRIMARY KEY (collection, id));
CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, chat_key TEXT, text TEXT, kind TEXT,
    due REAL, cron TEXT, status TEXT, "by" TEXT, created REAL);
CREATE TABLE IF NOT EXISTS sessions (chat_key TEXT, endpoint TEXT, json TEXT,
    PRIMARY KEY (chat_key, endpoint));
"""
PENDING_TASKS = "SELECT * FROM tasks WHERE status = 'pending'"
HISTORY = """SELECT role, text, sender_id, sender_name, ts FROM messages
WHERE chat_key = ? AND (? IS NULL OR id < ?) AND id > (SELECT COALESCE(MAX(id), 0)
FROM messages WHERE chat_key = ? AND role = 'new') ORDER BY id DESC LIMIT ?"""
# An update keeps `created`, so doc_list order stays oldest first.
UPSERT_DOC = """INSERT INTO docs VALUES (?, ?, ?, ?, ?) ON CONFLICT (collection, id)
DO UPDATE SET json = excluded.json, updated = excluded.updated"""


class Store:
    def __init__(self, path: Path) -> None:
        self.on_change: list[DocListener] = []  # called after every doc_put / doc_delete
        # One connection shared by all threads, a lock around writes, and autocommit: every
        # write is a single statement, so no transaction is ever left open. A short busy timeout:
        # calls block the event loop, so a locked database should fail fast and be retried.
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, timeout=1, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)

    def _write(self, sql: str, *args: Any) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, args)

    def _rows(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return [dict(row) for row in self._db.execute(sql, args).fetchall()]

    def _value(self, sql: str, *args: Any) -> Any:
        """The first column of the first row, or None."""
        row = self._db.execute(sql, args).fetchone()
        return None if row is None else row[0]

    # messages
    def add_message(
        self, chat_key: str, role: str, text: str, sender_id: str = "", sender_name: str = ""
    ) -> int:
        """role is "user" or "assistant". Returns the message id."""
        values = (chat_key, role, text, sender_id, sender_name, time.time())
        cur = self._write("INSERT INTO messages VALUES (NULL, ?, ?, ?, ?, ?, ?)", *values)
        return cur.lastrowid

    def history(
        self, chat_key: str, limit: int = 30, before: int | None = None
    ) -> list[dict[str, Any]]:
        """The last `limit` messages with id < `before` (if given) since the chat's latest
        new_chat(), oldest first: {"role", "text", "sender_id", "sender_name", "ts"}."""
        return self._rows(HISTORY, chat_key, before, before, chat_key, limit)[::-1]  # oldest first

    def new_chat(self, chat_key: str) -> None:
        """Start the chat afresh: history() leaves out what came before, and the chat's backend
        sessions go, so a resumed one cannot bring it back. Nothing else is deleted."""
        self.add_message(chat_key, "new", "")
        self._write("DELETE FROM sessions WHERE chat_key = ?", chat_key)

    # documents
    def doc_list(self, collection: str, *, shared_with: str | None = None) -> list[dict[str, Any]]:
        """All docs, oldest first. With shared_with, only docs whose "_shared" holds that key."""
        sql = "SELECT json FROM docs WHERE collection = ? ORDER BY created, rowid"
        docs = [json.loads(row["json"]) for row in self._rows(sql, collection)]
        if shared_with is None:
            return docs
        return [doc for doc in docs if _shared(doc, shared_with)]

    def doc_get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        sql = "SELECT json FROM docs WHERE collection = ? AND id = ?"
        text = self._value(sql, collection, doc_id)
        return None if text is None else json.loads(text)

    def doc_put(
        self, collection: str, doc_id: str | None, data: dict[str, Any], by: str = ""
    ) -> dict[str, Any]:
        """Replace (not merge) the doc. doc_id None makes a new short id. Returns the stored doc."""
        doc_id = doc_id or secrets.token_hex(8)  # 64 random bits: a clash needs no check
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
        values = (chat_key, text, kind, due, cron, "pending", by, time.time())
        cur = self._write("INSERT INTO tasks VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?)", *values)
        return self._rows("SELECT * FROM tasks WHERE id = ?", cur.lastrowid)[0]

    def task_due(self, now: float) -> list[dict[str, Any]]:
        """Pending tasks with due <= now, earliest first."""
        return self._rows(PENDING_TASKS + " AND due <= ? ORDER BY due, id", now)

    def task_next(self) -> float | None:
        """Due time of the earliest pending task."""
        return self._value("SELECT MIN(due) FROM tasks WHERE status = 'pending'")

    def task_reschedule(self, task_id: int, due: float) -> None:
        self._write("UPDATE tasks SET due = ? WHERE id = ?", due, task_id)

    def task_end(self, task_id: int, status: str = "done") -> bool:
        """Mark a pending task "done" or "cancelled"; False when it was not pending."""
        sql = "UPDATE tasks SET status = ? WHERE id = ? AND status = 'pending'"
        return self._write(sql, status, task_id).rowcount > 0

    def task_list(self, chat_key: str | None = None) -> list[dict[str, Any]]:
        """Pending tasks, earliest first, optionally for one chat."""
        sql = PENDING_TASKS + " AND (? IS NULL OR chat_key = ?) ORDER BY due, id"
        return self._rows(sql, chat_key, chat_key)

    # backend sessions, e.g. a Claude Code session id per chat
    def session_get(self, chat_key: str, endpoint: str) -> dict[str, Any]:
        sql = "SELECT json FROM sessions WHERE chat_key = ? AND endpoint = ?"
        text = self._value(sql, chat_key, endpoint)
        return {} if text is None else json.loads(text)

    def session_set(self, chat_key: str, endpoint: str, data: dict[str, Any]) -> None:
        self._write("REPLACE INTO sessions VALUES (?, ?, ?)", chat_key, endpoint, json.dumps(data))

    def close(self) -> None:
        with self._lock:
            self._db.close()


def _shared(doc: dict[str, Any], chat: str) -> bool:
    """Whether doc["_shared"] is a list holding `chat`."""
    shared = doc.get("_shared")
    return isinstance(shared, list) and chat in shared


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

    if op == "list":
        if collection == "_tasks":
            return store.task_list(chat)
        return store.doc_list(collection, shared_with=chat)

    doc = store.doc_get(collection, doc_id) if doc_id else None
    if doc is not None and chat is not None and not _shared(doc, chat):
        raise PermissionError(f"{collection}/{doc_id} is not shared with this chat")
    if doc is None and op != "put":
        raise KeyError(f"no document {collection}/{doc_id}")
    if op == "get":
        return doc
    if op == "delete":
        return store.doc_delete(collection, doc["id"])

    data = dict(data or {})
    if chat is not None:  # members keep the doc's sharing; their new docs stay in their chat
        data["_shared"] = doc["_shared"] if doc else [chat]
    elif doc and "_shared" in doc:  # the owner unshares with "_shared": []
        data.setdefault("_shared", doc["_shared"])
    return store.doc_put(collection, doc_id, data, by=by)
