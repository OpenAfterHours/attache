"""The email skill's sidecar, offline: its checks on real-looking header blocks (Gmail,
Microsoft 365, and a forged header below the server's own), and its inbox loop against a mail
backend in memory, through the mail skill's real mail.py. No mail server, Outlook or network."""

from __future__ import annotations

import importlib.util
import io
import json
import queue
import re
import sys
import threading
import tomllib
import types
from datetime import UTC, datetime, timedelta
from email.parser import HeaderParser
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from attache import skills

FOLDER = skills.BUNDLED / "email"
SCRIPTS = skills.BUNDLED / "mail" / "scripts"


def load(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses look their module up there
    spec.loader.exec_module(module)
    return module


channel = load("email_channel", FOLDER / "channel.py")


# Header blocks, top first, as each store gives them.

GOOGLE_PASS = """mx.google.com;
       dkim=pass header.i=@example.com header.s=s1 header.b=Ab12Cd34;
       spf=pass (google.com: domain of dan@example.com designates 203.0.113.5 as
       permitted sender) smtp.mailfrom=dan@example.com;
       dmarc=pass (p=NONE sp=NONE dis=NONE) header.from=example.com"""
GOOGLE_FAIL = """mx.google.com;
       spf=pass (google.com: domain of bounce@evil.example designates 198.51.100.7 as
       permitted sender) smtp.mailfrom=bounce@evil.example;
       dmarc=fail (p=NONE sp=NONE dis=NONE) header.from=example.com"""
GOOGLE_NO_DMARC = """mx.google.com;
       spf=pass (google.com: domain of bounce@evil.example designates 198.51.100.7 as
       permitted sender) smtp.mailfrom=bounce@evil.example"""
DAN = """From: Dan Price <dan@example.com>
Reply-To: Dan Price <quotes@example.com>
To: you@gmail.com
Subject: @attache run the VA
Date: Tue, 20 Oct 2026 10:29:58 +0100
Message-ID: <va-1@example.com>
MIME-Version: 1.0
Content-Type: text/plain; charset=utf-8
"""
FORGED = "Authentication-Results: mx.google.com; dmarc=pass header.from=example.com\n" + DAN


def gmail(results: str = GOOGLE_PASS, sender: str = DAN) -> str:
    """What Gmail's IMAP gives: Google's trace and its results on top, then the sender's own
    headers, which may hold anything."""
    return (
        "Delivered-To: you@gmail.com\n"
        "Received: by 2002:a05:7300:5f05 with SMTP id x5csp1234567dyc;\n"
        "        Tue, 20 Oct 2026 02:30:01 -0700 (PDT)\n"
        f"ARC-Authentication-Results: i=1; {GOOGLE_PASS}\n"
        "Return-Path: <dan@example.com>\n"
        "Received: from mail.example.com (mail.example.com. [203.0.113.5])\n"
        "        by mx.google.com with ESMTPS id a1si1234567wrx.2026.10.20.02.30.00\n"
        "        for <you@gmail.com>; Tue, 20 Oct 2026 02:30:00 -0700 (PDT)\n"
        f"Authentication-Results: {results}\n"
        f"{sender}"
    )


M365_PASS = """spf=pass (sender IP is 203.0.113.5)
 smtp.mailfrom=example.com; dkim=pass (signature was verified)
 header.d=example.com;dmarc=pass action=none
 header.from=example.com;compauth=pass reason=100"""


def m365(results: str = M365_PASS, sender: str = DAN, auth_as: str = "Anonymous") -> str:
    """What Exchange Online keeps (Graph's internetMessageHeaders, COM's transport headers)
    for mail from outside the organisation."""
    return (
        "Received: from DB9PR01MB5678.eurprd01.prod.outlook.com (2603:10a6:10:1::12) by\n"
        " AM0PR01MB1234.eurprd01.prod.outlook.com with HTTPS; Tue, 20 Oct 2026 09:30:02 +0000\n"
        "Received: from mail.example.com (203.0.113.5) by\n"
        " DB9PR01MB5678.mail.protection.outlook.com (10.167.16.99) with Microsoft SMTP\n"
        " Server id 15.20.8000.12 via Frontend Transport; Tue, 20 Oct 2026 09:30:01 +0000\n"
        f"Authentication-Results: {results}\n"
        "Received-SPF: Pass (protection.outlook.com: domain of example.com designates\n"
        " 203.0.113.5 as permitted sender) receiver=protection.outlook.com;\n"
        f"{sender}"
        f"X-MS-Exchange-Organization-AuthAs: {auth_as}\n"
        "X-MS-Exchange-Organization-AuthSource: DB9PR01MB5678.eurprd01.prod.outlook.com\n"
    )


INTERNAL = """Received: from AM0PR01MB1234.eurprd01.prod.outlook.com (2603:10a6:208::12) by
 AM0PR01MB9999.eurprd01.prod.outlook.com with HTTPS; Tue, 20 Oct 2026 09:30:02 +0000
From: Sam Lee <Sam@Corp.example>
To: You <you@corp.example>
Subject: @attache is Phil free on Tuesday?
Message-ID: <diary-1@corp.example>
X-MS-Exchange-Organization-AuthAs: Internal
X-MS-Exchange-Organization-AuthMechanism: 04
X-MS-Exchange-Organization-AuthSource: AM0PR01MB1234.eurprd01.prod.outlook.com
"""


def head(raw: str) -> Any:
    return HeaderParser().parsestr(raw)


def proven(raw: str, address: str = "dan@example.com", exchange: bool = False) -> bool:
    return channel.authenticated(head(raw), address, exchange)


# Who sent it


def test_gmail_and_microsoft_365_results_prove_the_sender() -> None:
    assert proven(gmail())
    assert proven(gmail().replace("\n", "\r\n"))  # IMAP's line ends
    assert proven(m365())
    assert proven(m365(), exchange=True)  # an outside sender on Exchange: by DMARC


def test_only_the_topmost_results_count() -> None:
    """The receiving server prepends its own; one further down may be the sender's."""
    assert not proven(gmail(GOOGLE_FAIL, FORGED))
    assert not proven(gmail(GOOGLE_NO_DMARC, FORGED))  # a domain without DMARC, then forged
    assert not proven(m365(M365_PASS.replace("dmarc=pass", "dmarc=fail"), FORGED))
    assert not proven(gmail(GOOGLE_FAIL, DAN + f"Authentication-Results: {GOOGLE_PASS}\n"))
    assert not proven(DAN)  # no results at all


def test_the_pass_must_be_for_the_from_domain() -> None:
    assert not proven(gmail(), "dan@evil.example")
    assert not proven(gmail(), "dan@mail.example.com")  # a subdomain is another domain
    shouting = GOOGLE_PASS.replace("header.from=example.com", "header.from=Example.COM")
    assert proven(gmail(shouting), "dan@example.com")


@pytest.mark.parametrize(
    "results",
    [
        # the sender's words in a comment: the real result follows, so there are two
        "mx.google.com; spf=pass (domain of x);dmarc=pass header.from=example.com;(@evil.example)"
        " smtp.mailfrom=evil.example; dmarc=fail header.from=example.com",
        # in a comment and nothing else: no DMARC result at all
        "mx.google.com; spf=pass (google.com: dmarc=pass header.from=example.com)",
        # a quoted local part, which may hold anything
        'mx.google.com; spf=pass smtp.mailfrom="x;dmarc=pass header.from=example.com"@evil.example',
        # two results, even two passes: one of them may be the sender's
        "mx.google.com; dmarc=pass header.from=example.com; dmarc=pass header.from=example.com",
        "mx.google.com; dmarc=passed header.from=example.com",
        "mx.google.com; dmarc = pass header.from=example.com",  # not the servers' form: fails
    ],
)
def test_words_the_sender_can_reach_never_count(results: str) -> None:
    assert not proven(gmail(results))


def test_comments_are_dropped() -> None:
    assert channel.uncommented("a (b (c) d) e") == "a  e"
    assert channel.uncommented(r"a (b \) c) d") == "a  d"  # an escaped ) stays in the comment
    assert channel.uncommented("a ) b") == "a  b"  # a stray ) closes nothing


def test_exchange_internal_mail_counts_only_on_exchange() -> None:
    sam = "sam@corp.example"
    assert proven(INTERNAL, sam, exchange=True)
    assert not proven(INTERNAL, sam, exchange=False)  # elsewhere anyone can write that header
    assert not proven(m365(sender=DAN, auth_as="Anonymous"), sam, exchange=True)
    both = INTERNAL + "X-MS-Exchange-Organization-AuthAs: Anonymous\n"
    assert not proven(both, sam, exchange=True)  # every copy must say Internal
    assert not proven(gmail(GOOGLE_FAIL, FORGED + "X-MS-Exchange-Organization-AuthAs: Internal\n"))


def test_outlook_classic_trusts_authas_only_on_an_exchange_store() -> None:
    def com(store_type: int) -> Any:
        store = SimpleNamespace(ExchangeStoreType=store_type)
        return SimpleNamespace(session=SimpleNamespace(DefaultStore=store))

    assert channel.on_exchange("com", com(0)) and channel.on_exchange("com", com(4))
    assert not channel.on_exchange("com", com(channel.NOT_EXCHANGE))  # Gmail in Outlook, say
    assert channel.on_exchange("graph", None) and not channel.on_exchange("imap", None)


@pytest.mark.parametrize(
    ("raw", "address"),
    [
        ("From: Dan Price <Dan@Example.com>\n", "dan@example.com"),
        ('From: "dan@example.com" <evil@evil.example>\n', "evil@evil.example"),
        ("From: a@example.com, b@example.com\n", None),
        ("From: a@example.com\nFrom: b@example.com\n", None),
        ("From: Dan Price\n", None),
        ("To: you@gmail.com\n", None),
    ],
)
def test_the_from_address(raw: str, address: str | None) -> None:
    assert channel.from_address(head(raw)) == address


@pytest.mark.parametrize(
    ("raw", "machine"),
    [
        ("Auto-Submitted: auto-replied\n", True),
        ("Auto-Submitted: auto-generated (vacation)\n", True),
        ("Auto-Submitted:\n", True),
        ("Auto-Submitted: no\n", False),
        ("Auto-Submitted: No (typed by a person)\n", False),
        ("List-Id: Parents <parents.school.example>\n", True),
        ("List-Unsubscribe: <mailto:leave@lists.example>\n", True),
        ("Precedence: bulk\n", True),
        ("Precedence: List\n", True),
        ("Precedence: junk\n", True),
        ("Precedence: first-class\n", False),
        ("Subject: hello\n", False),
    ],
)
def test_automatic_mail(raw: str, machine: bool) -> None:
    assert channel.automatic(head(raw)) is machine


@pytest.mark.parametrize(
    ("text", "mentioned"),
    [
        ("@attache run the VA", True),
        ("Hi @Attaché, is Phil free?", True),
        ("@ATTACHÉ: diary", True),
        ("thanks (@attache)", True),
        ("write to dan@attache.org", False),
        ("@attachements", False),
        ("attache, please", False),
        ("", False),
    ],
)
def test_mentions_ignore_case_and_accents(text: str, mentioned: bool) -> None:
    assert channel.mentions(text, "@attache") is mentioned
    assert channel.mentions(text, "@Attaché") is mentioned


@pytest.mark.parametrize(
    ("body", "new"),
    [
        (
            "Run it today?\n\nOn Tue, 20 Oct 2026 at 09:30, Dan Price <dan@example.com> wrote:\n"
            "> @attache run the VA",
            "Run it today?",
        ),
        (
            "Run it.\nOn Tue, 20 Oct 2026 at 09:30, Dan Price <\ndan@example.com> wrote:\n> old",
            "Run it.",
        ),  # Gmail wraps the line
        ("Yes please.\r\n\r\n> earlier\r\n> more", "Yes please."),
        ("Thanks\n\n-----Original Message-----\nFrom: Attaché\nSent: Monday", "Thanks"),
        (
            "Tuesday works.\n\n________________________________\nFrom: Phil <phil@corp.example>\n"
            "Sent: Monday, 19 October 2026 17:02\nTo: Dan\nSubject: Diary",
            "Tuesday works.",
        ),
        (
            "From: the top of the hill\nyou can see the sea",
            "From: the top of the hill\nyou can see the sea",
        ),  # From: alone isn't Outlook's block
        ("On Monday I'm out.", "On Monday I'm out."),
        ("  plain  ", "plain"),
    ],
)
def test_quoted_history_is_cut(body: str, new: str) -> None:
    assert channel.new_part(body) == new


# The inbox loop, against a backend in memory and the mail skill's own mail.py

NOW = datetime.now(UTC).replace(microsecond=0)


class FakeBackend:
    """The mail backend calls the channel makes, in memory, each noted with its thread."""

    def __init__(self) -> None:
        self.messages: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple] = []
        self.threads: set[int] = set()
        self.drafts: dict[str, dict[str, Any]] = {}
        self.sent: list[dict[str, Any]] = []
        self.broken: set[str] = set()  # ids whose headers can't be read just now
        self.redirect: list[str] | None = None  # a backend that ignored `to`
        self.on_list: Any = None

    def add(self, native: str, headers: str, body: str, when: datetime = NOW, **fields: Any):
        message = {"headers": headers, "body": body, "received": when, **fields}
        self.messages[native] = {"subject": "@attache run the VA", "name": "Dan Price"} | message

    def note(self, *call: Any) -> None:
        self.calls.append(call)
        self.threads.add(threading.get_ident())

    def list(self, folder, unread, limit, since, query) -> list[dict[str, Any]]:
        self.note("list", folder, unread, limit, since, query)
        if self.on_list:
            self.on_list()
        found = [(m["received"], n) for n, m in self.messages.items() if m["received"] >= since]
        stamp = "%Y-%m-%dT%H:%M:%SZ"
        return [{"id": n, "received_utc": when.strftime(stamp)} for when, n in found][-limit:]

    def headers(self, native: str) -> str:
        self.note("headers", native)
        if native in self.broken:
            raise OSError("connection reset")
        return self.messages[native]["headers"]

    def read(self, native: str, max_chars: int) -> dict[str, Any]:
        self.note("read", native, max_chars)
        message = self.messages[native]
        sender = {"name": message["name"], "smtp": "dan@example.com"}
        return {"id": native, "subject": message["subject"], "from": sender,
                "body": message["body"][:max_chars]}  # fmt: skip

    def draft(self, to, cc, subject, body, reply_to, reply_all) -> dict[str, Any]:
        self.note("draft", to, cc, subject, body, reply_to, reply_all)
        native = f"draft-{len(self.drafts) + 1}"
        self.drafts[native] = {"id": native, "to": self.redirect or to, "cc": cc, "bcc": []}
        self.drafts[native] |= {"subject": "Re: x", "body": body, "attachments": []}
        return self.drafts[native]

    def send_draft(self, native: str) -> str:
        self.note("send_draft", native)
        self.sent.append(self.drafts[native])
        return native


@pytest.fixture
def mail_py() -> types.ModuleType:
    return load("email_test_mail", SCRIPTS / "mail.py")


@pytest.fixture
def fake() -> FakeBackend:
    return FakeBackend()


def make_inbox(home: Path, mail_py: types.ModuleType, fake: FakeBackend, monkeypatch) -> Any:
    """An Inbox on account "gmail", whose backend is `fake`, with its state in home/inbox."""
    (home / "skills" / "mail").mkdir(parents=True, exist_ok=True)
    accounts = '[gmail]\nbackend = "imap"\nuser = "you@gmail.com"\n'
    (home / "skills" / "mail" / "accounts.toml").write_text(accounts, encoding="utf-8")
    monkeypatch.setattr(mail_py, "open_backend", lambda name, settings, home: fake)
    (home / "inbox").mkdir(exist_ok=True)
    state = channel.State.load(home / "inbox" / ".state.json")
    return channel.Inbox(mail_py, mail_py.Mail(home), "gmail", state, {"you@gmail.com"})


@pytest.fixture
def inbox(home: Path, mail_py: types.ModuleType, fake: FakeBackend, monkeypatch) -> Any:
    return make_inbox(home, mail_py, fake, monkeypatch)


def events(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def messages(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    return [e for e in events(capsys) if e["type"] == "message"]


BODY = "Can you run it today?\n\nOn Mon, 19 Oct 2026, Phil <you@gmail.com> wrote:\n> Sure."


def test_a_first_start_never_answers_old_mail(inbox: Any, fake: FakeBackend, capsys) -> None:
    fake.add("old", gmail(), BODY, when=NOW - timedelta(hours=1))
    inbox.check()
    assert messages(capsys) == []
    since = fake.calls[0][4]
    assert since >= NOW - timedelta(seconds=1) and fake.calls[0][:4] == ("list", None, False, 50)
    fake.add("new", gmail(), BODY, when=datetime.now(UTC) + timedelta(seconds=1))
    inbox.check()
    assert [m["message_id"] for m in messages(capsys)] == ["new"]


def test_an_admitted_message(inbox: Any, fake: FakeBackend, home: Path, capsys) -> None:
    fake.add("m1", gmail(sender=DAN.replace("dan@example.com", "Dan@Example.com")), BODY)
    inbox.state.since = (NOW - timedelta(minutes=1)).isoformat()
    inbox.check()
    (message,) = messages(capsys)
    assert message == {
        "type": "message",
        "chat_id": "dan@example.com",
        "sender_id": "dan@example.com",
        "sender_name": "Dan Price",
        "text": "Subject: @attache run the VA\n\nCan you run it today?",
        "is_group": False,
        "mentioned": True,
        "message_id": "m1",
        "files": [],
    }
    saved = json.loads((home / "inbox" / ".state.json").read_text(encoding="utf-8"))
    assert saved["done"] == ["m1"] and saved["latest"] == {"dan@example.com": "m1"}
    assert ("read", "m1", 4000) in fake.calls  # no more than the text passed on


def test_nothing_is_passed_on_twice_across_restarts(
    home: Path, mail_py: types.ModuleType, fake: FakeBackend, monkeypatch, capsys
) -> None:
    fake.add("m1", gmail(), BODY)
    first = make_inbox(home, mail_py, fake, monkeypatch)
    first.state.since = (NOW - timedelta(minutes=10)).isoformat()
    first.check()
    assert len(messages(capsys)) == 1
    # the watermark moves up to 5 minutes before the newest mail, and never back
    assert datetime.fromisoformat(first.state.since) == NOW - channel.OVERLAP
    again = make_inbox(home, mail_py, fake, monkeypatch)  # a restart, from the state file
    again.check()
    assert messages(capsys) == [] and again.state.latest == {"dan@example.com": "m1"}
    assert again.state.since == first.state.since


def test_skipped_mail_is_logged_by_id_only(inbox: Any, fake: FakeBackend, capsys) -> None:
    own = DAN.replace("Dan Price <dan@example.com>", "You <You@Gmail.com>")
    fake.add("own", gmail(sender=own), BODY)
    fake.add("auto", gmail(sender=DAN + "Auto-Submitted: auto-replied\n"), BODY)
    fake.add("list", gmail(sender=DAN + "List-Id: <va.lists.example>\n"), BODY)
    fake.add("forged", gmail(GOOGLE_FAIL, FORGED), BODY)
    fake.add("quiet", gmail(), "Can you run it?", subject="The VA")
    fake.add("quoted", gmail(), "Thanks!\n\n> @attache run the VA", subject="Re: the VA")
    inbox.state.since = (NOW - timedelta(minutes=1)).isoformat()
    inbox.check()
    logs = [e for e in events(capsys) if e["type"] == "log"]
    assert [e["text"] for e in logs] == [
        "skipped message own: from this account",
        "skipped message auto: automatic or list mail",
        "skipped message list: automatic or list mail",
        "skipped message forged: sender not authenticated",
        "skipped message quiet: no mention",
        "skipped message quoted: no mention",  # a mention in the quoted history doesn't count
    ]
    assert {e["level"] for e in logs} == {"info"}
    assert [call[1] for call in fake.calls if call[0] == "read"] == ["quiet", "quoted"]
    assert inbox.state.latest == {}  # only the proven are read at all, and none admitted


def test_the_text_is_capped(inbox: Any, fake: FakeBackend, capsys) -> None:
    fake.add("long", gmail(), "@attache " + "x" * 9000)
    inbox.state.since = (NOW - timedelta(minutes=1)).isoformat()
    inbox.check()
    (message,) = messages(capsys)
    assert len(message["text"]) == 4000


def test_a_message_that_fails_is_tried_again(inbox: Any, fake: FakeBackend, capsys) -> None:
    fake.add("m1", gmail(), BODY, when=NOW - timedelta(seconds=2))
    fake.add("m2", gmail(), BODY)
    fake.broken = {"m1"}
    inbox.state.since = (NOW - timedelta(minutes=1)).isoformat()
    inbox.check()
    out = events(capsys)
    assert [e["message_id"] for e in out if e["type"] == "message"] == ["m2"]
    assert any(e["text"] == "couldn't check message m1: connection reset" for e in out)
    fake.broken = set()
    inbox.check()
    assert [m["message_id"] for m in messages(capsys)] == ["m1"]


def admitted(inbox: Any, fake: FakeBackend, *natives: str) -> None:
    for n, native in enumerate(natives):
        fake.add(native, gmail(), BODY, when=NOW - timedelta(seconds=len(natives) - n))
    inbox.state.since = (NOW - timedelta(minutes=1)).isoformat()
    inbox.check()


def send(chat_id: str = "dan@example.com", **fields: Any) -> dict[str, Any]:
    return {"type": "send", "chat_id": chat_id, "text": "Started.", "files": [], **fields}


def test_a_reply_goes_to_the_from_address_alone(
    inbox: Any, fake: FakeBackend, home: Path, capsys
) -> None:
    admitted(inbox, fake, "m1", "m2")  # Dan's mail has a Reply-To: it is never used
    capsys.readouterr()
    inbox.answer(send(reply_to="m1"))
    signed = "Started.\n\nSent by Attaché, an assistant."
    drafted = [call for call in fake.calls if call[0] == "draft"]
    assert drafted == [("draft", ["dan@example.com"], [], None, signed, "m2", False)]  # latest
    assert [d["id"] for d in fake.sent] == ["draft-1"] and capsys.readouterr().out == ""
    (line,) = (home / "logs" / "mail.jsonl").read_text(encoding="utf-8").splitlines()
    audit = json.loads(line)
    assert audit["command"] == "email-reply" and audit["account"] == "gmail" and audit["ok"]
    assert audit["ids"] == ["gmail:m2", "gmail:draft-1"] and "Started" not in line


def test_never_a_cold_email(inbox: Any, fake: FakeBackend, home: Path, capsys) -> None:
    admitted(inbox, fake, "m1")
    capsys.readouterr()
    inbox.answer(send("sam@example.com"))
    (event,) = events(capsys)
    assert event["level"] == "warning" and "never a new email" in event["text"]
    assert "sam@" not in event["text"]
    assert not [call for call in fake.calls if call[0] in ("draft", "send_draft")]
    assert not (home / "logs" / "mail.jsonl").exists()


def test_files_are_not_sent(inbox: Any, fake: FakeBackend, capsys) -> None:
    admitted(inbox, fake, "m1")
    capsys.readouterr()
    inbox.answer(send(files=["C:/files/report.pdf"]))
    assert events(capsys)[0]["text"] == "1 file(s) not sent: email replies are text only"
    assert len(fake.sent) == 1


def test_a_draft_to_anyone_else_is_not_sent(
    inbox: Any, fake: FakeBackend, home: Path, capsys
) -> None:
    admitted(inbox, fake, "m1")
    capsys.readouterr()
    fake.redirect = ["quotes@example.com"]  # as a backend that answered the Reply-To
    inbox.answer(send())
    assert fake.sent == [] and "isn't to the sender alone" in events(capsys)[0]["text"]
    audit = json.loads((home / "logs" / "mail.jsonl").read_text(encoding="utf-8"))
    assert audit["ok"] is False and audit["ids"] == ["gmail:m1"]


def test_ask_numbers_the_options(inbox: Any, fake: FakeBackend) -> None:
    admitted(inbox, fake, "m1")
    question = {"type": "ask", "chat_id": "dan@example.com", "ask_id": "a1", "text": "Run it?"}
    inbox.answer(question | {"options": ["Approve", "Decline"]})
    asked = "Run it?\n\nReply 1 for Approve, 2 for Decline."
    assert fake.sent[0]["body"] == f"{asked}\n\n{channel.SIGNATURE}"


def stdin(monkeypatch: pytest.MonkeyPatch, text: str) -> io.TextIOWrapper:
    stream = io.TextIOWrapper(io.BytesIO(text.encode("utf-8")), encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", stream)
    return stream


def test_every_mail_call_is_on_the_main_thread(
    inbox: Any, fake: FakeBackend, monkeypatch, capsys
) -> None:
    """COM objects belong to the thread that made them: the reader thread only queues."""
    fake.add("m1", gmail(), BODY)
    inbox.state.since = (NOW - timedelta(minutes=1)).isoformat()
    stdin(monkeypatch, json.dumps(send()) + "\nnoise\n" + '{"type": "stop"}\n')
    commands: queue.Queue[Any] = queue.Queue()
    reader = threading.Thread(target=channel.read_commands, args=(commands,), daemon=True)
    fake.on_list = lambda: reader.is_alive() or reader.start()  # commands come mid-check
    assert channel.serve(inbox, commands, 60.0) == 0
    assert fake.threads == {threading.get_ident()} and reader.ident != threading.get_ident()
    assert [call[0] for call in fake.calls].count("list") == 1
    assert len(fake.sent) == 1 and fake.sent[0]["to"] == ["dan@example.com"]


class Commands:
    """A command queue that is empty `empties` times, then says stop, on a clock of our own."""

    def __init__(self, clock: list[float], empties: int) -> None:
        self.clock, self.empties, self.waits = clock, empties, []

    def get(self, timeout: float) -> dict[str, Any]:
        self.waits.append(timeout)
        if len(self.waits) > self.empties:
            return {"type": "stop"}
        self.clock[0] += 1800  # half an hour passes each time
        raise queue.Empty


def test_a_failed_check_backs_off_and_hints_once_an_hour(monkeypatch, capsys) -> None:
    clock = [1000.0]
    monkeypatch.setattr(channel.time, "monotonic", lambda: clock[0])
    opened = {"gmail": "a signed-in backend"}

    def check() -> None:
        raise OSError("imap.gmail.com refused the password")

    inbox = SimpleNamespace(check=check, account="gmail", mail=SimpleNamespace(opened=opened))
    commands = Commands(clock, 6)
    assert channel.serve(inbox, commands, 60.0) == 0
    assert opened == {}  # signs in afresh next time
    assert commands.waits == [0, 120, 240, 480, 960, 960, 960]  # doubling, to 16 checks' time
    texts = [e["text"] for e in events(capsys)]
    assert len(texts) == 6 and all(t.startswith("checking gmail failed: imap") for t in texts)
    hinted = [n for n, t in enumerate(texts) if t.endswith("attache skill mail doctor")]
    assert hinted == [0, 2, 4]  # once an hour at most, as half an hour passes between checks


def test_without_an_account_it_explains_and_waits_for_stop(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ATTACHE_ACCOUNT", raising=False)
    stdin(monkeypatch, 'noise\n{"type": "send", "chat_id": "x"}\n{"type": "stop"}\n')
    assert channel.main() == 2
    out = capsys.readouterr()
    assert "[channels.email] needs account" in out.err and out.out == ""
    monkeypatch.setenv("ATTACHE_ACCOUNT", "gmail")
    stdin(monkeypatch, "")  # the end of stdin counts as stop
    assert channel.main() == 2
    assert "the mail skill isn't installed" in capsys.readouterr().err


def test_open_inbox_uses_the_installed_mail_skill(home: Path, monkeypatch) -> None:
    skills.install_bundled(home)
    accounts = '[gmail]\nbackend = "imap"\nuser = "You@Gmail.com"\n[work]\nbackend = "com"\n'
    (home / "skills" / "mail" / "accounts.toml").write_text(accounts, encoding="utf-8")
    monkeypatch.setattr(sys, "path", list(sys.path))
    sys.modules.pop("mail", None)
    try:
        monkeypatch.setenv("ATTACHE_ACCOUNT", "gmail")
        monkeypatch.setenv("ATTACHE_FILES", str(home / "files" / "inbox" / "email"))
        monkeypatch.setenv("ATTACHE_MENTION", "@bot")
        found = channel.open_inbox(home)
        assert found.account == "gmail" and found.own == {"you@gmail.com"}
        assert found.mention == "@bot" and found.signature == channel.SIGNATURE
        assert found.state.path == home / "files" / "inbox" / "email" / ".state.json"
        assert Path(found.skill.__file__).parent == home / "skills" / "mail" / "scripts"
        monkeypatch.setenv("ATTACHE_ACCOUNT", "work")
        assert channel.open_inbox(home).own == set()  # no user: nothing known as its own
        monkeypatch.setenv("ATTACHE_ACCOUNT", "titan")
        with pytest.raises(Exception, match="no account 'titan'"):
            channel.open_inbox(home)
    finally:
        sys.modules.pop("mail", None)


def test_the_state_file(tmp_path: Path, capsys) -> None:
    path = tmp_path / ".state.json"
    state = channel.State.load(path)
    assert datetime.fromisoformat(state.since) >= NOW and state.done == []
    state.done += [str(n) for n in range(600)]
    state.save()
    again = channel.State.load(path)
    assert again.done == [str(n) for n in range(100, 600)] and again.since == state.since
    assert [p.name for p in tmp_path.iterdir()] == [".state.json"]  # written by a rename
    path.write_text("{not json", encoding="utf-8")
    fresh = channel.State.load(path)
    assert fresh.done == [] and "starting from now" in events(capsys)[0]["text"]


def test_parse_and_read_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    assert channel.parse_command('{"type": "send", "chat_id": "a@b.c"}')["type"] == "send"
    for line in ["", "noise", "[1]", '{"type": "dance"}', '{"type": ["send"]}']:
        assert channel.parse_command(line) is None
    stdin(monkeypatch, '{"type": "ask"}\nnoise\n{"type": "answered"}\n')
    commands: queue.Queue[Any] = queue.Queue()
    channel.read_commands(commands)
    assert [commands.get_nowait() for _ in range(3)] == [
        {"type": "ask"},
        {"type": "answered"},
        None,  # the end of stdin: Attaché has gone
    ]


# The skill


def test_skill_md() -> None:
    meta, body = skills.parse((FOLDER / "SKILL.md").read_text(encoding="utf-8"))
    assert meta["name"] == "email" and meta["metadata"] == {"attache-channel": "channel.py"}
    assert "attache-roles" not in meta["metadata"]  # setup is for the owner
    for needed in ("[channels.email]", 'email = "dan@example.com"', "[owner.ids]", "dmarc=pass"):
        assert needed in body, needed
    assert "Reply-To" in body and "doctor" in body


def test_pep_723_header_is_the_mail_skills() -> None:
    """The channel runs mail.py's backends, so it needs exactly their dependencies."""

    def header(path: Path) -> dict[str, Any]:
        source = path.read_text(encoding="utf-8")
        block = re.search(r"(?m)^# /// script\n((?:#.*\n)+?)^# ///$", source)
        assert block
        return tomllib.loads("\n".join(line[2:] for line in block[1].splitlines()))

    assert header(FOLDER / "channel.py") == header(SCRIPTS / "mail.py")
