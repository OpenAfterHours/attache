"""The mail skill's IMAP backend (Gmail and Titan), against an in-memory IMAPClient stand-in,
a fake SMTP and a fake keyring. No server, password or keyring is touched."""

from __future__ import annotations

import email
import hashlib
import imaplib
import importlib.util
import json
import re
import smtplib
import ssl
import sys
from datetime import UTC, date, datetime, timedelta
from email import policy
from email.message import EmailMessage
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from attache import skills

SCRIPTS = skills.BUNDLED / "mail" / "scripts"
FIXTURES = Path(__file__).parent / "fixtures"
SEEN, FLAGGED, DRAFT, DELETED = rb"\Seen", rb"\Flagged", rb"\Draft", rb"\Deleted"
NOW = datetime(2026, 10, 20, 9, 30, tzinfo=UTC)
GONE = "no such message (it may have moved)"
TITAN = {
    "backend": "imap",
    "imap": "imap.titan.email:993",
    "smtp": "smtp.titan.email:465",
    "user": "you@yourdomain.com",
    "sent_folder": "Sent",
}
GMAIL = {
    "backend": "imap",
    "kind": "gmail",
    "imap": "imap.gmail.com:993",
    "smtp": "smtp.gmail.com:465",
    "user": "you@gmail.com",
}
TITAN_FOLDERS = {"INBOX": (), "INBOX/Work": (), "Archive": (rb"\Noselect",)}
TITAN_FOLDERS |= {"Brouillons": (rb"\Drafts",), "Sent": (rb"\Sent",)}
GMAIL_FOLDERS = {"INBOX": (), "[Gmail]": (rb"\NoSelect",), "[Gmail]/All Mail": (rb"\All",)}
GMAIL_FOLDERS |= {"[Gmail]/Drafts": (rb"\Drafts",), "[Gmail]/Sent Mail": (rb"\Sent",)}
GMAIL_FOLDERS |= {"Receipts": ()}
ALL_MAIL = "[Gmail]/All Mail"


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


imap = load("imap_backend")


def fixture(name: str) -> bytes:
    return (FIXTURES / f"mail_{name}.eml").read_bytes()


@pytest.fixture(autouse=True)
def in_a_temporary_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The backends below get Path(".") as their home, where .mail/sent.jsonl goes."""
    monkeypatch.chdir(tmp_path)


class FakeSMTP:
    def __init__(self, server: FakeIMAP) -> None:
        self.server = server

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.quit()

    def send_message(self, msg: EmailMessage, to_addrs: list[str]) -> None:
        if self.server.smtp_fails:
            raise self.server.smtp_fails
        self.server.sent.append((msg, to_addrs))
        if self.server.gmail:  # Gmail files sent mail itself
            self.server.add("[Gmail]/Sent Mail", msg.as_bytes(), [SEEN])

    def quit(self) -> None:
        self.server.calls.append(("smtp quit",))


class FakeIMAP:
    """IMAPClient stand-in, signed in already: folders of messages in memory. As RFC 3501
    says, a UID that isn't there is ignored without an error. On Gmail every message is in
    All Mail too, as the same message with the same X-GM-MSGID."""

    def __init__(self, gmail: bool = False) -> None:
        self.gmail = gmail
        self.boxes: dict[str, dict[str, Any]] = {}
        for name, flags in (GMAIL_FOLDERS if gmail else TITAN_FOLDERS).items():
            self.boxes[name] = {"flags": flags, "validity": 7, "next": 1, "messages": {}}
        self.capabilities = {"MOVE", "UIDPLUS"}
        self.calls: list[tuple] = []
        self.sent: list[tuple] = []
        self.selected = ""
        self.next_msgid = 1801234567890123456
        self.smtp_fails: Exception | None = None
        self.append_fails: Exception | None = None

    def smtp(self) -> FakeSMTP:
        return FakeSMTP(self)

    def add(self, folder: str, raw: bytes, flags: Any = (), when: datetime = NOW) -> int:
        message = {"raw": raw, "flags": set(flags), "date": when, "msgid": self.next_msgid}
        self.next_msgid += 1
        if self.gmail and folder != ALL_MAIL:
            self._put(ALL_MAIL, message)
        return self._put(folder, message)

    def _put(self, folder: str, message: dict[str, Any]) -> int:
        box = self.boxes[folder]
        uid, box["next"] = box["next"], box["next"] + 1
        box["messages"][uid] = message
        return uid

    def messages(self, folder: str | None = None) -> dict[int, dict[str, Any]]:
        return self.boxes[folder or self.selected]["messages"]

    def _there(self, uids: list[int]) -> list[int]:
        return [uid for uid in uids if uid in self.messages()]

    # the IMAPClient methods the backend uses

    def list_folders(self) -> list[tuple]:
        return [(box["flags"], b"/", name) for name, box in self.boxes.items()]

    def select_folder(self, folder: str, readonly: bool = False) -> dict[bytes, Any]:
        if folder not in self.boxes:
            raise imaplib.IMAP4.error(f"no folder {folder}")
        self.calls.append(("select", folder, readonly))
        self.selected = folder
        return {b"UIDVALIDITY": self.boxes[folder]["validity"], b"EXISTS": len(self.messages())}

    def folder_status(self, folder: str, items: list[bytes]) -> dict[bytes, int]:
        messages = self.boxes[folder]["messages"].values()
        unseen = sum(SEEN not in message["flags"] for message in messages)
        return {b"MESSAGES": len(messages), b"UNSEEN": unseen}

    def search(self, criteria: list[Any], charset: str | None = None) -> list[int]:
        self.calls.append(("search", self.selected, list(criteria), charset))
        return [uid for uid, msg in self.messages().items() if matches(msg, list(criteria))]

    def fetch(self, uids: list[int], items: list[bytes]) -> dict[int, dict[bytes, Any]]:
        self.calls.append(("fetch", self.selected, list(uids), list(items)))
        rows = {}
        for uid in self._there(uids):
            message, row = self.messages()[uid], {}
            head = re.split(rb"\r?\n\r?\n", message["raw"], maxsplit=1)[0]
            for item in items:
                if item == b"FLAGS":
                    row[item] = tuple(message["flags"])
                elif item == b"INTERNALDATE":
                    row[item] = message["date"]
                elif item == b"X-GM-MSGID":
                    row[item] = message["msgid"]
                elif item == b"BODY.PEEK[]":
                    row[b"BODY[]"] = message["raw"]
                elif item == b"BODY.PEEK[HEADER]":
                    row[b"BODY[HEADER]"] = head
                elif item == b"BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]":
                    found = re.search(rb"(?im)^message-id:.*$", head)
                    row[b"BODY[HEADER.FIELDS (MESSAGE-ID)]"] = found[0] if found else b""
                elif partial := re.fullmatch(rb"BODY\.PEEK\[\]<0\.(\d+)>", item):
                    row[b"BODY[]<0>"] = message["raw"][: int(partial[1])]
                else:
                    raise AssertionError(f"unexpected fetch item {item!r}")
            rows[uid] = row
        return rows

    def append(self, folder: str, raw: bytes, flags: Any = (), msg_time: Any = None) -> bytes:
        if self.append_fails and folder == "Sent":
            raise self.append_fails
        self.calls.append(("append", folder, set(flags)))
        self.add(folder, raw, flags)
        return b"[APPENDUID 7 1] APPEND completed"

    def add_flags(self, uids: list[int], flags: list[bytes]) -> None:
        self.calls.append(("add_flags", self.selected, list(uids), list(flags)))
        for uid in self._there(uids):
            self.messages()[uid]["flags"].update(flags)

    def remove_flags(self, uids: list[int], flags: list[bytes]) -> None:
        self.calls.append(("remove_flags", self.selected, list(uids), list(flags)))
        for uid in self._there(uids):
            self.messages()[uid]["flags"].difference_update(flags)

    def add_gmail_labels(self, uids: list[int], labels: list[str]) -> None:
        self.calls.append(("add_gmail_labels", self.selected, list(uids), list(labels)))

    def remove_gmail_labels(self, uids: list[int], labels: list[str]) -> None:
        self.calls.append(("remove_gmail_labels", self.selected, list(uids), list(labels)))

    def move(self, uids: list[int], folder: str) -> bytes:
        self.calls.append(("move", self.selected, list(uids), folder))
        for uid in self._there(uids):
            self._put(folder, self.messages().pop(uid))
        return b"Move completed"  # IMAPClient gives only this; COPYUID came untagged

    def delete_messages(self, uids: list[int]) -> None:
        self.add_flags(uids, [DELETED])

    def expunge(self, uids: list[int]) -> None:
        self.calls.append(("expunge", self.selected, list(uids)))
        for uid in self._there(uids):
            if DELETED in self.messages()[uid]["flags"]:
                del self.messages()[uid]

    def has_capability(self, name: str) -> bool:
        return name in self.capabilities


def matches(message: dict[str, Any], criteria: list[Any]) -> bool:
    """The few IMAP search keys the backend sends."""
    while criteria:
        key = criteria.pop(0)
        if key == "UNSEEN":
            ok = SEEN not in message["flags"]
        elif key == "SINCE":
            ok = message["date"].date() >= criteria.pop(0)
        elif key in ("TEXT", "X-GM-RAW"):
            ok = criteria.pop(0).lower().encode() in message["raw"].lower()
        elif key == "X-GM-MSGID":
            ok = message["msgid"] == criteria.pop(0)
        elif key == "HEADER":
            name, value = criteria.pop(0), criteria.pop(0)
            ok = value in str(email.message_from_bytes(message["raw"]).get(name, ""))
        else:
            ok = key == "ALL"
        if not ok:
            return False
    return True


def titan(fake: FakeIMAP | None = None, **settings: Any) -> Any:
    return imap.Backend("titan", TITAN | settings, Path("."), client=fake or FakeIMAP())


def gmail(fake: FakeIMAP | None = None) -> Any:
    return imap.Backend("gmail", GMAIL, Path("."), client=fake or FakeIMAP(gmail=True))


def sent_records() -> list[dict[str, Any]]:
    text = Path(".mail", "sent.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


@pytest.fixture
def keyring(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], str]:
    """A keyring in memory, as `keyring` is imported inside functions."""
    store: dict[tuple[str, str], str] = {}
    module = SimpleNamespace(
        get_password=lambda service, user: store.get((service, user)),
        set_password=lambda service, user, password: store.__setitem__((service, user), password),
    )
    monkeypatch.setitem(sys.modules, "keyring", module)
    return store


def fake_imapclient(monkeypatch: pytest.MonkeyPatch, refuse: bool = False, down: bool = False):
    """Put a fake `imapclient` module in place; returns the clients it makes."""
    made: list[Any] = []

    class Client(FakeIMAP):
        def __init__(self, host: str, port: int, ssl: bool, timeout: float) -> None:
            if down:
                raise ConnectionRefusedError(10061, "No connection could be made")
            super().__init__()
            self.address = (host, port, ssl, timeout)
            made.append(self)

        def login(self, user: str, password: str) -> None:
            self.signed_in = (user, password, self.normalise_times)
            if refuse:
                raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials")

    monkeypatch.setitem(sys.modules, "imapclient", SimpleNamespace(IMAPClient=Client))
    return made


# ids and parsing


def test_ids_round_trip_and_bad_ones_are_refused() -> None:
    native = imap.encode_id("INBOX/Work/Big jobs", 7, 42)
    assert re.fullmatch(r"[A-Za-z0-9_-]+", native)
    assert imap.decode_id(native) == ("INBOX/Work/Big jobs", 7, 42)
    for bad in ("!!", native[:-4], "SU5CT1gvc2V2ZW4vNDI", "é"):
        with pytest.raises(LookupError, match="is not a message id"):
            imap.decode_id(bad)


def test_html_bodies_become_text() -> None:
    msg = email.message_from_bytes(fixture("html"), policy=policy.default)
    text = imap.text_of(msg)
    assert text == "Your order #77 has shipped.\nTrack it & relax.\nQuestions? Ask us"
    assert imap.strip_html("<table><tr><td>a</td><td>b</td></tr></table><!-- x -->") == "a b"


# reading


def test_titan_lists_summaries() -> None:
    fake = FakeIMAP()
    old = fake.add("INBOX", fixture("plain"), [SEEN, FLAGGED], NOW - timedelta(days=2))
    new = fake.add("INBOX", fixture("attachment"), (), NOW)
    found = {item["id"]: item for item in titan(fake).list(None, False, 25, None, None)}
    assert set(found) == {imap.encode_id("INBOX", 7, new), imap.encode_id("INBOX", 7, old)}
    first = found[imap.encode_id("INBOX", 7, old)]
    assert first == {
        "id": imap.encode_id("INBOX", 7, old),
        "folder": "INBOX",
        "subject": "Boiler service quote",
        "from": {"name": "Dan Price", "smtp": "dan@example.com"},
        "received_utc": "2026-10-18T09:30:00Z",
        "preview": first["preview"],
        "unread": False,
        "flagged": True,
    }
    assert first["preview"].startswith("Hi, attached is the quote for the boiler service.\n")
    assert found[imap.encode_id("INBOX", 7, new)]["unread"] is True  # mail.py caps previews
    assert ("select", "INBOX", True) in fake.calls  # read-only: listing marks nothing read
    fetched = next(call for call in fake.calls if call[0] == "fetch")
    assert b"BODY.PEEK[]<0.32768>" in fetched[3]  # a partial fetch, not whole attachments


def test_list_filters_and_limits() -> None:
    fake = FakeIMAP()
    for days in (3, 2, 1, 0):
        fake.add("INBOX/Work", fixture("plain"), (), NOW - timedelta(days=days, hours=1))
    backend = titan(fake)
    assert len(backend.list("INBOX/Work", False, 2, None, None)) == 2
    assert fake.calls[-1][2] == [3, 4]  # the newest two
    since = NOW - timedelta(days=1, hours=2)
    found = backend.list("INBOX/Work", True, 25, since, "boiler")
    assert len(found) == 2  # SINCE a day early, then the exact time
    search = [call for call in fake.calls if call[0] == "search"][-1]
    assert search[2] == ["UNSEEN", "SINCE", date(2026, 10, 18), "TEXT", "boiler"]
    assert search[3] == "UTF-8"
    assert backend.list("INBOX", False, 25, None, None) == []


def test_gmail_ids_are_x_gm_msgid_and_search_is_gmails() -> None:
    fake = FakeIMAP(gmail=True)
    fake.add("INBOX", fixture("html"))
    backend = gmail(fake)
    (summary,) = backend.list(None, False, 25, None, "shipped")
    assert summary["id"] == "1801234567890123456"
    assert summary["subject"] == "Your order has shipped \N{PACKAGE}"
    assert next(call for call in fake.calls if call[0] == "search")[2] == ["X-GM-RAW", "shipped"]
    message = backend.read(summary["id"], 8000)
    assert message["folder"] == ALL_MAIL and message["body"].startswith("Your order #77")
    assert ("search", ALL_MAIL, ["X-GM-MSGID", 1801234567890123456], None) in fake.calls
    with pytest.raises(LookupError, match=re.escape(GONE)):
        backend.read("999", 8000)


def test_read_gives_text_addresses_and_attachments() -> None:
    fake = FakeIMAP()
    plain = imap.encode_id("INBOX", 7, fake.add("INBOX", fixture("plain")))
    files = imap.encode_id("INBOX", 7, fake.add("INBOX", fixture("attachment"), [SEEN]))
    message = titan(fake).read(plain, 8000)
    assert message["to"] == ["you@yourdomain.com", "ann@example.com"]
    assert message["cc"] == ["sam@example.com", "you@yourdomain.com"]
    assert "evil@example.com" in message["body"] and message["attachments"] == []
    other = titan(fake).read(files, 20)  # mail.py caps the body
    assert other["body"] == "The minutes are attached. Café at noon?"
    assert other["attachments"] == [{"name": "minutes.pdf", "size": 19}]
    assert SEEN not in fake.messages("INBOX")[1]["flags"]  # reading peeks


def test_a_stale_or_unknown_id_is_no_such_message() -> None:
    fake = FakeIMAP()
    native = imap.encode_id("INBOX", 7, fake.add("INBOX", fixture("plain")))
    backend = titan(fake)
    backend.move([native], "INBOX/Work")
    for call in (
        lambda: backend.read(native, 8000),
        lambda: backend.flag([native], True),
        lambda: backend.mark_read([native], True),
        lambda: backend.move([native], "INBOX"),
        lambda: backend.flag([imap.encode_id("INBOX", 7, 999)], True),
    ):
        with pytest.raises(LookupError, match=re.escape(GONE)):
            call()
    assert all(not message["flags"] for message in fake.messages("INBOX/Work").values())


def test_folders_skip_the_unselectable() -> None:
    fake = FakeIMAP()
    fake.add("INBOX/Work", fixture("plain"))
    fake.add("INBOX/Work", fixture("plain"), [SEEN])
    folders = titan(fake).folders()
    assert [f["path"] for f in folders] == ["INBOX", "INBOX/Work", "Brouillons", "Sent"]
    assert folders[1] == {"folder": "Work", "path": "INBOX/Work", "unread": 1, "total": 2}
    assert [f["path"] for f in gmail().folders()][:2] == ["INBOX", ALL_MAIL]


def test_a_renumbered_folder_is_refused() -> None:
    fake = FakeIMAP()
    native = imap.encode_id("INBOX", 7, fake.add("INBOX", fixture("plain")))
    fake.boxes["INBOX"]["validity"] = 8
    with pytest.raises(LookupError, match="renumbered INBOX"):
        titan(fake).read(native, 8000)


# filing


def test_titan_moves_give_the_new_ids_and_flags_change() -> None:
    fake = FakeIMAP()
    natives = [imap.encode_id("INBOX", 7, fake.add("INBOX", fixture("plain"))) for _ in range(2)]
    backend = titan(fake)
    assert backend.flag(natives, True) == natives
    assert backend.mark_read(natives[:1], False) == natives[:1]
    assert ("add_flags", "INBOX", [1], [FLAGGED]) in fake.calls
    assert ("add_flags", "INBOX", [2], [FLAGGED]) in fake.calls
    assert ("remove_flags", "INBOX", [1], [SEEN]) in fake.calls
    assert ("select", "INBOX", False) in fake.calls  # changes need a read-write select
    moved = backend.move(natives, "INBOX/Work")
    assert moved == [imap.encode_id("INBOX/Work", 7, 1), imap.encode_id("INBOX/Work", 7, 2)]
    assert len(fake.messages("INBOX/Work")) == 2 and not fake.messages("INBOX")
    assert backend.read(moved[0], 8000)["folder"] == "INBOX/Work"  # the new id works


def test_a_moved_message_without_a_message_id_is_reported() -> None:
    fake = FakeIMAP()
    bare = re.sub(rb"(?im)^message-id:.*\r?\n", b"", fixture("plain"))
    native = imap.encode_id("INBOX", 7, fake.add("INBOX", bare))
    with pytest.raises(LookupError, match="moved to INBOX/Work, but can't find it there"):
        titan(fake).move([native], "INBOX/Work")
    assert len(fake.messages("INBOX/Work")) == 1  # it did move


def test_gmail_moves_by_label() -> None:
    fake = FakeIMAP(gmail=True)
    fake.add("INBOX", fixture("html"))
    backend = gmail(fake)
    assert backend.move(["1801234567890123456"], "Receipts") == ["1801234567890123456"]
    labels = [call for call in fake.calls if "gmail_labels" in call[0]]
    assert labels == [
        ("add_gmail_labels", ALL_MAIL, [1], ["Receipts"]),
        ("remove_gmail_labels", ALL_MAIL, [1], ["\\Inbox"]),
    ]
    fake.calls.clear()
    backend.move(["1801234567890123456"], "inbox")
    assert [call for call in fake.calls if "gmail_labels" in call[0]] == [
        ("add_gmail_labels", ALL_MAIL, [1], ["\\Inbox"])
    ]
    assert not any(call[0] == "move" for call in fake.calls)


# drafts and sending


def test_a_new_draft_goes_to_the_special_use_drafts_folder() -> None:
    fake = FakeIMAP()
    backend = titan(fake)
    to, cc = ["Dan <dan@example.com>"], ["sam@example.com"]
    draft = backend.draft(to, cc, "Quote", "Yes.", None, False)
    assert draft == {
        "id": imap.encode_id("Brouillons", 7, 1),
        "to": ["dan@example.com"],
        "cc": ["sam@example.com"],
        "bcc": [],
        "subject": "Quote",
        "body": "Yes.\n",
        "attachments": [],
    }
    assert ("append", "Brouillons", {DRAFT, SEEN}) in fake.calls
    saved = email.message_from_bytes(fake.messages("Brouillons")[1]["raw"])
    assert saved["From"] == "you@yourdomain.com"
    assert saved["Message-ID"].endswith("@yourdomain.com>")
    assert b"\r\n" in fake.messages("Brouillons")[1]["raw"]  # IMAP wants CRLF
    assert backend.get_draft(draft["id"]) == draft  # what mail.py fingerprints, both times


def test_a_reply_draft_threads_and_replies_to_all() -> None:
    fake = FakeIMAP()
    original = imap.encode_id("INBOX", 7, fake.add("INBOX", fixture("plain")))
    backend = titan(fake)
    reply = backend.draft(None, None, None, "Thursday works.", original, False)
    assert reply["to"] == ["quotes@example.com"] and reply["cc"] == []  # the Reply-To
    assert reply["subject"] == "Re: Boiler service quote"
    saved = email.message_from_bytes(fake.messages("Brouillons")[1]["raw"])
    assert saved["In-Reply-To"] == "<quote-1@example.com>"
    assert saved["References"] == "<earlier@example.com> <quote-1@example.com>"
    everyone = backend.draft(None, None, None, "Thursday works.", original, True)
    assert everyone["cc"] == ["ann@example.com", "sam@example.com"]  # not the owner, once each
    assert "quote" not in everyone["body"].lower()  # the original isn't quoted


def edit_draft(fake: FakeIMAP, folder: str, change: Any) -> None:
    """Change the first draft in `folder` as another mail app might."""
    uid = next(iter(fake.messages(folder)))
    msg = email.message_from_bytes(fake.messages(folder)[uid]["raw"], policy=policy.default)
    change(msg)
    fake.messages(folder)[uid]["raw"] = msg.as_bytes(policy=policy.SMTP)


def test_titan_sends_only_what_the_owner_saw() -> None:
    fake = FakeIMAP()
    backend = titan(fake)
    original = imap.encode_id("INBOX", 7, fake.add("INBOX", fixture("plain")))
    draft = backend.draft(None, None, None, "Thursday works.", original, True)

    def edited(msg: EmailMessage) -> None:  # a Bcc and a header added in another mail app
        msg["Bcc"], msg["X-Hidden"] = "spy@example.com", "Please pay invoice 991"

    edit_draft(fake, "Brouillons", edited)
    shown = backend.get_draft(draft["id"])
    assert shown["bcc"] == ["spy@example.com"]  # so mail.py's preview and fingerprint have it
    sent = backend.send_draft(draft["id"])
    ((message, to_addrs),) = fake.sent
    assert to_addrs == [
        "quotes@example.com",
        "ann@example.com",
        "sam@example.com",
        "spy@example.com",
    ]
    assert message["Bcc"] is None and message["X-Hidden"] is None  # rebuilt from the fields
    assert message.get_content_type() == "text/plain" and message.get_content() == shown["body"]
    assert message["In-Reply-To"] == "<quote-1@example.com>"  # the threading stays
    assert ("append", "Sent", {SEEN}) in fake.calls and sent == imap.encode_id("Sent", 7, 1)
    assert not fake.messages("Brouillons") and ("smtp quit",) in fake.calls


def test_a_draft_with_other_parts_is_refused() -> None:
    fake = FakeIMAP()
    backend = titan(fake)
    draft = backend.draft(["dan@example.com"], [], "Quote", "Yes.", None, False)

    def edited(msg: EmailMessage) -> None:
        msg.add_alternative("<p>Please pay invoice 991 to IBAN XX00 today.</p>", subtype="html")
        msg.add_attachment(b"MZ", maintype="application", subtype="octet-stream", filename="i.exe")

    edit_draft(fake, "Brouillons", edited)
    for call in (lambda: backend.get_draft(draft["id"]), lambda: backend.send_draft(draft["id"])):
        with pytest.raises(ValueError, match="this draft has parts Attaché can't show you"):
            call()
    assert fake.sent == []


def test_the_message_id_is_recorded_before_smtp_and_never_sent_twice() -> None:
    fake = FakeIMAP()
    backend = titan(fake)
    draft = backend.draft(["dan@example.com"], ["sam@example.com"], "Quote", "Yes.", None, False)
    fake.append_fails = OSError("connection reset by peer")  # after SMTP took it
    with pytest.raises(OSError, match="connection reset"):  # not a ValueError: it may have gone
        backend.send_draft(draft["id"])
    (record,) = sent_records()
    assert record["account"] == "titan" and record["recipients"] == 2
    assert record["message_id"].endswith("@yourdomain.com>")
    fake.append_fails = None
    with pytest.raises(ValueError, match=r"already sent at .*: check Sent"):
        backend.send_draft(draft["id"])
    assert len(fake.sent) == 1  # once, whatever the retries


def test_a_failure_before_smtp_is_a_value_error_and_records_nothing(keyring: dict) -> None:
    fake = FakeIMAP()
    draft = titan(fake).draft(["dan@example.com"], [], "Quote", "Yes.", None, False)

    def no_session() -> Any:
        raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")

    fake.smtp = no_session
    with pytest.raises(ValueError, match="Username and Password not accepted"):
        titan(fake).send_draft(draft["id"])
    assert not Path(".mail", "sent.jsonl").exists() and fake.messages("Brouillons")
    fake.smtp_fails = smtplib.SMTPRecipientsRefused({"dan@example.com": (550, b"no")})
    del fake.smtp  # back to the class's
    with pytest.raises(smtplib.SMTPRecipientsRefused):  # recorded first: it might have gone
        titan(fake).send_draft(draft["id"])
    assert len(sent_records()) == 1


def test_the_send_limit_counts_recipients_in_the_last_hour() -> None:
    fake = FakeIMAP()
    backend = titan(fake, send_limit_hour=3)
    now = datetime.now(UTC)
    Path(".mail").mkdir()
    with Path(".mail", "sent.jsonl").open("w", encoding="utf-8") as log:
        for minutes, count in ((30, 2), (120, 9)):
            when = (now - timedelta(minutes=minutes)).isoformat(timespec="seconds")
            record = {"ts": when, "account": "titan", "message_id": f"<{minutes}@x>"}
            log.write(json.dumps({**record, "recipients": count}) + "\n")
        log.write('{"cut short\n')
    two = backend.draft(["a@x.com", "b@x.com"], [], "S", "B", None, False)
    with pytest.raises(ValueError, match="titan sends to 3 people an hour; 2 so far"):
        backend.send_draft(two["id"])
    one = backend.draft(["a@x.com"], [], "S", "B", None, False)
    backend.send_draft(one["id"])
    last = Path(".mail", "sent.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    assert len(fake.sent) == 1 and json.loads(last)["recipients"] == 1


def test_gmail_files_its_own_sent_mail() -> None:
    fake = FakeIMAP(gmail=True)
    backend = gmail(fake)
    draft = backend.draft(["dan@example.com"], [], "Quote", "Yes.", None, False)
    assert ("select", "[Gmail]/Drafts", True) in fake.calls
    sent = backend.send_draft(draft["id"])
    appended = [call for call in fake.calls if call[:2] == ("append", "[Gmail]/Sent Mail")]
    assert len(fake.sent) == 1 and not appended
    sent_copy = next(iter(fake.messages("[Gmail]/Sent Mail").values()))
    assert sent == str(sent_copy["msgid"]) and not fake.messages("[Gmail]/Drafts")


def test_only_drafts_are_sent() -> None:
    fake = FakeIMAP()
    inbox = imap.encode_id("INBOX", 7, fake.add("INBOX", fixture("plain")))
    with pytest.raises(ValueError, match="is not a draft"):
        titan(fake).send_draft(inbox)
    fake = FakeIMAP(gmail=True)
    fake.add("INBOX", fixture("plain"))
    with pytest.raises(ValueError, match=re.escape(GONE)):  # on Gmail only Drafts is searched
        gmail(fake).send_draft("1801234567890123456")
    assert fake.sent == []


def test_without_uidplus_the_draft_is_only_marked_deleted() -> None:
    fake = FakeIMAP()
    fake.capabilities = {"MOVE"}
    backend = titan(fake)
    draft = backend.draft(["dan@example.com"], [], "Quote", "Yes.", None, False)
    backend.send_draft(draft["id"])
    assert DELETED in fake.messages("Brouillons")[1]["flags"]
    assert not any(call[0] == "expunge" for call in fake.calls)  # never other deleted mail


def test_calendar_and_tasks_are_outlook_only() -> None:
    backend = titan()
    for call in (
        lambda: backend.agenda(7),
        backend.tasks,
        lambda: backend.respond("x", "accept", None),
        lambda: backend.create_event("S", NOW, NOW, [], None),
    ):
        with pytest.raises(NotImplementedError, match="not in v1 for IMAP accounts"):
            call()


# signing in


def test_doctor_names_the_cause_and_the_fix(monkeypatch: pytest.MonkeyPatch, keyring: dict) -> None:
    fake = FakeIMAP()
    fake.add("INBOX", fixture("plain"))
    fake.add("INBOX", fixture("plain"), [SEEN])
    assert titan(fake).doctor() == (True, "signed in as you@yourdomain.com: 1 unread of 2 in INBOX")
    assert ("smtp quit",) in fake.calls  # SMTP signed in too

    fake_imapclient(monkeypatch)
    ok, detail = imap.Backend("titan", TITAN, Path(".")).doctor()
    expected = "no password stored for titan: the owner runs `attache skill mail login titan`"
    assert not ok and detail == expected

    keyring[("attache-mail", "gmail")] = keyring[("attache-mail", "titan")] = "old"
    fake_imapclient(monkeypatch, refuse=True)
    ok, detail = imap.Backend("gmail", GMAIL, Path(".")).doctor()
    assert not ok and "imap.gmail.com:993 refused the password: make a new app password" in detail
    ok, detail = imap.Backend("titan", TITAN, Path(".")).doctor()
    assert not ok and "'Enable Titan on Other Apps'" in detail and "log in again" in detail

    fake_imapclient(monkeypatch, down=True)
    ok, detail = imap.Backend("titan", TITAN, Path(".")).doctor()
    assert not ok and detail.startswith("can't reach the server") and "accounts.toml" in detail

    def refused() -> Any:
        raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")

    fake.smtp = refused
    assert titan(fake).doctor()[1].startswith("imap.titan.email:993 refused the password")


def test_login_keeps_the_password_in_the_keyring(
    monkeypatch: pytest.MonkeyPatch, keyring: dict
) -> None:
    import getpass

    prompts: list[str] = []
    monkeypatch.setattr(getpass, "getpass", lambda prompt: prompts.append(prompt) or " abcd efgh ")
    imap.Backend("gmail", GMAIL, Path(".")).login()
    assert prompts == ["gmail app password for you@gmail.com (not shown): "]
    assert keyring == {("attache-mail", "gmail"): "abcd efgh"}  # doctor then checks it
    monkeypatch.setattr(getpass, "getpass", lambda prompt: "  ")
    with pytest.raises(ValueError, match="nothing stored"):
        imap.Backend("titan", TITAN, Path(".")).login()
    assert ("attache-mail", "titan") not in keyring


def test_imap_signs_in_with_the_stored_password(monkeypatch: pytest.MonkeyPatch, keyring: dict):
    keyring[("attache-mail", "gmail")] = "abcd efgh"
    made = fake_imapclient(monkeypatch)
    imap.Backend("gmail", GMAIL, Path(".")).folders()
    assert made[0].address == ("imap.gmail.com", 993, True, 30)
    assert made[0].signed_in == ("you@gmail.com", "abcd efgh", False)  # zones kept on dates


def test_smtp_checks_the_servers_certificate(monkeypatch: pytest.MonkeyPatch, keyring: dict):
    keyring[("attache-mail", "titan")] = "secret"
    opened: list[Any] = []

    class SMTPSSL:
        def __init__(self, host: str, port: int, timeout: float, context: ssl.SSLContext):
            opened.append((host, port, context))

        def login(self, user: str, password: str) -> None:
            opened.append((user, password))

    monkeypatch.setattr(imap.smtplib, "SMTP_SSL", SMTPSSL)
    imap.Backend("titan", TITAN, Path("."))._smtp_session()
    (host, port, context), login = opened
    assert (host, port) == ("smtp.titan.email", 465) and login == ("you@yourdomain.com", "secret")
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname


# mail.py and the IMAP backend together


def mail_py(home: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any, fake: FakeIMAP) -> Any:
    """A runner for mail.py, with a Titan account on `fake`."""
    mail = load("mail")
    (home / "skills" / "mail").mkdir(parents=True)
    accounts = '[titan]\nbackend = "imap"\nuser = "you@yourdomain.com"\nsent_folder = "Sent"\n'
    (home / "skills" / "mail" / "accounts.toml").write_text(accounts, encoding="utf-8")
    backend = imap.Backend("titan", TITAN, home, fake)
    monkeypatch.setattr(mail, "open_backend", lambda name, settings, home: backend)

    def run(*argv: str) -> dict[str, Any]:
        mail.main(list(argv))
        return json.loads(capsys.readouterr().out)

    return run


def approve(home: Path, f: str) -> None:
    """The core's ask on Send: the sha256 of the preview text it read and showed."""
    shown = (home / ".approvals" / f"{f}.preview").read_text(encoding="utf-8")
    seen = hashlib.sha256(shown.encode("utf-8")).hexdigest()
    (home / ".approvals" / f).write_text(seen, encoding="utf-8")


def test_draft_approve_and_send_through_mail_py(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    fake = FakeIMAP()
    run = mail_py(home, monkeypatch, capsys, fake)
    args = ["--account", "titan", "--to", "dan@example.com", "--cc", "sam@example.com"]
    draft = run("draft-new", *args, "--subject", "Quote", "--body", "Thursday works.")["draft"]
    f = draft["fingerprint"]
    assert draft["id"].startswith("titan:") and draft["body"] == "Thursday works.\n"
    preview = (home / ".approvals" / f"{f}.preview").read_text(encoding="utf-8")
    assert preview.startswith("Send from titan (imap)\nTo: dan@example.com\nCc: sam@example.com\n")
    approve(home, f)  # the owner's Send
    sent = run("send-draft", draft["id"], "--fingerprint", f)
    assert sent == {"ok": True, "sent": True, "id": "titan:" + imap.encode_id("Sent", 7, 1)}
    assert len(fake.sent) == 1 and not list((home / ".approvals").iterdir())
    last = json.loads((home / "logs" / "mail.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert last["approved"] is True and "Quote" not in json.dumps(last)


def test_a_titan_send_that_failed_after_smtp_is_not_sent_again(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    fake = FakeIMAP()
    run = mail_py(home, monkeypatch, capsys, fake)
    args = ["--account", "titan", "--to", "dan@example.com", "--subject", "Quote", "--body", "Yes."]
    draft = run("draft-new", *args)["draft"]
    f = draft["fingerprint"]
    approve(home, f)
    fake.append_fails = OSError("connection reset by peer")
    first = run("send-draft", draft["id"], "--fingerprint", f)
    assert first["error"] == "connection reset by peer; it may have been sent: check Sent first"
    fake.append_fails = None
    approve(home, f)  # asked again, and the owner said yes again
    retry = run("send-draft", draft["id"], "--fingerprint", f)
    assert retry["error"].startswith("already sent at ") and retry["error"].endswith("check Sent")
    assert len(fake.sent) == 1  # the record in sent.jsonl, not the leftover marker, decides


def test_a_titan_try_that_stopped_before_smtp_can_be_approved_again(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    fake = FakeIMAP()
    run = mail_py(home, monkeypatch, capsys, fake)
    args = ["--account", "titan", "--to", "dan@example.com", "--subject", "Quote", "--body", "Yes."]
    draft = run("draft-new", *args)["draft"]
    f = draft["fingerprint"]
    (home / ".approvals" / f"{f}.sending").write_text("x", encoding="utf-8")  # killed mid-way
    approve(home, f)
    assert run("send-draft", draft["id"], "--fingerprint", f)["sent"]  # nothing was recorded
    assert len(fake.sent) == 1 and not list((home / ".approvals").iterdir())
