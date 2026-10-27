from __future__ import annotations

import re
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from attache.store import Store, db_access

ME, FAMILY, WORK = "local:me", "slack:C0123", "slack:C0456"


@pytest.fixture
def store(home: Path) -> Iterator[Store]:
    s = Store(home / "attache.db")
    yield s
    s.close()


def test_wal_and_reopen(home: Path) -> None:
    s = Store(home / "attache.db")
    assert s._db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    s.add_message(ME, "user", "hello")
    s.doc_put("todos", "a", {"text": "milk"})
    s.close()
    s = Store(home / "attache.db")
    assert s.history(ME)[0]["text"] == "hello"
    assert s.doc_get("todos", "a")["text"] == "milk"
    s.close()


def test_messages(store: Store) -> None:
    first = store.add_message(ME, "user", "one", "owner", "Alex")
    assert isinstance(first, int)
    store.add_message(ME, "assistant", "two")
    store.add_message("slack:C1", "user", "elsewhere")
    store.add_message(ME, "user", "three")
    history = store.history(ME)
    assert [m["text"] for m in history] == ["one", "two", "three"]
    assert set(history[0]) == {"role", "text", "sender_id", "sender_name", "ts"}
    assert (history[0]["role"], history[0]["sender_id"], history[0]["sender_name"]) == (
        "user",
        "owner",
        "Alex",
    )
    assert [m["text"] for m in store.history(ME, limit=2)] == ["two", "three"]
    assert store.history("nobody") == []


def test_history_before(store: Store) -> None:
    ids = [store.add_message(ME, "user", f"m{i}") for i in range(5)]
    assert [m["text"] for m in store.history(ME, before=ids[3])] == ["m0", "m1", "m2"]
    assert [m["text"] for m in store.history(ME, limit=2, before=ids[3])] == ["m1", "m2"]
    assert store.history(ME, before=ids[0]) == []
    assert len(store.history(ME, before=None)) == 5


def test_docs(store: Store) -> None:
    doc = store.doc_put("todos", None, {"text": "milk", "done": False}, by="owner")
    assert re.fullmatch(r"[0-9a-f]{16}", doc["id"])  # 64 random bits: no clash check needed
    assert doc["_by"] == "owner" and isinstance(doc["_updated"], float)
    assert store.doc_get("todos", doc["id"]) == doc
    store.doc_put("todos", "b", {"text": "eggs"})
    replaced = store.doc_put("todos", doc["id"], {"text": "oat milk"}, by="sam")
    assert "done" not in replaced and replaced["_by"] == "sam"  # replace, not merge
    assert [d["text"] for d in store.doc_list("todos")] == ["oat milk", "eggs"]  # oldest first
    assert store.doc_delete("todos", "b") is True
    assert store.doc_delete("todos", "b") is False
    assert store.doc_get("todos", "b") is None
    assert store.doc_list("empty") == []


def test_doc_list_shared_with(store: Store) -> None:
    store.doc_put("todos", "a", {"_shared": [FAMILY]})
    store.doc_put("todos", "b", {"_shared": [WORK, FAMILY]})
    store.doc_put("todos", "c", {})
    store.doc_put("todos", "d", {"_shared": FAMILY})  # not a list: never matches
    assert [d["id"] for d in store.doc_list("todos", shared_with=FAMILY)] == ["a", "b"]
    assert [d["id"] for d in store.doc_list("todos", shared_with=WORK)] == ["b"]


def test_listeners(store: Store) -> None:
    seen: list[tuple[str, str, Any]] = []

    def broken(*args: Any) -> None:
        raise RuntimeError("boom")

    store.on_change += [broken, lambda c, i, d: seen.append((c, i, d))]
    doc = store.doc_put("todos", "a", {"text": "milk"})
    store.doc_delete("todos", "a")
    store.doc_delete("todos", "a")  # nothing deleted: no event
    assert seen == [("todos", "a", doc), ("todos", "a", None)]


def test_concurrent_writes(store: Store) -> None:
    def work(n: int) -> None:
        for i in range(25):
            store.doc_put("load", f"{n}-{i}", {"n": n})
            store.add_message(ME, "user", f"{n}-{i}")

    threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(store.doc_list("load")) == 100
    assert len(store.history(ME, limit=1000)) == 100


def test_tasks(store: Store) -> None:
    later = store.task_add(FAMILY, "later", 200.0, by="sam")
    soon = store.task_add(WORK, "soon", 100.0, kind="turn", cron="0 7 * * *")
    assert set(soon) == {"id", "chat_key", "text", "kind", "due", "cron", "status", "by", "created"}
    assert (soon["kind"], soon["cron"], soon["status"]) == ("turn", "0 7 * * *", "pending")
    assert later["by"] == "sam" and later["cron"] is None
    assert store.task_next() == 100.0
    assert [t["text"] for t in store.task_due(150.0)] == ["soon"]
    assert [t["text"] for t in store.task_list()] == ["soon", "later"]
    assert [t["text"] for t in store.task_list(FAMILY)] == ["later"]
    store.task_reschedule(soon["id"], 300.0)
    assert store.task_next() == 200.0
    assert store.task_end(later["id"]) is True  # "done" by default
    assert [t["text"] for t in store.task_list()] == ["soon"]
    assert store.task_end(soon["id"], "cancelled") is True
    assert store.task_end(soon["id"], "cancelled") is False
    assert store.task_end(later["id"], "cancelled") is False  # already done
    assert store.task_next() is None and store.task_due(1e12) == []
    statuses = [row[0] for row in store._db.execute("SELECT status FROM tasks ORDER BY id")]
    assert statuses == ["done", "cancelled"]


def test_sessions(store: Store) -> None:
    assert store.session_get(ME, "claude") == {}
    store.session_set(ME, "claude", {"session_id": "abc"})
    store.session_set(ME, "claude-api", {"other": 1})
    assert store.session_get(ME, "claude") == {"session_id": "abc"}
    store.session_set(ME, "claude", {"session_id": "def"})
    assert store.session_get(ME, "claude") == {"session_id": "def"}


def test_new_chat_starts_history_afresh_and_drops_its_sessions(store: Store) -> None:
    store.add_message(ME, "user", "old")
    store.add_message(FAMILY, "user", "elsewhere")
    store.session_set(ME, "claude", {"session_id": "abc"})
    store.session_set(ME, "claude-api", {"other": 1})
    store.session_set(FAMILY, "claude", {"session_id": "def"})
    store.new_chat(ME)
    assert store.history(ME) == []
    assert store.session_get(ME, "claude") == {} and store.session_get(ME, "claude-api") == {}
    later = store.add_message(ME, "user", "new")
    assert [m["text"] for m in store.history(ME)] == ["new"]
    assert store.history(ME, before=later) == []
    store.new_chat(ME)
    store.add_message(ME, "assistant", "newer")
    assert [m["text"] for m in store.history(ME)] == ["newer"]  # from the latest new_chat
    assert [m["text"] for m in store.history(FAMILY)] == ["elsewhere"]  # other chats as they were
    assert store.session_get(FAMILY, "claude") == {"session_id": "def"}
    rows = store._db.execute("SELECT role, text FROM messages WHERE chat_key = ?", (ME,))
    assert [tuple(row) for row in rows] == [
        ("user", "old"),  # kept, only out of the history
        ("new", ""),
        ("user", "new"),
        ("new", ""),
        ("assistant", "newer"),
    ]


# db_access: the rules shared by the db tool and the web bridge


@pytest.fixture
def docs(store: Store) -> Store:
    store.doc_put("todos", "mine", {"text": "private"})
    store.doc_put("todos", "fam", {"text": "milk", "_shared": [FAMILY]})
    store.doc_put("todos", "work", {"text": "report", "_shared": [WORK]})
    store.doc_put("_artifacts", "board", {"name": "board"})
    return store


def test_owner_access(docs: Store) -> None:
    assert [d["id"] for d in db_access(docs, "list", "todos")] == ["mine", "fam", "work"]
    assert db_access(docs, "get", "todos", "mine")["text"] == "private"
    new = db_access(docs, "put", "todos", None, {"text": "eggs"}, by="owner")
    assert new["_by"] == "owner" and "_shared" not in new
    assert db_access(docs, "get", "todos", new["id"]) == new
    assert db_access(docs, "delete", "todos", new["id"]) is True
    with pytest.raises(KeyError):
        db_access(docs, "get", "todos", new["id"])
    with pytest.raises(KeyError):
        db_access(docs, "delete", "todos", "missing")
    with pytest.raises(ValueError):
        db_access(docs, "drop", "todos")


def test_owner_put_keeps_sharing_unless_given(docs: Store) -> None:
    assert db_access(docs, "put", "todos", "fam", {"text": "oat milk"})["_shared"] == [FAMILY]
    assert db_access(docs, "put", "todos", "fam", {"_shared": []})["_shared"] == []


def test_underscore_collections_are_read_only(docs: Store) -> None:
    assert db_access(docs, "get", "_artifacts", "board")["name"] == "board"
    assert len(db_access(docs, "list", "_artifacts")) == 1
    for chat in (None, FAMILY):
        with pytest.raises(PermissionError):
            db_access(docs, "put", "_artifacts", "board", {"name": "x"}, chat=chat)
        with pytest.raises(PermissionError):
            db_access(docs, "put", "_tasks", None, {"text": "x"}, chat=chat)
    with pytest.raises(PermissionError):
        db_access(docs, "delete", "_artifacts", "board")
    assert docs.doc_get("_artifacts", "board")["name"] == "board"


def test_member_sees_only_shared_docs(docs: Store) -> None:
    assert [d["id"] for d in db_access(docs, "list", "todos", chat=FAMILY)] == ["fam"]
    assert db_access(docs, "get", "todos", "fam", chat=FAMILY)["text"] == "milk"
    with pytest.raises(PermissionError):
        db_access(docs, "get", "todos", "mine", chat=FAMILY)
    with pytest.raises(PermissionError):
        db_access(docs, "get", "todos", "work", chat=FAMILY)
    with pytest.raises(KeyError):
        db_access(docs, "get", "todos", "missing", chat=FAMILY)
    assert db_access(docs, "list", "_artifacts", chat=FAMILY) == []


def test_member_put(docs: Store) -> None:
    with pytest.raises(PermissionError):
        db_access(docs, "put", "todos", "mine", {"text": "hijack"}, chat=FAMILY)
    assert docs.doc_get("todos", "mine")["text"] == "private"
    updated = db_access(
        docs, "put", "todos", "fam", {"text": "oat milk", "_shared": [WORK]}, by="sam", chat=FAMILY
    )
    assert updated["_shared"] == [FAMILY] and updated["_by"] == "sam"  # sharing kept
    new = db_access(docs, "put", "todos", None, {"text": "bread"}, chat=FAMILY)
    assert new["_shared"] == [FAMILY]
    named = db_access(docs, "put", "todos", "shop", {"_shared": [WORK]}, chat=FAMILY)
    assert named["_shared"] == [FAMILY]
    ids = [d["id"] for d in db_access(docs, "list", "todos", chat=FAMILY)]
    assert ids == ["fam", new["id"], "shop"]


def test_member_delete_refused(docs: Store) -> None:
    with pytest.raises(PermissionError):
        db_access(docs, "delete", "todos", "fam", chat=FAMILY)
    assert docs.doc_get("todos", "fam") is not None


def test_tasks_virtual_list(docs: Store) -> None:
    docs.task_add(FAMILY, "family reminder", 100.0)
    docs.task_add(WORK, "work reminder", 50.0)
    done = docs.task_add(FAMILY, "done", 10.0)
    docs.task_end(done["id"])
    everything = db_access(docs, "list", "_tasks")
    assert [t["text"] for t in everything] == ["work reminder", "family reminder"]
    assert [t["text"] for t in db_access(docs, "list", "_tasks", chat=FAMILY)] == [
        "family reminder"
    ]
    with pytest.raises(PermissionError):
        db_access(docs, "delete", "_tasks", str(done["id"]))
