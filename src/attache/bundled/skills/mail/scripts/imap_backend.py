"""Gmail, Titan and other IMAP accounts for the mail skill: IMAPClient to read and file,
smtplib to send. mail.py adds the account prefix, the previews and approvals, the caps and
the log.

Native ids: Gmail's X-GM-MSGID, which survives moves and labels; elsewhere
"<folder>/<UIDVALIDITY>/<UID>" in url-safe base64, refused once the server renumbers the
folder, so a move gives back new ids. Gmail's folders are labels, so a move there adds the
label and takes the message out of the inbox, and Gmail files sent mail itself; other servers
get a copy APPENDed to `sent_folder`.

Drafts are plain text, APPENDed to the folder flagged \\Drafts (SPECIAL-USE) with a Message-ID
of their own. send_draft rebuilds the message from what get_draft gives (to, cc, bcc, subject,
body), so nothing goes that the owner wasn't shown, and refuses a draft with other parts. Until
the SMTP server has the message, every failure is a ValueError: nothing went. Just before SMTP
the Message-ID is recorded in <home>/.mail/sent.jsonl with its number of recipients, and a
recorded Message-ID is never sent again. Those records also count `send_limit_hour`.

The password is in the OS keyring (service "attache-mail", username the account name), put
there by `mail.py login`. IMAPClient, keyring and getpass are imported inside functions, and
tests pass `client=`: an IMAPClient stand-in, already signed in, whose `smtp()` returns a
signed-in SMTP stand-in.
"""

from __future__ import annotations

import base64
import contextlib
import email
import html
import imaplib
import json
import re
import smtplib
import ssl
from datetime import UTC, datetime, timedelta
from email import policy
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid, parseaddr
from pathlib import Path
from typing import Any

SERVICE = "attache-mail"
PARTIAL = b"BODY.PEEK[]<0.32768>"  # enough of a listed message for its headers and a preview
NOT_IN_V1 = "not in v1 for IMAP accounts: calendar and tasks are Outlook only"
GMAIL_FIX = "make a new app password (Google cancels them when the Google password changes)"
TITAN_FIX = "for Titan, switch on 'Enable Titan on Other Apps' in webmail, or use an app password"
PARTS = "this draft has parts Attaché can't show you; send it from your mail app"
SEEN, FLAGGED, DRAFT = rb"\Seen", rb"\Flagged", rb"\Draft"
Json = dict[str, Any]


def host_port(text: str, port: int) -> tuple[str, int]:
    """ "imap.gmail.com:993" -> ("imap.gmail.com", 993); the port is optional."""
    host, sep, given = text.rpartition(":")
    return (host, int(given)) if sep else (text, port)


def encode_id(folder: str, validity: int, uid: int) -> str:
    return base64.urlsafe_b64encode(f"{folder}/{validity}/{uid}".encode()).decode().rstrip("=")


def decode_id(native: str) -> tuple[str, int, int]:
    try:
        text = base64.urlsafe_b64decode(native + "=" * (-len(native) % 4)).decode()
        folder, validity, uid = text.rsplit("/", 2)  # a folder name can hold "/"
        return folder, int(validity), int(uid)
    except ValueError:  # bad base64 and bad UTF-8 are ValueErrors too
        raise LookupError(f"{native!r} is not a message id of this account") from None


def strip_html(markup: str) -> str:
    """An HTML body as text to read: no scripts, styles or tags."""
    markup = re.sub(r"(?is)<(script|style)\b.*?</\1\s*>|<!--.*?-->", " ", markup)
    markup = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d|table)\s*>", "\n", markup)
    markup = re.sub(r"(?i)</t[dh]\s*>", " ", markup)  # table cells; other tags just go
    text = re.sub(r"[ \t\xa0]+", " ", html.unescape(re.sub(r"<[^>]*>", "", markup)))
    return re.sub(r"\s*\n\s*", "\n", text).strip()


def text_of(msg: EmailMessage) -> str:
    """The message's text: the plain part, or else the HTML part without the markup."""
    part = msg.get_body(("plain", "html"))
    if part is None:
        return ""
    try:
        text = part.get_content()
    except (LookupError, ValueError):  # an unknown or wrong charset
        text = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
    text = text.replace("\r\n", "\n")  # mail on the wire has CRLF line ends
    return strip_html(text) if part.get_content_subtype() == "html" else text


def addresses(msg: EmailMessage, *headers: str) -> list[str]:
    values = [str(value) for header in headers for value in msg.get_all(header, [])]
    return [address for _, address in getaddresses(values) if address]


def parse(row: Json) -> EmailMessage:
    """The message in a fetch row: BODY[], a partial BODY[]<0> or BODY[HEADER...]."""
    raw = next(value for key, value in row.items() if key.startswith(b"BODY["))
    return email.message_from_bytes(raw or b"", policy=policy.default)


def compose(sender: str, fields: Json, headers: dict[str, str]) -> EmailMessage:
    """A plain-text message of exactly these fields; `headers` adds the Message-ID and the
    threading. Bcc is never a header: only SMTP is told."""
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(fields["to"])
    if fields["cc"]:
        msg["Cc"] = ", ".join(fields["cc"])
    msg["Subject"] = fields["subject"]
    msg["Date"] = formatdate(localtime=True)
    for name, value in headers.items():  # only those with a value
        msg[name] = value
    msg.set_content(fields["body"])
    return msg


def draft_fields(native: str, msg: EmailMessage) -> Json:
    """A draft as get_draft gives it: what the fingerprint covers, and all send_draft sends.
    read() takes the addresses, subject and body from here too."""
    return {
        "id": native,
        "to": addresses(msg, "to"),
        "cc": addresses(msg, "cc"),
        "bcc": addresses(msg, "bcc"),
        "subject": str(msg.get("subject", "")),
        "body": text_of(msg),
        "attachments": [],  # not in v1 for IMAP: a draft with any is refused
    }


class Backend:
    guards_resends = True  # sent.jsonl: what went once is never sent again, so mail.py may retry

    def __init__(self, account: str, settings: Json, home: Path, client: Any = None) -> None:
        self.account, self.settings, self.home = account, settings, home
        self.records = home / ".mail" / "sent.jsonl"  # a line per message given to SMTP
        self.user = str(settings.get("user") or "")
        self.gmail = settings.get("kind") == "gmail"
        self._imap = client
        self._smtp = getattr(client, "smtp", None) or self._smtp_session

    # signing in

    def _password(self) -> str:
        import keyring

        if password := keyring.get_password(SERVICE, self.account):
            return password
        again = f"attache skill mail login {self.account}"
        raise LookupError(f"no password stored for {self.account}: the owner runs `{again}`")

    def _connect(self, password: str | None = None) -> Any:
        from imapclient import IMAPClient

        host, port = host_port(self.settings["imap"], 993)
        client = IMAPClient(host, port=port, ssl=True, timeout=30)
        client.normalise_times = False  # INTERNALDATE keeps its zone
        client.login(self.user, password or self._password())
        return client

    def _smtp_session(self) -> smtplib.SMTP:
        """Signed in to send, over SMTPS (port 465), as Gmail and Titan use. Without a context
        smtplib wouldn't check the server's certificate."""
        host, port = host_port(self.settings["smtp"], 465)
        session = smtplib.SMTP_SSL(host, port, timeout=30, context=ssl.create_default_context())
        session.login(self.user, self._password())
        return session

    @property
    def imap(self) -> Any:
        if self._imap is None:
            self._imap = self._connect()
        return self._imap

    def doctor(self) -> tuple[bool, str]:
        try:
            status = self.imap.folder_status("INBOX", [b"MESSAGES", b"UNSEEN"])
            self._smtp().quit()  # signs in to send, and sends nothing
        except LookupError as error:  # no password stored
            return False, str(error)
        except (imaplib.IMAP4.error, smtplib.SMTPAuthenticationError):
            fix = GMAIL_FIX if self.gmail else TITAN_FIX
            return False, f"{self.settings['imap']} refused the password: {fix}; then log in again"
        except OSError as error:
            return False, f"can't reach the server: {error}; check imap and smtp in accounts.toml"
        unread, total = status[b"UNSEEN"], status[b"MESSAGES"]
        return True, f"signed in as {self.user}: {unread} unread of {total} in INBOX"

    def login(self) -> None:
        import getpass

        import keyring

        kind = "app password" if self.gmail else "password"
        password = getpass.getpass(f"{self.account} {kind} for {self.user} (not shown): ").strip()
        if not password:
            raise ValueError("no password given; nothing stored")
        keyring.set_password(SERVICE, self.account, password)  # `doctor` then checks it

    # finding messages

    def _special(self, flag: bytes, fallback: str) -> str:
        """The folder with this SPECIAL-USE flag, such as \\Drafts."""
        for flags, _, name in self.imap.list_folders():
            if flag.lower() in {each.lower() for each in flags}:
                return name
        return fallback

    def _select(self, folder: str, readonly: bool = True) -> int:
        """Select the folder; its UIDVALIDITY."""
        return int(self.imap.select_folder(folder, readonly=readonly)[b"UIDVALIDITY"])

    def _fetch(self, uids: list[int], *items: bytes) -> dict[int, Json]:
        return self.imap.fetch(uids, [*items, *([b"X-GM-MSGID"] if self.gmail else [])])

    def _native(self, folder: str, validity: int, uid: int, row: Json) -> str:
        return str(row[b"X-GM-MSGID"]) if self.gmail else encode_id(folder, validity, uid)

    def _locate(self, native: str, readonly: bool, folder: str = "") -> tuple[str, int]:
        """Select the folder holding this message: (folder, uid), or LookupError if it isn't
        there. A Gmail id is looked up in `folder`, All Mail unless given."""
        if self.gmail:
            folder = folder or self._special(rb"\All", "[Gmail]/All Mail")
            self._select(folder, readonly)
            uids = self.imap.search(["X-GM-MSGID", int(native)]) if native.isdigit() else []
        else:
            folder, validity, uid = decode_id(native)
            if self._select(folder, readonly) != validity:
                raise LookupError(f"the server renumbered {folder}: list it again")
            uids = list(self.imap.fetch([uid], [b"FLAGS"]))  # IMAP ignores a UID not there
        if not uids:
            raise LookupError("no such message (it may have moved)")
        return folder, uids[0]

    def _message(self, native: str, *items: bytes, folder: str = "") -> tuple[str, int, Json]:
        """One message's folder (now selected), uid and fetched items."""
        name, uid = self._locate(native, True, folder)
        return name, uid, self._fetch([uid], *items)[uid]

    def _find(self, folder: str, message_id: str) -> str | None:
        """The native id of the message in `folder` with this Message-ID, if there is one."""
        validity = self._select(folder)
        uids = self.imap.search(["HEADER", "Message-ID", message_id])
        if not uids:
            return None
        uid = max(uids)
        return self._native(folder, validity, uid, self._fetch([uid], b"FLAGS")[uid])

    # reading

    def folders(self) -> list[Json]:
        found = []
        for flags, delimiter, name in self.imap.list_folders():
            if {rb"\noselect", rb"\nonexistent"} & {each.lower() for each in flags}:
                continue
            status = self.imap.folder_status(name, [b"MESSAGES", b"UNSEEN"])
            leaf = name.rsplit(delimiter.decode(), 1)[-1] if delimiter else name
            unread, total = status[b"UNSEEN"], status[b"MESSAGES"]
            found.append({"folder": leaf, "path": name, "unread": unread, "total": total})
        return found

    def _summary(self, native: str, folder: str, row: Json, msg: EmailMessage) -> Json:
        flags = {each.lower() for each in row[b"FLAGS"]}
        name, address = parseaddr(str(msg.get("from", "")))
        return {
            "id": native,
            "folder": folder,
            "subject": str(msg.get("subject", "")),
            "from": {"name": name, "smtp": address},
            "received_utc": row[b"INTERNALDATE"].astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "preview": text_of(msg),  # mail.py flattens and caps it
            "unread": SEEN.lower() not in flags,
            "flagged": FLAGGED.lower() in flags,
        }

    def list(
        self,
        folder: str | None,
        unread: bool,
        limit: int,
        since: datetime | None,
        query: str | None,
    ) -> list[Json]:
        folder = folder or "INBOX"
        validity = self._select(folder)
        criteria: list[Any] = ["UNSEEN"] if unread else []
        if since:  # SINCE takes a date in the server's zone: a day early, then the exact time
            criteria += ["SINCE", since.date() - timedelta(days=1)]
        if query:
            criteria += ["X-GM-RAW", query] if self.gmail else ["TEXT", query]
        uids = sorted(self.imap.search(criteria or ["ALL"], charset="UTF-8" if query else None))
        uids = uids[-limit:]
        if since and uids:  # the exact time, before the bodies: the email channel polls often
            dates = self.imap.fetch(uids, [b"INTERNALDATE"])
            uids = [uid for uid, row in dates.items() if row[b"INTERNALDATE"] >= since]
        rows = self._fetch(uids, b"FLAGS", b"INTERNALDATE", PARTIAL) if uids else {}
        return [  # mail.py puts them newest first, across the accounts
            self._summary(self._native(folder, validity, uid, row), folder, row, parse(row))
            for uid, row in rows.items()
        ]

    def read(self, native: str, max_chars: int) -> Json:
        folder, _, row = self._message(native, b"FLAGS", b"INTERNALDATE", b"BODY.PEEK[]")
        msg = parse(row)
        attachments = [
            {"name": part.get_filename() or "", "size": len(part.get_payload(decode=True) or b"")}
            for part in msg.iter_attachments()
        ]
        message = self._summary(native, folder, row, msg) | draft_fields(native, msg)
        return message | {"attachments": attachments}  # mail.py caps the body at max_chars

    # filing

    def move(self, natives: list[str], folder: str) -> list[str]:
        """Move messages; their ids afterwards. Outside Gmail a moved message is found again
        by its Message-ID, since IMAPClient doesn't hand back MOVE's COPYUID."""
        moved = []
        for native in natives:
            _, uid = self._locate(native, readonly=False)
            if self.gmail:  # a label; INBOX is the \Inbox label itself
                label = "\\Inbox" if folder.upper() == "INBOX" else folder
                self.imap.add_gmail_labels([uid], [label])
                if label != "\\Inbox":
                    self.imap.remove_gmail_labels([uid], ["\\Inbox"])
                moved.append(native)
                continue
            head = self._fetch([uid], b"BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]")[uid]
            message_id = parse(head).get("message-id")
            self.imap.move([uid], folder)
            new = self._find(folder, str(message_id)) if message_id else None
            if new is None:
                raise LookupError(f"moved to {folder}, but can't find it there: list it")
            moved.append(new)
        return moved

    def _flags(self, natives: list[str], flag: bytes, on: bool) -> list[str]:
        for native in natives:
            _, uid = self._locate(native, readonly=False)
            (self.imap.add_flags if on else self.imap.remove_flags)([uid], [flag])
        return natives

    def flag(self, natives: list[str], on: bool) -> list[str]:
        return self._flags(natives, FLAGGED, on)

    def mark_read(self, natives: list[str], on: bool) -> list[str]:
        return self._flags(natives, SEEN, on)

    # drafts and sending

    def draft(self, to, cc, subject, body, reply_to: str | None, reply_all: bool) -> Json:
        """Save a new message, or a reply to reply_to (cc and subject then come from it, and
        `to` unless it is given, as the email channel gives the From address so that Reply-To
        is never used), in Drafts. A reply holds only the body given, without the original
        quoted."""
        threading = {}
        if reply_to:
            original = parse(self._message(reply_to, b"BODY.PEEK[HEADER]")[2])
            to = to or addresses(original, "reply-to") or addresses(original, "from")
            others = addresses(original, "to", "cc") if reply_all else []
            mine = {self.user.lower(), *(each.lower() for each in to)}
            cc = list(dict.fromkeys(each for each in others if each.lower() not in mine))
            subject = str(original.get("subject", ""))
            subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
            if parent := original.get("message-id"):
                references = f"{original.get('references', '')} {parent}".strip()
                threading = {"In-Reply-To": str(parent), "References": references}
        message_id = make_msgid(domain=self.user.rpartition("@")[2] or "localhost")
        fields = {"to": to, "cc": cc or [], "subject": subject, "body": body}
        msg = compose(self.user, fields, {"Message-ID": message_id, **threading})
        drafts = self._special(rb"\Drafts", "Drafts")
        self.imap.append(drafts, msg.as_bytes(policy=policy.SMTP), flags=[DRAFT, SEEN])
        native = self._find(drafts, message_id)
        if native is None:
            raise LookupError(f"the draft was saved in {drafts} but isn't there")
        return self.get_draft(native)

    def _draft(self, native: str) -> tuple[str, int, EmailMessage]:
        """A plain-text message in Drafts: its folder, uid and contents. Only those are sent."""
        drafts = self._special(rb"\Drafts", "Drafts")
        folder, uid, row = self._message(native, b"BODY.PEEK[]", folder=drafts)
        msg = parse(row)
        if folder != drafts or not msg.get("message-id"):
            raise ValueError(f"{native} is not a draft saved by draft-new or draft-reply")
        if msg.get_content_type() != "text/plain":
            raise ValueError(PARTS)
        return folder, uid, msg

    def get_draft(self, native: str) -> Json:
        return draft_fields(native, self._draft(native)[2])

    def headers(self, native: str) -> str:
        """The message's header block as received, top first (the email channel's checks)."""
        row = self._message(native, b"BODY.PEEK[HEADER]")[2]
        raw = next(value for key, value in row.items() if key.startswith(b"BODY["))
        return (raw or b"").decode("utf-8", "replace")

    def _may_send(self, message_id: str, recipients: int) -> None:
        """Refuse a Message-ID that went to SMTP before, by <home>/.mail/sent.jsonl, and a
        send past send_limit_hour."""
        hour_ago = (datetime.now(UTC) - timedelta(hours=1)).isoformat(timespec="seconds")
        records, recent = [], 0
        log = self.records
        for line in log.read_text(encoding="utf-8").splitlines() if log.exists() else []:
            with contextlib.suppress(ValueError):  # a line cut short
                records.append(json.loads(line))
        for record in records:
            if record.get("message_id") == message_id:
                raise ValueError(f"already sent at {record.get('ts')}: check Sent")
            if record.get("account") == self.account and record.get("ts", "") >= hour_ago:
                recent += record.get("recipients", 0)
        limit = self.settings.get("send_limit_hour")
        if limit and recent + recipients > limit:
            raise ValueError(f"{self.account} sends to {limit} people an hour; {recent} so far")

    def _record(self, message_id: str, recipients: int) -> None:
        self.records.parent.mkdir(parents=True, exist_ok=True)
        when = datetime.now(UTC).isoformat(timespec="seconds")
        record = {"ts": when, "account": self.account, "message_id": message_id}
        with self.records.open("a", encoding="utf-8") as log:
            log.write(json.dumps({**record, "recipients": recipients}) + "\n")

    def send_draft(self, native: str) -> str:
        """Send what get_draft gives, file it in Sent and remove the draft: see the module
        docstring for what a failure means."""
        try:
            folder, uid, msg = self._draft(native)
            draft = draft_fields(native, msg)
            recipients = draft["to"] + draft["cc"] + draft["bcc"]
            keep = ("Message-ID", "In-Reply-To", "References")
            headers = {name: str(msg[name]) for name in keep if msg[name]}
            outgoing = compose(self.user, draft, headers)
            self._may_send(headers["Message-ID"], len(recipients))
            session = self._smtp()
            self._record(headers["Message-ID"], len(recipients))
        except Exception as error:  # the server has nothing yet
            raise ValueError(str(error) or type(error).__name__) from error
        with session:
            session.send_message(outgoing, to_addrs=recipients)
        sent_folder = self.settings.get("sent_folder") or self._special(rb"\Sent", "Sent")
        if not self.gmail:  # Gmail files sent mail itself
            self.imap.append(sent_folder, outgoing.as_bytes(policy=policy.SMTP), flags=[SEEN])
        self._select(folder, readonly=False)
        self.imap.delete_messages([uid])
        if self.imap.has_capability("UIDPLUS"):  # else the server expunges it in its own time
            self.imap.expunge([uid])
        return self._find(sent_folder, headers["Message-ID"]) or ""

    def agenda(self, *args: Any) -> Any:
        """Outlook only, like tasks, respond and create_event."""
        raise NotImplementedError(NOT_IN_V1)

    tasks = respond = create_event = agenda
