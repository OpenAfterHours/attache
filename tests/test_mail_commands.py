"""The mail skill's shared commands (mail.py), against fake backends: the JSON shapes, ids,
previews, fingerprints and approvals, caps and the audit log. No mail server is touched."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import sys
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from attache import skills

FOLDER = skills.BUNDLED / "mail"
ACCOUNTS = """
[gmail]
backend = "imap"
kind = "gmail"
user = "you@gmail.com"
[titan]
backend = "imap"
user = "you@yourdomain.com"
[outlook]
backend = "com"
"""
SECRET_SUBJECT = "Boiler service quote"
SECRET_BODY = "Thanks Dan, Thursday at 9am works."


def load_mail() -> ModuleType:
    spec = importlib.util.spec_from_file_location("attache_mail", FOLDER / "scripts" / "mail.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def summary(native: str, received: str, **fields: Any) -> dict[str, Any]:
    base = {"id": native, "folder": "INBOX", "subject": SECRET_SUBJECT, "received_utc": received}
    base |= {"from": {"name": "Dan", "smtp": "dan@example.com"}, "preview": "Hi", "unread": True}
    return base | {"flagged": False} | fields


class FakeBackend:
    """Backend stand-in that records what mail.py asks of it. An id "gone" isn't there."""

    def __init__(self, account: str, settings: dict[str, Any], home: Path) -> None:
        self.account, self.settings, self.calls = account, settings, []
        self.items: list[dict[str, Any]] = []
        self.drafts: dict[str, dict[str, Any]] = {}
        self.sent: list[str] = []
        self.fails: Exception | None = None  # for list
        self.send_fails: Exception | None = None  # for send_draft, respond and create_event
        self.during_send: Any = None  # called while sending, to look at the approval files

    def doctor(self) -> tuple[bool, str]:
        return True, f"{self.account} is fine"

    def login(self) -> None:
        self.calls.append(("login",))

    def folders(self) -> list[dict[str, Any]]:
        return [{"folder": "Receipts", "path": "INBOX/Receipts", "unread": 1, "total": 4}]

    def list(self, folder: Any, unread: bool, limit: int, since: Any, query: Any) -> list:
        self.calls.append(("list", folder, unread, limit, since, query))
        if self.fails:
            raise self.fails
        return self.items

    def read(self, native: str, max_chars: int) -> dict[str, Any]:
        self.calls.append(("read", native, max_chars))
        return summary(native, "2026-10-18T16:42:00Z") | {"to": [], "cc": [], "body": "x" * 9000}

    def _there(self, native: str) -> None:
        if native == "gone":
            raise LookupError("no such message (it may have moved)")

    def move(self, natives: list[str], folder: str) -> list[str]:
        self.calls.append(("move", natives, folder))
        for native in natives:
            self._there(native)
        return [f"moved-{native}" for native in natives]

    def flag(self, natives: list[str], on: bool) -> list[str]:
        self.calls.append(("flag", natives, on))
        for native in natives:
            self._there(native)
        return natives

    def mark_read(self, natives: list[str], on: bool) -> list[str]:
        self.calls.append(("mark_read", natives, on))
        return natives

    def draft(self, to: Any, cc: Any, subject: Any, body: str, reply_to: Any, all_: bool) -> dict:
        self.calls.append(("draft", to, cc, subject, reply_to, all_))
        native = f"d{len(self.drafts) + 1}"
        self.drafts[native] = {
            "id": native,
            "to": to or ["dan@example.com"],
            "cc": cc or [],
            "bcc": [],
            "subject": subject or f"Re: {SECRET_SUBJECT}",
            "body": body,
            "attachments": [],
        }
        return json.loads(json.dumps(self.drafts[native]))

    def get_draft(self, native: str) -> dict[str, Any]:
        return json.loads(json.dumps(self.drafts[native]))

    def _send(self, what: str) -> None:
        if self.during_send:
            self.during_send()
        if self.send_fails:
            raise self.send_fails
        self.sent.append(what)

    def send_draft(self, native: str) -> str:
        self._send(native)
        return f"sent-{native}"

    def agenda(self, days: int) -> list[dict[str, Any]]:
        if self.settings["backend"] == "imap":
            raise NotImplementedError("not in v1 for IMAP accounts")
        return [{"id": "e2", "start": "2026-10-22T09:00:00Z"}, {"id": "e1", "start": "2026-10-21"}]

    def tasks(self) -> list[dict[str, Any]]:
        return [{"id": "t1", "subject": "Renew passport", "due": None, "done": False}]

    def respond(self, native: str, response: str, body: Any) -> None:
        self.calls.append(("respond", native, response, body))
        self._send(native)

    def create_event(self, subject: str, start: Any, end: Any, attendees: list, body: Any) -> str:
        self.calls.append(("create_event", subject, start, end, attendees, body))
        self._send(subject)
        return "e9"


@pytest.fixture
def mail(home: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """mail.py with accounts.toml in the home and fake backends, kept across calls."""
    (home / "skills" / "mail").mkdir(parents=True)
    (home / "skills" / "mail" / "accounts.toml").write_text(ACCOUNTS, encoding="utf-8")
    module = load_mail()
    accounts = tomllib.loads(ACCOUNTS).items()
    module.backends = {name: FakeBackend(name, settings, home) for name, settings in accounts}
    monkeypatch.setattr(module, "open_backend", lambda name, *_: module.backends[name])
    return module


def run(module: ModuleType, capsys: Any, *argv: str) -> dict[str, Any]:
    code = module.main(list(argv))
    out = capsys.readouterr().out
    assert out.count("\n") == 1, out  # exactly one JSON object
    result = json.loads(out)
    assert code == (0 if result["ok"] else 1)
    return result


def backend(module: ModuleType, name: str) -> FakeBackend:
    return module.backends[name]


def log_lines(home: Path) -> list[dict[str, Any]]:
    text = (home / "logs" / "mail.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


def approve(home: Path, fingerprint: str) -> None:
    """What the core's ask tool does when the owner picks Send: it reads the preview (refusing
    without one), shows it, and writes the sha256 of the text it showed."""
    shown = (home / ".approvals" / f"{fingerprint}.preview").read_text(encoding="utf-8")
    seen = hashlib.sha256(shown.encode("utf-8")).hexdigest()
    (home / ".approvals" / fingerprint).write_text(seen, encoding="utf-8")


def approvals(home: Path) -> list[str]:
    return sorted(path.name for path in (home / ".approvals").iterdir())


def draft_new(module: ModuleType, capsys: Any, account: str = "titan") -> dict[str, Any]:
    args = ["--to", "dan@example.com", "--subject", SECRET_SUBJECT, "--body", SECRET_BODY]
    return run(module, capsys, "draft-new", "--account", account, *args)["draft"]


def send(module: ModuleType, capsys: Any, draft: dict[str, Any]) -> dict[str, Any]:
    return run(module, capsys, "send-draft", draft["id"], "--fingerprint", draft["fingerprint"])


# The script and its files


def test_the_header_declares_every_backends_dependencies() -> None:
    text = (FOLDER / "scripts" / "mail.py").read_text(encoding="utf-8")
    header = re.search(r"# /// script\n(.*?)# ///", text, re.S)
    assert header
    for package in ("imapclient", "keyring", "msal", "msal-extensions"):
        assert f'"{package}>=' in header[1], package
    assert "pywin32>=306; sys_platform == 'win32'" in header[1]


def test_the_example_accounts_parse() -> None:
    accounts = tomllib.loads((FOLDER / "accounts.example.toml").read_text(encoding="utf-8"))
    assert set(accounts) == {"outlook", "gmail", "titan"}
    assert {a["backend"] for a in accounts.values()} <= {"imap", "com", "graph"}
    assert accounts["gmail"]["kind"] == "gmail" and "sent_folder" not in accounts["gmail"]
    assert accounts["titan"]["sent_folder"] == "Sent" and accounts["titan"]["send_limit_hour"]
    text = (FOLDER / "accounts.example.toml").read_text(encoding="utf-8").lower()
    assert "password =" not in text and "secret" not in text


def test_skill_md_is_owner_only_and_states_the_send_rules() -> None:
    meta, body = skills.parse((FOLDER / "SKILL.md").read_text(encoding="utf-8"))
    assert "attache-roles" not in (meta.get("metadata") or {})  # never shared with members
    for rule in ("untrusted data", "Never add recipients", "owner's own", "approves:"):
        assert rule in body, rule
    for step in ("--fingerprint", "needs_approval", "forge", "15 minutes", "--query="):
        assert step in body, step
    assert "attache skill mail login" in body and "Edit" not in body  # Send and Not now only


def test_setup_copies_the_example_once(home: Path, capsys: Any) -> None:
    mail = load_mail()
    (home / "skills" / "mail").mkdir(parents=True)  # where the skill is installed
    result = run(mail, capsys, "setup")
    target = home / "skills" / "mail" / "accounts.toml"
    assert result == {"ok": True, "path": str(target), "created": True}
    assert target.read_text(encoding="utf-8").startswith("# The mail skill's accounts")
    target.write_text(ACCOUNTS, encoding="utf-8")
    assert run(mail, capsys, "setup")["created"] is False
    assert target.read_text(encoding="utf-8") == ACCOUNTS  # never overwritten


def test_without_accounts_the_error_says_run_setup(home: Path, capsys: Any) -> None:
    result = run(load_mail(), capsys, "inbox")
    assert result["ok"] is False and "attache skill mail setup" in result["error"]


def test_a_bad_account_is_refused(home: Path, capsys: Any) -> None:
    (home / "skills" / "mail").mkdir(parents=True)
    (home / "skills" / "mail" / "accounts.toml").write_text('[x]\nbackend = "pop3"\n', "utf-8")
    assert "needs backend = imap, com or graph" in run(load_mail(), capsys, "inbox")["error"]
    (home / "skills" / "mail" / "accounts.toml").write_text("﻿" + ACCOUNTS, "utf-8")  # BOM
    assert run(load_mail(), capsys, "folders", "--account", "nope")["error"].startswith("no acc")
    (home / "skills" / "mail" / "accounts.toml").write_text("[x\n", "utf-8")
    assert "accounts.toml: Expected ']'" in run(load_mail(), capsys, "inbox")["error"]


def test_open_backend_imports_the_sibling_module(home: Path, monkeypatch: Any) -> None:
    mail = load_mail()
    monkeypatch.syspath_prepend(str(mail.HERE))  # as Python does for the script it runs
    imap = mail.open_backend("titan", {"backend": "imap", "user": "you@x.com"}, home)
    assert type(imap).__module__ == "imap_backend" and imap.account == "titan"


def test_help_and_values_that_start_with_a_dash(mail: ModuleType, capsys: Any) -> None:
    usage = run(mail, capsys, "send-draft", "-h")  # inside the one JSON object
    assert usage["ok"] is False and "usage: mail.py send-draft" in usage["error"]
    assert "[--fingerprint FINGERPRINT] id" in usage["error"]
    assert "--fingerprint" in usage["error"]
    run(mail, capsys, "search", "--account", "gmail", "--query=-in:chats from:dan")
    assert backend(mail, "gmail").calls[-1][-1] == "-in:chats from:dan"
    args = ["--account", "titan", "--to", "a@x.com", "--subject", "S", "--body=- Thu\n- Fri"]
    assert run(mail, capsys, "draft-new", *args)["draft"]["body"] == "- Thu\n- Fri"


# Reading


def test_inbox_merges_the_accounts_newest_first(mail: ModuleType, capsys: Any) -> None:
    gmail, titan = backend(mail, "gmail"), backend(mail, "titan")
    gmail.items = [summary("101", "2026-10-18T16:42:00Z", preview="  Hi,\n\n  quote " + "x" * 300)]
    titan.items = [summary("t0", "2026-10-17T08:00:00Z"), summary("t1", "2026-10-19T08:00:00Z")]
    result = run(mail, capsys, "inbox", "--limit", "2")
    assert [item["id"] for item in result["items"]] == ["titan:t1", "gmail:101"]
    first, second = result["items"]
    assert first["account"] == "titan" and first["untrusted"] is True
    assert second["preview"].startswith("Hi, quote x") and len(second["preview"]) == 200
    assert backend(mail, "outlook").calls == [("list", None, False, 2, None, None)]


def test_inbox_and_search_pass_their_filters(mail: ModuleType, capsys: Any) -> None:
    since = ["--since", "2026-10-20T07:00+01:00"]
    run(mail, capsys, "inbox", "--account", "gmail", "--unread", *since)
    when = datetime(2026, 10, 20, 6, 0, tzinfo=UTC)
    assert backend(mail, "gmail").calls == [("list", None, True, 25, when, None)]
    run(mail, capsys, "search", "--query", "from:dan", "--folder", "Work", "--limit", "500")
    assert backend(mail, "titan").calls[-1] == ("list", "Work", False, 100, None, "from:dan")


def test_one_failing_account_doesnt_stop_the_others(mail: ModuleType, capsys: Any) -> None:
    backend(mail, "gmail").fails = PermissionError("Gmail refused the password")
    backend(mail, "titan").items = [summary("t1", "2026-10-19T08:00:00Z")]
    result = run(mail, capsys, "inbox")
    assert result["ok"] and [item["id"] for item in result["items"]] == ["titan:t1"]
    assert result["errors"] == [{"account": "gmail", "error": "Gmail refused the password"}]
    alone = run(mail, capsys, "inbox", "--account", "gmail")
    assert alone == {"ok": False, "error": "Gmail refused the password"}


def test_agenda_is_outlook_only(mail: ModuleType, capsys: Any) -> None:
    result = run(mail, capsys, "agenda")
    assert [event["id"] for event in result["items"]] == ["outlook:e1", "outlook:e2"]
    assert "errors" not in result  # the IMAP accounts are left out, not failed
    imap = run(mail, capsys, "agenda", "--account", "titan")
    assert imap["ok"] is False and "not in v1 for IMAP accounts" in imap["error"]
    tasks = run(mail, capsys, "tasks", "--account", "outlook")
    assert tasks["items"][0]["id"] == "outlook:t1" and tasks["items"][0]["untrusted"]


def test_read_caps_the_body(mail: ModuleType, capsys: Any) -> None:
    item = run(mail, capsys, "read", "gmail:101", "--max-chars", "1000")["item"]
    assert item["id"] == "gmail:101" and item["untrusted"] is True
    assert len(item["body"]) == 1000 and item["truncated"] is True
    assert backend(mail, "gmail").calls == [("read", "101", 1000)]
    run(mail, capsys, "read", "gmail:101", "--max-chars", "999999")
    assert backend(mail, "gmail").calls[-1] == ("read", "101", 50000)
    run(mail, capsys, "read", "gmail:101", "--max-chars", "-5")
    assert backend(mail, "gmail").calls[-1] == ("read", "101", 1)


@pytest.mark.parametrize("item_id", ["101", "nobody:101", "gmail:", ":101"])
def test_ids_must_name_an_account(mail: ModuleType, capsys: Any, item_id: str) -> None:
    result = run(mail, capsys, "read", item_id)
    assert result["ok"] is False and "is not a mail id" in result["error"]


def test_folders_and_doctor_cover_each_account(mail: ModuleType, capsys: Any, monkeypatch: Any):
    folders = run(mail, capsys, "folders", "--account", "titan")["items"]
    assert folders == [
        {"folder": "Receipts", "path": "INBOX/Receipts", "unread": 1, "total": 4}
        | {"account": "titan", "untrusted": True}
    ]
    opened = mail.open_backend

    def no_pywin32(name: str, settings: dict[str, Any], home: Path) -> Any:
        if settings["backend"] == "com":
            raise ModuleNotFoundError("No module named 'win32com'")
        return opened(name, settings, home)

    monkeypatch.setattr(mail, "open_backend", no_pywin32)
    accounts = run(mail, capsys, "doctor")["accounts"]
    assert accounts[0] == {
        "account": "gmail",
        "backend": "imap",
        "ok": True,
        "detail": "gmail is fine",
    }
    assert accounts[2]["ok"] is False and "win32com" in accounts[2]["detail"]
    assert run(mail, capsys, "doctor", "--account", "nope")["error"].startswith("no account 'nope'")


# Filing


def test_moving_more_than_20_needs_many(mail: ModuleType, capsys: Any) -> None:
    ids = [f"gmail:{n}" for n in range(21)]
    refused = run(mail, capsys, "move", *ids, "--to", "Receipts")
    assert refused["ok"] is False and "--many" in refused["error"]
    assert backend(mail, "gmail").calls == []
    moved = run(mail, capsys, "move", *ids, "titan:t1", "--to", "Receipts", "--many")["moved"]
    assert moved[0] == "gmail:moved-0" and moved[-1] == "titan:moved-t1" and len(moved) == 22


def test_one_missing_message_doesnt_hide_the_rest(mail: ModuleType, capsys: Any) -> None:
    result = run(mail, capsys, "move", "gmail:1", "gmail:gone", "nobody:2", "--to", "Receipts")
    assert result["ok"] and result["moved"] == ["gmail:moved-1"]  # the new ids
    assert result["errors"] == [
        {"id": "gmail:gone", "error": "no such message (it may have moved)"},
        {
            "id": "nobody:2",
            "error": "'nobody:2' is not a mail id: use the ids inbox and search give",
        },
    ]
    flagged = run(mail, capsys, "flag", "titan:gone", "titan:t1")
    assert flagged["changed"] == ["titan:t1"] and flagged["errors"][0]["id"] == "titan:gone"
    alone = run(mail, capsys, "flag", "titan:gone")
    assert alone == {"ok": False, "error": "titan:gone: no such message (it may have moved)"}


def test_flag_and_mark_read(mail: ModuleType, capsys: Any) -> None:
    assert run(mail, capsys, "flag", "gmail:1", "titan:t1")["changed"] == ["gmail:1", "titan:t1"]
    run(mail, capsys, "mark-read", "gmail:1", "--off")
    assert backend(mail, "gmail").calls == [("flag", ["1"], True), ("mark_read", ["1"], False)]


# Drafts, previews, fingerprints and approvals


def fingerprint(account: str, native: str, preview: str) -> str:
    """The fingerprint as the contract defines it: what the owner sees, for this item."""
    fields = {"account": account, "id": native, "preview": preview}
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()[:16]


def test_a_draft_comes_with_the_preview_the_owner_approves(
    mail: ModuleType, capsys: Any, home: Path
) -> None:
    args = ["--to", "b@x.com", "a@x.com", "--cc", "c@x.com", "--subject", "Hi", "--body", "Yo"]
    draft = run(mail, capsys, "draft-new", "--account", "titan", *args)["draft"]
    preview = "\n".join(
        [
            "Send from titan (imap)",
            "To: b@x.com, a@x.com",
            "Cc: c@x.com",
            "Bcc: none",
            "Subject: Hi",
            "Attachments: none",
            "",
            "Yo",
        ]
    )
    assert draft == {
        "id": "titan:d1",
        "account": "titan",
        "to": ["b@x.com", "a@x.com"],
        "cc": ["c@x.com"],
        "bcc": [],
        "subject": "Hi",
        "body": "Yo",
        "attachments": [],
        "fingerprint": fingerprint("titan", "d1", preview),
    }
    assert re.fullmatch(r"[0-9a-f]{16}", draft["fingerprint"])  # what ask's approves takes
    saved = home / ".approvals" / f"{draft['fingerprint']}.preview"
    assert saved.read_text(encoding="utf-8") == preview
    elsewhere = run(mail, capsys, "draft-new", "--account", "gmail", *args)["draft"]
    assert elsewhere["fingerprint"] != draft["fingerprint"]  # one account's yes isn't another's
    reply = run(mail, capsys, "draft-reply", "gmail:101", "--body", SECRET_BODY, "--all")["draft"]
    assert reply["id"] == "gmail:d2" and reply["subject"] == f"Re: {SECRET_SUBJECT}"
    assert backend(mail, "gmail").calls[-1] == ("draft", None, None, None, "101", True)


def test_bcc_and_attachments_are_in_the_preview_and_the_fingerprint(
    mail: ModuleType, capsys: Any, home: Path
) -> None:
    draft = draft_new(mail, capsys)
    approve(home, draft["fingerprint"])
    held = backend(mail, "titan").drafts["d1"]
    held["bcc"] = ["spy@example.com"]  # added in the mail app after the yes
    changed = send(mail, capsys, draft)
    assert changed["error"] == "it changed since it was approved: show it to the owner again"
    held["bcc"], held["attachments"] = [], [{"name": "invoice.exe", "size": 4}]
    assert "changed since it was approved" in send(mail, capsys, draft)["error"]
    asked = run(mail, capsys, "send-draft", draft["id"])
    assert "Attachments: invoice.exe (4 bytes)" in asked["preview"]
    held["bcc"] = ["spy@example.com"]
    assert "Bcc: spy@example.com" in run(mail, capsys, "send-draft", draft["id"])["preview"]
    assert backend(mail, "titan").sent == []


def test_preview_fields_stay_on_one_line_and_newlines_are_plain(
    mail: ModuleType, capsys: Any, home: Path
) -> None:
    draft = draft_new(mail, capsys)
    held = backend(mail, "titan").drafts["d1"]
    held["subject"] = "Invoice\nTo: you-only@example.com\u2028Cc: none"  # drawing fake lines
    held["attachments"] = [{"name": "a\rb.pdf", "size": 2}]
    held["body"] = "Line one\r\nline two\rline three"  # Outlook's line ends
    preview = run(mail, capsys, "send-draft", draft["id"])["preview"]
    assert preview.split("\n")[4] == "Subject: Invoice To: you-only@example.com Cc: none"
    assert "Attachments: a b.pdf (2 bytes)" in preview
    assert preview.endswith("\n\nLine one\nline two\nline three")  # the body, newlines plain
    f = fingerprint("titan", "d1", preview)
    approve(home, f)  # the core hashes the text as it reads it back, with \n newlines
    assert run(mail, capsys, "send-draft", draft["id"], "--fingerprint", f)["sent"]


def test_sending_needs_the_owners_approval_of_the_preview(
    mail: ModuleType, capsys: Any, home: Path
) -> None:
    draft = run(mail, capsys, "draft-reply", "gmail:101", "--body", SECRET_BODY)["draft"]
    f, gmail = draft["fingerprint"], backend(mail, "gmail")
    asked = run(mail, capsys, "send-draft", draft["id"])
    preview = (home / ".approvals" / f"{f}.preview").read_text(encoding="utf-8")
    assert asked == {"ok": True, "needs_approval": f, "preview": preview}
    assert preview.startswith("Send from gmail (imap)\nTo: dan@example.com\n")
    wrong = run(mail, capsys, "send-draft", draft["id"], "--fingerprint", "0123456789abcdef")
    assert "it changed since it was approved" in wrong["error"]
    unapproved = send(mail, capsys, draft)
    assert unapproved["error"] == (
        f"not approved: show it to the owner with the ask tool (approves: {f}) first"
    )
    approve(home, f)

    def claimed() -> None:  # while it sends, the approval is taken, so a second call has none
        assert approvals(home) == [f"{f}.preview", f"{f}.sending"]

    gmail.during_send = claimed
    assert send(mail, capsys, draft) == {"ok": True, "sent": True, "id": "gmail:sent-d1"}
    assert gmail.sent == ["d1"] and approvals(home) == []  # used once, and tidied
    again = send(mail, capsys, draft)
    assert again["error"].startswith("not approved") and gmail.sent == ["d1"]
    assert log_lines(home)[-2]["approved"] is True and "approved" not in log_lines(home)[-1]


def test_a_rewritten_preview_is_no_use(mail: ModuleType, capsys: Any, home: Path) -> None:
    draft = draft_new(mail, capsys)
    f = draft["fingerprint"]
    saved = home / ".approvals" / f"{f}.preview"
    saved.write_text(saved.read_text(encoding="utf-8").replace("dan@", "nobody@"), "utf-8")
    approve(home, f)  # the owner approved the rewritten text, not the real draft
    result = send(mail, capsys, draft)
    expected = f"the preview changed since it was approved: ask again (approves: {f})"
    assert result["error"] == expected
    assert backend(mail, "titan").sent == [] and f not in approvals(home)


def test_an_approval_lasts_15_minutes(mail: ModuleType, capsys: Any, home: Path) -> None:
    draft = draft_new(mail, capsys)
    approve(home, draft["fingerprint"])
    old = time.time() - 16 * 60
    os.utime(home / ".approvals" / draft["fingerprint"], (old, old))
    result = send(mail, capsys, draft)
    assert result["error"].startswith("the approval expired: ask again")
    left = approvals(home)
    assert backend(mail, "titan").sent == [] and left == [f"{draft['fingerprint']}.preview"]


def test_previews_of_drafts_never_sent_go_after_a_day(
    mail: ModuleType, capsys: Any, home: Path
) -> None:
    first = draft_new(mail, capsys)
    old = time.time() - 25 * 60 * 60
    os.utime(home / ".approvals" / f"{first['fingerprint']}.preview", (old, old))
    second = run(mail, capsys, "draft-reply", "gmail:101", "--body", "Another")["draft"]
    assert approvals(home) == [f"{second['fingerprint']}.preview"]


def test_a_send_that_certainly_failed_keeps_the_owners_yes(
    mail: ModuleType, capsys: Any, home: Path
) -> None:
    draft, titan = draft_new(mail, capsys), backend(mail, "titan")
    approve(home, draft["fingerprint"])
    titan.send_fails = ValueError("smtp.titan.email refused the password")
    assert send(mail, capsys, draft)["error"] == "smtp.titan.email refused the password"
    assert draft["fingerprint"] in approvals(home)  # put back: nothing left the machine
    titan.send_fails = None
    assert send(mail, capsys, draft)["sent"] and titan.sent == ["d1"]


def test_a_send_that_may_have_gone_is_never_retried(
    mail: ModuleType, capsys: Any, home: Path
) -> None:
    draft, outlook = draft_new(mail, capsys, "outlook"), backend(mail, "outlook")
    approve(home, draft["fingerprint"])
    outlook.send_fails = OSError("connection reset by peer")
    failed = send(mail, capsys, draft)
    assert failed["error"] == "connection reset by peer; it may have been sent: check Sent first"
    assert f"{draft['fingerprint']}.sending" in approvals(home)
    outlook.send_fails = None
    approve(home, draft["fingerprint"])  # even with a fresh yes
    retry = send(mail, capsys, draft)
    assert retry["error"] == "this may already have been sent; check Sent, then edit it to resend"
    assert outlook.sent == []


def test_a_backend_that_guards_resends_decides_itself(
    mail: ModuleType, capsys: Any, home: Path
) -> None:
    draft, titan = draft_new(mail, capsys), backend(mail, "titan")
    titan.guards_resends = True  # as the IMAP backend, with its record of what went
    approve(home, draft["fingerprint"])
    titan.send_fails = OSError("killed")  # a try that stopped: .sending is left behind
    send(mail, capsys, draft)
    titan.send_fails = None
    approve(home, draft["fingerprint"])
    assert send(mail, capsys, draft)["sent"] and titan.sent == ["d1"]
    assert approvals(home) == []


def test_respond_and_create_event_need_approval(mail: ModuleType, capsys: Any, home: Path) -> None:
    asked = run(mail, capsys, "respond", "outlook:m1", "tentative", "--body", "Running late")
    preview = "\n".join(
        [
            "Reply 'tentative' to an invitation in outlook",
            f"Invitation: {SECRET_SUBJECT} from dan@example.com",
            "",
            "Running late",
        ]
    )
    assert asked == {"ok": True, "needs_approval": asked["needs_approval"], "preview": preview}
    f = asked["needs_approval"]
    assert f == fingerprint("outlook", "m1", preview)
    approve(home, f)
    other = run(mail, capsys, "respond", "outlook:m1", "decline", "--fingerprint", f)
    assert "it changed since it was approved" in other["error"]
    args = ["respond", "outlook:m1", "tentative", "--body", "Running late", "--fingerprint", f]
    assert run(mail, capsys, *args) == {"ok": True, "sent": True, "id": "outlook:m1"}
    assert backend(mail, "outlook").calls[-1] == ("respond", "m1", "tentative", "Running late")

    event = ["create-event", "--account", "outlook", "--subject", "Boiler service"]
    event += ["--start", "2026-10-23T09:00", "--end", "2026-10-23T10:00"]
    event += ["--attendees", "dan@x.com", "ann@x.com"]
    asked = run(mail, capsys, *event)
    assert asked["preview"] == "\n".join(
        [
            "Create an event in outlook",
            "Subject: Boiler service",
            "Start: 2026-10-23 09:00",
            "End: 2026-10-23 10:00",
            "Invite: ann@x.com, dan@x.com",
            "",
            "",
        ]
    )
    approve(home, asked["needs_approval"])
    created = run(mail, capsys, *event, "--fingerprint", asked["needs_approval"])
    assert created == {"ok": True, "created": True, "id": "outlook:e9"}
    _, _, start, end, attendees, body = backend(mail, "outlook").calls[-1]
    assert (start, end) == (datetime(2026, 10, 23, 9), datetime(2026, 10, 23, 10))
    assert attendees == ["ann@x.com", "dan@x.com"] and body is None and approvals(home) == []


def test_imap_accounts_are_refused_meetings_before_the_owner_is_asked(
    mail: ModuleType, capsys: Any, home: Path
) -> None:
    event = ["--subject", "S", "--start", "2026-10-23T09:00", "--end", "2026-10-23T10:00"]
    for argv in (
        ["respond", "titan:m1", "accept"],
        ["create-event", "--account", "gmail", *event],
    ):
        assert run(mail, capsys, *argv)["error"] == "not in v1 for IMAP accounts"
    assert not (home / ".approvals").exists()  # no preview, so nothing for ask to show
    assert backend(mail, "titan").calls == []


def test_login_is_only_for_the_owners_terminal(
    mail: ModuleType, capsys: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: False))
    refused = run(mail, capsys, "login", "gmail")
    assert refused["error"] == "run this yourself in a terminal: attache skill mail login gmail"
    assert backend(mail, "gmail").calls == []
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))  # as NUL on Windows
    monkeypatch.setenv("ATTACHE_WEB_URL", "http://127.0.0.1:8765")  # the script tool's child
    assert run(mail, capsys, "login", "gmail")["error"].startswith("run this yourself")
    assert backend(mail, "gmail").calls == []
    monkeypatch.delenv("ATTACHE_WEB_URL")
    assert run(mail, capsys, "login", "gmail") == {
        "ok": True,
        "account": "gmail",
        "signed_in": True,
    }
    assert backend(mail, "gmail").calls == [("login",)]


# The audit log


def test_every_call_is_logged_without_subjects_or_bodies(
    mail: ModuleType, capsys: Any, home: Path
) -> None:
    backend(mail, "gmail").items = [summary("101", "2026-10-18T16:42:00Z")]
    run(mail, capsys, "inbox", "--account", "gmail")
    run(mail, capsys, "read", "gmail:101")
    draft = run(mail, capsys, "draft-reply", "gmail:101", "--body", SECRET_BODY)["draft"]
    run(mail, capsys, "move", "gmail:101", "--to", "Receipts")
    run(mail, capsys, "draft-new", "--account", "gmail", "--body", SECRET_BODY, "--bogus")
    run(mail, capsys, "read", "nobody:1")
    lines = log_lines(home)
    commands = [line["command"] for line in lines]
    assert commands == ["inbox", "read", "draft-reply", "move", None, "read"]
    assert lines[0]["ids"] == ["gmail:101"] and lines[0]["account"] == "gmail"
    assert lines[2]["ids"] == ["gmail:101", draft["id"]] and lines[2]["account"] == "gmail"
    assert lines[3]["ids"] == ["gmail:101", "gmail:moved-101"]
    assert all(set(line) >= {"ts", "command", "account", "ids", "ok"} for line in lines)
    assert lines[4] == lines[4] | {"ok": False, "error": "invalid arguments", "ids": []}
    assert lines[5]["ok"] is False and "is not a mail id" in lines[5]["error"]
    text = (home / "logs" / "mail.jsonl").read_text(encoding="utf-8")
    assert SECRET_SUBJECT not in text and SECRET_BODY not in text and "Thursday" not in text


def test_errors_are_one_json_object(mail: ModuleType, capsys: Any) -> None:
    for argv in (["frobnicate"], [], ["read"], ["respond", "outlook:m1", "maybe"]):
        result = run(mail, capsys, *argv)
        assert result["ok"] is False and result["error"].startswith("mail.py")
