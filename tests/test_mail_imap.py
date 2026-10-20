"""The mail skill's IMAP backend (Gmail and Titan), against an in-memory IMAPClient stand-in,
a fake SMTP and a fake keyring. No server, password or keyring is touched."""

from __future__ import annotations

import email
import imaplib
import importlib.util
import json
import re
import smtplib
import ssl
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from attache import skills

SCRIPTS = skills.BUNDLED / "mail" / "scripts"
FIXTURES = Path(__file__).parent / "fixtures"
SEEN, FLAGGED, DRAFT, DELETED = rb"\Seen", rb"\Flagged", rb"\Draft", rb"\Deleted"
NOW = datetime(2026, 10, 20, 9, 30, tzinfo=UTC)
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


class FakeSMTP:
    def __init__(self, server: FakeIMAP) -> None:
        self.server = server

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.quit()

    def send_message(self, msg: email.message.EmailMessage, to_addrs: list[str]) -> None:
        self.server.sent.append((msg, to_addrs))
        if self.server.gmail:  # Gmail files sent mail itself
            self.server.add("[Gmail]/Sent Mail", msg.as_bytes(), [SEEN])

    def quit(self) -> None:
        self.server.calls.append(("smtp quit",))


class FakeIMAP:
    """IMAPClient stand-in, signed in already: folders of messages in memory. On Gmail every
    message is in All Mail too, as the same message with the same X-GM-MSGID."""

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
        for uid in uids:
            message, row = self.messages()[uid], {}
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
                    row[b"BODY[HEADER]"] = re.split(rb"\r?\n\r?\n", message["raw"], maxsplit=1)[0]
                elif partial := re.fullmatch(rb"BODY\.PEEK\[\]<0\.(\d+)>", item):
                    row[b"BODY[]<0>"] = message["raw"][: int(partial[1])]
                else:
                    raise AssertionError(f"unexpected fetch item {item!r}")
            rows[uid] = row
        return rows

    def append(self, folder: str, raw: bytes, flags: Any = (), msg_time: Any = None) -> bytes:
        self.calls.append(("append", folder, set(flags)))
        self.add(folder, raw, flags)
        return b"[APPENDUID 7 1] APPEND completed"

    def add_flags(self, uids: list[int], flags: list[bytes]) -> None:
        self.calls.append(("add_flags", self.selected, list(uids), list(flags)))
        for uid in uids:
            self.messages()[uid]["flags"].update(flags)

    def remove_flags(self, uids: list[int], flags: list[bytes]) -> None:
        self.calls.append(("remove_flags", self.selected, list(uids), list(flags)))
        for uid in uids:
            self.messages()[uid]["flags"].difference_update(flags)

    def add_gmail_labels(self, uids: list[int], labels: list[str]) -> None:
        self.calls.append(("add_gmail_labels", self.selected, list(uids), list(labels)))

    def remove_gmail_labels(self, uids: list[int], labels: list[str]) -> None:
        self.calls.append(("remove_gmail_labels", self.selected, list(uids), list(labels)))

    def move(self, uids: list[int], folder: str) -> None:
        self.calls.append(("move", self.selected, list(uids), folder))
        for uid in uids:
            self._put(folder, self.messages().pop(uid))

    def delete_messages(self, uids: list[int]) -> None:
        self.add_flags(uids, [DELETED])

    def expunge(self, uids: list[int]) -> None:
        self.calls.append(("expunge", self.selected, list(uids)))
        for uid in uids:
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


def titan(fake: FakeIMAP | None = None) -> Any:
    return imap.Backend("titan", TITAN, Path("."), client=fake or FakeIMAP())


def gmail(fake: FakeIMAP | None = None) -> Any:
    return imap.Backend("gmail", GMAIL, Path("."), client=fake or FakeIMAP(gmail=True))


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
    msg = email.message_from_bytes(fixture("html"), policy=email.policy.default)
    text = imap.text_of(msg)
    assert text == "Your order #77 has shipped.\nTrack it & relax.\nQuestions? Ask us"
    assert imap.strip_html("<table><tr><td>a</td><td>b</td></tr></table><!-- x -->") == "a b"


# reading


def test_titan_lists_summaries_newest_first() -> None:
    fake = FakeIMAP()
    old = fake.add("INBOX", fixture("plain"), [SEEN, FLAGGED], NOW - timedelta(days=2))
    new = fake.add("INBOX", fixture("attachment"), (), NOW)
    found = titan(fake).list(None, False, 25, None, None)
    assert [item["id"] for item in found] == [
        imap.encode_id("INBOX", 7, new),
        imap.encode_id("INBOX", 7, old),
    ]
    assert found[1] == {
        "id": imap.encode_id("INBOX", 7, old),
        "folder": "INBOX",
        "subject": "Boiler service quote",
        "from": {"name": "Dan Price", "smtp": "dan@example.com"},
        "received_utc": "2026-10-18T09:30:00Z",
        "preview": found[1]["preview"],
        "unread": False,
        "flagged": True,
    }
    assert found[1]["preview"].startswith("Hi, attached is the quote for the boiler service.")
    assert len(found[1]["preview"]) <= 200 and found[0]["unread"] and not found[0]["flagged"]
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
    with pytest.raises(LookupError, match="list again"):
        backend.read("999", 8000)


def test_read_gives_text_addresses_and_attachments() -> None:
    fake = FakeIMAP()
    plain = imap.encode_id("INBOX", 7, fake.add("INBOX", fixture("plain")))
    files = imap.encode_id("INBOX", 7, fake.add("INBOX", fixture("attachment"), [SEEN]))
    message = titan(fake).read(plain, 8000)
    assert message["to"] == ["you@yourdomain.com", "ann@example.com"]
    assert message["cc"] == ["sam@example.com", "you@yourdomain.com"]
    assert "evil@example.com" in message["body"] and message["truncated"] is False
    assert message["attachments"] == []
    other = titan(fake).read(files, 20)
    assert other["body"] == "The minutes are atta" and other["truncated"] is True
    assert other["attachments"] == [{"name": "minutes.pdf", "size": 19}]
    assert titan(fake).read(files, 8000)["body"] == "The minutes are attached. Café at noon?"
    assert SEEN not in fake.messages("INBOX")[1]["flags"]  # reading peeks


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


def test_titan_moves_and_flags() -> None:
    fake = FakeIMAP()
    natives = [imap.encode_id("INBOX", 7, fake.add("INBOX", fixture("plain"))) for _ in range(2)]
    backend = titan(fake)
    assert backend.flag(natives, True) == natives
    assert backend.mark_read(natives[:1], False) == natives[:1]
    assert ("add_flags", "INBOX", [1, 2], [FLAGGED]) in fake.calls
    assert ("remove_flags", "INBOX", [1], [SEEN]) in fake.calls
    assert ("select", "INBOX", False) in fake.calls  # changes need a read-write select
    assert backend.move(natives, "INBOX/Work") == natives
    assert ("move", "INBOX", [1, 2], "INBOX/Work") in fake.calls
    assert len(fake.messages("INBOX/Work")) == 2 and not fake.messages("INBOX")


def test_gmail_moves_by_label() -> None:
    fake = FakeIMAP(gmail=True)
    fake.add("INBOX", fixture("html"))
    backend = gmail(fake)
    backend.move(["1801234567890123456"], "Receipts")
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
    draft = backend.draft(
        ["Dan <dan@example.com>"], ["sam@example.com"], "Quote", "Yes.", None, False
    )
    assert draft == {
        "id": imap.encode_id("Brouillons", 7, 1),
        "to": ["dan@example.com"],
        "cc": ["sam@example.com"],
        "subject": "Quote",
        "body": "Yes.\n",
    }
    assert ("append", "Brouillons", {DRAFT, SEEN}) in fake.calls
    saved = email.message_from_bytes(fake.messages("Brouillons")[1]["raw"])
    assert saved["From"] == "you@yourdomain.com" and saved["Message-ID"].endswith(
        "@yourdomain.com>"
    )
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


def test_titan_sends_files_in_sent_and_removes_the_draft() -> None:
    fake = FakeIMAP()
    backend = titan(fake)
    draft = backend.draft(["dan@example.com"], ["sam@example.com"], "Quote", "Yes.", None, False)
    raw = fake.messages("Brouillons")[1]["raw"]
    fake.messages("Brouillons")[1]["raw"] = b"Bcc: spy@example.com\r\n" + raw  # edited elsewhere
    sent = backend.send_draft(draft["id"])
    ((message, to_addrs),) = fake.sent
    assert to_addrs == ["dan@example.com", "sam@example.com"] and message["Bcc"] is None
    assert ("append", "Sent", {SEEN}) in fake.calls
    assert sent == imap.encode_id("Sent", 7, 1)
    assert not fake.messages("Brouillons")  # the draft is gone
    assert ("smtp quit",) in fake.calls


def test_a_retry_after_a_send_that_went_through_sends_nothing() -> None:
    fake = FakeIMAP()
    backend = titan(fake)
    draft = backend.draft(["dan@example.com"], [], "Quote", "Yes.", None, False)
    fake.add("Sent", fake.messages("Brouillons")[1]["raw"], [SEEN])  # sent, then a crash
    assert backend.send_draft(draft["id"]) == imap.encode_id("Sent", 7, 1)
    assert fake.sent == [] and not fake.messages("Brouillons")


def test_gmail_files_its_own_sent_mail() -> None:
    fake = FakeIMAP(gmail=True)
    backend = gmail(fake)
    draft = backend.draft(["dan@example.com"], [], "Quote", "Yes.", None, False)
    assert ("select", "[Gmail]/Drafts", True) in fake.calls
    sent = backend.send_draft(draft["id"])
    assert len(fake.sent) == 1 and not any(
        c[:2] == ("append", "[Gmail]/Sent Mail") for c in fake.calls
    )
    sent_copy = next(iter(fake.messages("[Gmail]/Sent Mail").values()))
    assert sent == str(sent_copy["msgid"]) and not fake.messages("[Gmail]/Drafts")


def test_only_drafts_are_sent() -> None:
    fake = FakeIMAP()
    inbox = imap.encode_id("INBOX", 7, fake.add("INBOX", fixture("plain")))
    with pytest.raises(LookupError, match="is not a draft"):
        titan(fake).send_draft(inbox)
    with pytest.raises(LookupError, match="list again"):  # on Gmail only Drafts is searched
        fake = FakeIMAP(gmail=True)
        fake.add("INBOX", fixture("plain"))
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
    assert (
        not ok
        and detail
        == "no password stored for titan: the owner runs `attache skill mail login titan`"
    )

    keyring[("attache-mail", "gmail")] = keyring[("attache-mail", "titan")] = "old"
    fake_imapclient(monkeypatch, refuse=True)
    ok, detail = imap.Backend("gmail", GMAIL, Path(".")).doctor()
    assert not ok and "imap.gmail.com:993 refused the password: make a new app password" in detail
    ok, detail = imap.Backend("titan", TITAN, Path(".")).doctor()
    assert not ok and "'Enable Titan on Other Apps'" in detail and "login again" in detail

    fake_imapclient(monkeypatch, down=True)
    ok, detail = imap.Backend("titan", TITAN, Path(".")).doctor()
    assert not ok and detail.startswith("can't reach the server") and "accounts.toml" in detail

    def refused() -> Any:
        raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")

    fake.smtp = refused
    assert titan(fake).doctor()[1].startswith("imap.titan.email:993 refused the password")


def test_login_checks_the_password_before_keeping_it(
    monkeypatch: pytest.MonkeyPatch, keyring: dict
) -> None:
    import getpass

    prompts: list[str] = []
    monkeypatch.setattr(getpass, "getpass", lambda prompt: prompts.append(prompt) or " abcd efgh ")
    made = fake_imapclient(monkeypatch)
    imap.Backend("gmail", GMAIL, Path(".")).login()
    assert prompts == ["gmail app password for you@gmail.com (not shown): "]
    assert made[0].address == ("imap.gmail.com", 993, True, 30)
    assert made[0].signed_in == ("you@gmail.com", "abcd efgh", False)  # zones kept on dates
    assert keyring == {("attache-mail", "gmail"): "abcd efgh"}

    fake_imapclient(monkeypatch, refuse=True)
    with pytest.raises(PermissionError, match="refused the password"):
        imap.Backend("titan", TITAN, Path(".")).login()
    monkeypatch.setattr(getpass, "getpass", lambda prompt: "  ")
    with pytest.raises(ValueError, match="nothing stored"):
        imap.Backend("titan", TITAN, Path(".")).login()
    assert ("attache-mail", "titan") not in keyring


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


def test_draft_approve_and_send_through_mail_py(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    mail = load("mail")
    (home / "skills" / "mail").mkdir(parents=True)
    accounts = '[titan]\nbackend = "imap"\nuser = "you@yourdomain.com"\nsent_folder = "Sent"\n'
    (home / "skills" / "mail" / "accounts.toml").write_text(accounts, encoding="utf-8")
    fake = FakeIMAP()
    monkeypatch.setattr(
        mail, "open_backend", lambda name, settings, home: imap.Backend(name, settings, home, fake)
    )

    def run(*argv: str) -> dict[str, Any]:
        mail.main(list(argv))
        return json.loads(capsys.readouterr().out)

    args = ["--account", "titan", "--to", "dan@example.com", "--cc", "sam@example.com"]
    draft = run("draft-new", *args, "--subject", "Quote", "--body", "Thursday works.")["draft"]
    assert draft["id"].startswith("titan:") and draft["body"] == "Thursday works.\n"
    (home / ".approvals").mkdir()
    (home / ".approvals" / draft["fingerprint"]).touch()  # the owner's Send, as ask writes it
    sent = run("send-draft", draft["id"], "--fingerprint", draft["fingerprint"])
    assert sent == {"ok": True, "sent": True, "id": "titan:" + imap.encode_id("Sent", 7, 1)}
    assert len(fake.sent) == 1 and not (home / ".approvals" / draft["fingerprint"]).exists()
    last = json.loads((home / "logs" / "mail.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert last["recipients"] == 2 and last["approved"] is True and "Quote" not in json.dumps(last)
