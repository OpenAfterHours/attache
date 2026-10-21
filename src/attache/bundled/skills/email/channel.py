# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "imapclient>=3.0",
#     "keyring>=25",
#     "msal>=1.31",
#     "msal-extensions>=1.2",
#     "pywin32>=306; sys_platform == 'win32'",
# ]
# ///
"""Attaché's email channel: a sidecar (docs/channels.md) that watches one inbox of the mail
skill, so the people in attache.toml can write to Attaché by email. It answers in the thread.

    uv run channel.py    run the sidecar (Attaché starts it)

[channels.email] reaches it as environment variables: ATTACHE_ACCOUNT, the section of
skills/mail/accounts.toml whose inbox it watches and replies from; ATTACHE_EVERY, the seconds
between checks (60); ATTACHE_MENTION, what a message must contain ("@attache"; case and accents
don't matter); ATTACHE_SIGNATURE, the line under each reply (by default it says to start an
answer with the mention); ATTACHE_AUTHSERV_ID, the authserv-id the topmost
Authentication-Results must have: its first field, before the first ";", must be exactly this
name, case aside (optional: "mx.google.com" for Gmail). It imports the
mail skill's mail.py from the home folder and uses its backends, so signing in, `login` and
`doctor` are the mail skill's. Without a usable account, or with a mail skill older than this
channel, it says why on stderr and waits for `stop`.

What is passed on. A new inbox message becomes a `message` event only when all of these hold:
- it has one From address, and that isn't the account's own (`user` in accounts.toml);
- it isn't automatic: no Auto-Submitted (other than "no"), List-Id or List-Unsubscribe, and no
  Precedence of bulk, list or junk;
- the new part of its body, above the quoted history, mentions the assistant and doesn't hold
  the signature (a reply of Attaché's own, or another assistant's: no ping-pong);
- its sender is authenticated (authenticated()). Only this skip is logged, by id: the others
  are mail that was never for Attaché, and the log shows in the owner's terminal.
The text is "Subject: S", a blank line and the new part, at most 4,000 characters in all.
Whether the sender is someone Attaché answers is the core's call, by address.

Replies. `send` and `ask` answer the message named by `reply_to` when it came from that same
address, else that address's latest message passed on (reminders and background jobs have no
`reply_to`). The reply goes in its thread to that address alone: never to Reply-To, never Cc
or reply-all, and never a new email to an address with no message. If the message has moved
(Outlook classic's ids change then), it goes as a new email to that same address, "Re:" its
subject. The draft is checked before it is sent; IMAP replies say Auto-Submitted. This is the
core's own reply path, not the mail skill's approval-gated send-draft. Sends are audited in
logs/mail.jsonl, ids only.

Outlook's COM objects belong to the thread that made them, so every mail call happens on the
main thread; a reader thread only queues Attaché's commands. $ATTACHE_FILES/.state.json keeps
the watermark, the ids already looked at and the messages passed on (the last 500 of each), so
a restart answers nothing twice. The first start begins now: old mail is never answered. A
check looks at 50 messages at most, oldest first, and the watermark waits for the rest.
"""

from __future__ import annotations

import importlib
import json
import os
import queue
import re
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.message import Message
from email.parser import HeaderParser
from email.utils import getaddresses
from pathlib import Path
from types import SimpleNamespace
from typing import Any

HERE = Path(__file__).resolve().parent  # <home>/skills/email
NAME = "Attaché"
SIGNATURE = "Sent by Attaché, an assistant. To reply to me, start your message with {}."
COMMANDS = ("send", "ask", "answered", "stop")
MAX_TEXT = 4000  # characters passed on: the subject line and the new part of the body
KEEP = 500  # ids remembered as looked at, and messages remembered as passed on
LIMIT = 50  # messages looked at in one check, oldest first; the rest wait for the next
MOST = 1000  # messages listed in one check
TRIES = 3  # failed checks of one message before it is given up
OVERLAP = timedelta(minutes=5)  # a check looks back this far from the newest mail it saw
HOUR = 3600.0
NOT_EXCHANGE = 3  # OlExchangeStoreType olNotExchange: an Outlook store Exchange doesn't fill
LONG_AGO = "2000-01-01T00:00:00+00:00"
LOCK = threading.Lock()  # one writer at a time on stdout
Json = dict[str, Any]
QUOTED = re.compile(  # the first line of quoted history; the new part is everything above it
    r"""^(?: On\b[^\n]*(?:\n[^\n]*)?\bwrote:[ \t]*$  # On Tue 20 Oct, Dan <dan@x.com> wrote:
         | >                                       # a quoted line
         | -+[ ]?Original[ ]Message[ ]?-+          # -----Original Message-----
         | (?:_{8,}\n)?From:[^\n]*\n(?:Sent|Date): # Outlook's From: and Sent: lines
       )""",
    re.M | re.I | re.X,
)


def emit(event: Json) -> None:
    """Write one protocol line to Attaché."""
    with LOCK:
        print(json.dumps(event, ensure_ascii=False), flush=True)


def log(level: str, text: str) -> None:
    """A line for Attaché's log."""
    emit({"type": "log", "level": level, "text": text})


# Reading a message: pure functions, so tests need no mail account.


def fold(text: str) -> str:
    """Casefolded, without accents, spaces single: "@Attaché" becomes "@attache"."""
    letters = unicodedata.normalize("NFKD", text.casefold())
    return " ".join("".join(c for c in letters if not unicodedata.combining(c)).split())


def mentions(text: str, mention: str) -> bool:
    """Whether the text holds the mention as a word, not inside an address (dan@attache.org)."""
    return re.search(rf"(?<!\w){re.escape(fold(mention))}(?!\w)", fold(text)) is not None


def new_part(body: str) -> str:
    """The body above its quoted history (QUOTED)."""
    body = body.replace("\r\n", "\n")
    found = QUOTED.search(body)
    return (body[: found.start()] if found else body).strip()


def received(item: Json) -> datetime:
    """When an inbox message arrived; one without a time counts as long ago."""
    return datetime.fromisoformat(item.get("received_utc") or LONG_AGO)


def uncommented(value: str) -> str:
    """A header value without its (comments), which may repeat the sender's own words."""
    kept, depth, escaped = [], 0, False
    for char in value:
        if escaped:
            escaped = False
        elif char == "\\" and depth:
            escaped = True
        elif char in "()":
            depth = depth + 1 if char == "(" else max(depth - 1, 0)
        elif not depth:
            kept.append(char)
    return "".join(kept)


def dmarc_pass(results: str, domain: str) -> bool:
    """Whether an Authentication-Results value has one DMARC result, a pass for header.from =
    domain. A second result, or a quoted string, may be the sender's words: either fails."""
    clauses = [clause.lower().split() for clause in uncommented(results).split(";")]
    dmarc = [words for words in clauses if words and words[0].startswith("dmarc=")]
    if '"' in results or len(dmarc) != 1:
        return False
    return dmarc[0][0] == "dmarc=pass" and f"header.from={domain.lower()}" in dmarc[0]


def authenticated(head: Message, address: str, exchange: bool, authserv: str = "") -> bool:
    """Whether the From address is the real sender's. Only the topmost Authentication-Results
    counts: the receiving server prepends its own, and any below it may be the sender's. With
    `authserv`, its first field (the authserv-id, before the first ";", comments dropped) must
    also be exactly that name, case aside. On Exchange,
    X-MS-Exchange-Organization-AuthAs: Internal counts too, as Exchange strips that header from
    mail arriving from outside the organisation; every copy must say so. That means Exchange
    took the message as internal, not that a person is proven. Anything else fails."""
    top = (head.get_all("Authentication-Results") or [""])[0]
    server = uncommented(top).split(";")[0].strip().lower()
    if dmarc_pass(top, address.rpartition("@")[2]) and server == (authserv.lower() or server):
        return True
    auth_as = {v.strip().lower() for v in head.get_all("X-MS-Exchange-Organization-AuthAs") or []}
    return exchange and auth_as == {"internal"}


def from_address(head: Message) -> str | None:
    """The address in the message's one From header, if it names exactly one, in lower case."""
    froms = head.get_all("From") or []
    found = getaddresses(froms) if len(froms) == 1 else []
    return found[0][1].lower() if len(found) == 1 and "@" in found[0][1] else None


def automatic(head: Message) -> bool:
    """Whether a machine sent it: an auto-reply, a mailing list or bulk mail."""
    auto = uncommented(head.get("Auto-Submitted", "no")).strip().lower()
    precedence = uncommented(head.get("Precedence", "")).strip().lower()
    listed = "List-Id" in head or "List-Unsubscribe" in head
    return auto != "no" or listed or precedence in ("bulk", "list", "junk")


def on_exchange(kind: str, backend: Any) -> bool:
    """Whether Exchange delivers this account's mail, so an AuthAs header is Exchange's own:
    always through Graph; through COM when Outlook's default store is an Exchange mailbox."""
    if kind == "com":
        return backend.session.DefaultStore.ExchangeStoreType != NOT_EXCHANGE
    return kind == "graph"


# The inbox, its state and the replies.


@dataclass
class State:
    """What a restart keeps, in $ATTACHE_FILES/.state.json."""

    path: Path
    since: str = field(default_factory=lambda: datetime.now(UTC).isoformat())  # new: from now
    done: list[str] = field(default_factory=list)  # ids looked at, oldest first
    admitted: dict[str, list[str]] = field(default_factory=dict)  # id -> [address, subject]

    @classmethod
    def load(cls, path: Path) -> State:
        """The saved state; a new one, from now, if there is none or it can't be read."""
        if not path.is_file():
            return cls(path)
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            since = datetime.fromisoformat(saved["since"]).isoformat()
            return cls(path, since, list(saved["done"]), dict(saved.get("admitted") or {}))
        except (OSError, ValueError, KeyError, TypeError) as error:
            log("warning", f"{path.name} couldn't be read ({error}); starting from now")
            return cls(path)

    def latest(self, address: str) -> str | None:
        """The latest message passed on from this address."""
        return next((n for n, (a, _) in reversed(self.admitted.items()) if a == address), None)

    def save(self) -> None:
        """Write it whole, then rename it into place, so a crash leaves the old or the new."""
        del self.done[:-KEEP]
        for old in list(self.admitted)[:-KEEP]:
            del self.admitted[old]
        saving = self.path.with_name(self.path.name + ".new")
        state = {"since": self.since, "done": self.done, "admitted": self.admitted}
        saving.write_text(json.dumps(state), encoding="utf-8")
        os.replace(saving, self.path)


@dataclass
class Inbox:
    """The account this channel watches and answers from."""

    skill: Any  # the mail skill's mail.py module
    mail: Any  # its Mail(home)
    account: str
    state: State
    own: set[str] = field(default_factory=set)  # the account's own addresses
    mention: str = "@attache"
    signature: str = SIGNATURE.format("@attache")
    authserv: str = ""  # the server the topmost Authentication-Results must name, if set
    tries: dict[str, int] = field(default_factory=dict)  # id -> checks of it that failed

    def check(self) -> None:
        """Look at up to LIMIT new messages, oldest first, passing on those for Attaché. Each
        is marked done, and saved, before Attaché hears of it, so none is passed on twice. The
        watermark never passes one left for the next check, nor one whose check failed: that
        is tried again next time, and after TRIES failed checks given up, with a warning, so
        the watermark can't stay pinned while `done` (the last 500) rolls over."""
        backend = self.mail.backend(self.account)
        exchange = on_exchange(self.mail.settings(self.account)["backend"], backend)
        since = datetime.fromisoformat(self.state.since)
        items = sorted(backend.list(None, False, MOST, since, None), key=received)
        if len(items) >= MOST:
            log("warning", f"{MOST} new messages at once: older ones may be missed")
        waiting = [item for item in items if item["id"] not in self.state.done]
        failed = []  # this time; the watermark waits for them
        for item in waiting[:LIMIT]:
            try:
                found = self.consider(backend, item["id"], exchange)
            except Exception as error:
                tries = self.tries[item["id"]] = self.tries.get(item["id"], 0) + 1
                if tries < TRIES:
                    log("warning", f"couldn't check message {item['id']}: {error}")
                    failed.append(item)
                    continue
                log("warning", f"gave up on message {item['id']} after {TRIES} failed checks")
                found = None
            self.state.done.append(item["id"])
            if isinstance(found, str):
                log("info", f"skipped message {item['id']}: {found}")
            elif found:
                self.state.save()  # before Attaché hears of it: never passed on twice
                emit(found)
        left = [received(i) for i in failed + waiting[LIMIT:]]
        marks = [received(i) - OVERLAP for i in items[-1:]] + left
        self.state.since = max(since, min(marks, default=since)).isoformat()
        self.state.save()

    def consider(self, backend: Any, native: str, exchange: bool) -> Json | str | None:
        """The `message` event for one inbox message; else why it was skipped (an unproven
        sender), or None for mail that was never for Attaché: the account's own, a machine's,
        one without the mention in its new part, or one holding the signature."""
        raw = re.sub(r"\A(?:[^:\n]*\n)+", "", backend.headers(native))  # Exchange's title line
        head = HeaderParser().parsestr(raw)
        address = from_address(head)
        if address is None or address in self.own or automatic(head):
            return None
        message = backend.read(native, MAX_TEXT)
        new = new_part(message.get("body") or "")
        if not mentions(new, self.mention) or fold(self.signature) in fold(new):
            return None
        if not authenticated(head, address, exchange, self.authserv):
            return "sender not authenticated"
        subject = message.get("subject") or ""
        self.state.admitted[native] = [address, subject]
        return {
            "type": "message",
            "chat_id": address,
            "sender_id": address,
            "sender_name": (message.get("from") or {}).get("name") or "",
            "text": f"Subject: {subject}\n\n{new}"[:MAX_TEXT],
            "is_group": False,
            "mentioned": True,
            "message_id": native,
            "files": [],
        }

    def answer(self, command: Json) -> None:
        """A `send` or an `ask`, as a reply to the message it answers (`reply_to`, if that came
        from the same address), else to that address's latest message."""
        address, text = command["chat_id"], command.get("text") or ""
        if command["type"] == "ask":
            numbered = ", ".join(f"{n} for {each}" for n, each in enumerate(command["options"], 1))
            text = f"{text}\n\nReply {numbered}."
        if command.get("files"):
            log("info", f"{len(command['files'])} file(s) not sent: email replies are text only")
        native = command.get("reply_to")
        if self.state.admitted.get(native, [""])[0] != address:  # a reminder or a background job
            native = self.state.latest(address)
        if native is None:
            log("warning", "nothing from that address to reply to, and never a new email: dropped")
            return
        self.reply(address, native, f"{text}\n\n{self.signature}")

    def reply(self, address: str, native: str, body: str) -> None:
        """Reply to message `native`, to `address` alone, and audit it. If the message can't be
        found any more (moved: Outlook's ids change), a new email to `address`, "Re:" its
        subject, with the same check."""
        item = f"{self.account}:{native}"
        given = SimpleNamespace(command="email-reply", account=self.account, id=item)
        try:
            backend = self.mail.backend(self.account)
            backend.extra = {"Auto-Submitted": "auto-replied"}  # IMAP marks its replies so
            try:
                draft = backend.draft([address], [], None, body, native, False)
            except (LookupError, ValueError):  # not where it was, or gone
                subject = self.state.admitted[native][1]
                subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
                draft = backend.draft([address], [], subject, body, None, False)
            if [to.lower() for to in draft["to"]] != [address] or draft["cc"] or draft["bcc"]:
                raise ValueError("the draft isn't to the sender alone, so it stays in Drafts")
            sent = backend.send_draft(draft["id"])
            log("info", f"replied to message {native}")
            result = {"ok": True, "id": f"{self.account}:{sent}"}
        except Exception as error:
            self.mail.opened.clear()  # sign in afresh next time: a connection may have dropped
            log("error", f"couldn't reply to message {native}: {error}")
            result = {"ok": False, "error": str(error) or type(error).__name__}
        self.skill.audit(self.mail, given, result)


def open_inbox(home: Path) -> Inbox:
    """The Inbox [channels.email] names, with the mail skill's mail.py imported from home. A
    mail skill from before the channel (a home kept from 0.0.2) is refused: its replies would
    follow Reply-To."""
    account = os.environ.get("ATTACHE_ACCOUNT") or ""
    if not account:
        raise ValueError('[channels.email] needs account = "<a section of accounts.toml>"')
    scripts = home / "skills" / "mail" / "scripts"
    if not (scripts / "mail.py").is_file():
        raise FileNotFoundError(f"the mail skill isn't installed: no {scripts / 'mail.py'}")
    sys.path.insert(0, str(scripts))  # where mail.py finds its backends too
    import mail as skill

    mail = skill.Mail(home)
    settings = mail.settings(account)  # no such account: MailError
    if not hasattr(importlib.import_module(f"{settings['backend']}_backend").Backend, "headers"):
        old = f"the mail skill in {scripts.parent} is older than this channel"
        raise RuntimeError(f"{old}: see docs/mail.md, Updating the mail skill")
    files = Path(os.environ.get("ATTACHE_FILES") or home / "files" / "inbox" / "email")
    files.mkdir(parents=True, exist_ok=True)
    state, user = State.load(files / ".state.json"), str(settings.get("user") or "").lower()
    mention = os.environ.get("ATTACHE_MENTION") or "@attache"
    signature = (os.environ.get("ATTACHE_SIGNATURE") or "").strip() or SIGNATURE.format(mention)
    authserv = os.environ.get("ATTACHE_AUTHSERV_ID") or ""
    return Inbox(skill, mail, account, state, {user} - {""}, mention, signature, authserv)


# The protocol loop.


def parse_command(line: str) -> Json | None:
    """One stdin line as a command, or None when it isn't one."""
    try:
        command = json.loads(line)
    except ValueError:
        return None
    return command if isinstance(command, dict) and command.get("type") in COMMANDS else None


def read_commands(commands: queue.Queue[Json | None]) -> None:
    """Queue each command from stdin, then None at its end: Attaché has gone."""
    for line in sys.stdin:
        if (command := parse_command(line)) is not None:
            commands.put(command)
    commands.put(None)


def serve(inbox: Inbox, commands: queue.Queue[Json | None], every: float) -> int:
    """Check the inbox every `every` seconds and carry out commands as they come, all on this
    thread, until `stop` or the end of stdin. A failed check is logged and tried again, up to
    16 times later; the hint to run doctor comes at most once an hour."""
    due, failures, hinted = 0.0, 0, -HOUR
    while True:
        try:
            command = commands.get(timeout=max(0.0, due - time.monotonic()))
        except queue.Empty:
            try:
                inbox.check()
                failures = 0
            except Exception as error:
                failures += 1
                inbox.mail.opened.clear()  # sign in afresh: a connection may have dropped
                hint = ""
                if time.monotonic() - hinted >= HOUR:
                    hint, hinted = " Check it with: attache skill mail doctor", time.monotonic()
                log("warning", f"checking {inbox.account} failed: {error}.{hint}")
            due = time.monotonic() + every * min(2**failures, 16)
            continue
        if command is None or command["type"] == "stop":
            return 0
        try:
            if command["type"] in ("send", "ask"):
                inbox.answer(command)
        except Exception as error:  # a malformed command
            log("error", f"couldn't {command['type']}: {error!r}")


def main() -> int:
    """The sidecar. When the account can't be used, it says why on stderr and waits for stop:
    Attaché would only restart it, and it can't run until the setup is fixed."""
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    sys.stderr.reconfigure(encoding="utf-8")  # Attaché passes it on: "Attaché", not cp1252
    commands: queue.Queue[Json | None] = queue.Queue()
    threading.Thread(target=read_commands, args=(commands,), daemon=True).start()
    try:
        inbox = open_inbox(Path(os.environ.get("ATTACHE_HOME") or HERE.parents[1]))
        every = max(10.0, float(os.environ.get("ATTACHE_EVERY") or 60))
    except Exception as error:
        print(f"Email: {error}. Fix it, then restart Attaché.", file=sys.stderr, flush=True)
        while (command := commands.get()) is not None and command["type"] != "stop":
            pass  # nothing can be sent until the setup is fixed
        return 2
    emit({"type": "ready", "self_id": inbox.account, "name": NAME})
    return serve(inbox, commands, every)


if __name__ == "__main__":
    sys.exit(main())
