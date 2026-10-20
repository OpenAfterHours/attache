"""Outlook classic through COM, for mail.py: pywin32 with late binding, on Windows only.

The account is Outlook's default store, the mailbox Outlook opens first. A native id is the
item's EntryID: GetItemFromID finds it in that store without the StoreID, which would make
every id three times longer. EntryIDs change when an item moves, so `move` returns the new ones.

COM is called from one thread, the one mail.py runs on, and only in the owner's own logged-in,
non-elevated session: Windows refuses COM between elevated and normal programs, and Microsoft
doesn't support Office automation from services. pywin32 is imported only inside connect(), so
this module imports anywhere; tests pass a fake Outlook.Application as `client`.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

Text = str | None
When = datetime | str

# Outlook's enumerations, which late binding doesn't load.
INBOX, CALENDAR, TASKS = 6, 9, 13  # OlDefaultFolders
MAIL_ITEM, APPOINTMENT_ITEM = 0, 1  # OlItemType, also a folder's DefaultItemType
MAIL, APPOINTMENT, MEETING_REQUEST = 43, 26, 53  # OlObjectClass
TO, CC, BCC = 1, 2, 3  # OlMailRecipientType
PLAIN = 1  # OlBodyFormat olFormatPlain
MEETING = 1  # OlMeetingStatus olMeeting: the recipients are invited
MARK_NO_DATE = 4  # OlMarkInterval: a follow-up flag without a due date
FLAGGED = 2  # OlFlagStatus olFlagMarked
RESPONSES = {"accept": 3, "tentative": 2, "decline": 4}  # OlMeetingResponse
NONE_YEAR = 4501  # Outlook writes "no date" as 1 January 4501

SMTP = "http://schemas.microsoft.com/mapi/proptag/0x5D01001F"  # PR_SENDER_SMTP_ADDRESS
FLAG = "http://schemas.microsoft.com/mapi/proptag/0x10900003"  # PR_FLAG_STATUS
PREVIEW = "urn:schemas:httpmail:textdescription"  # the body's first 255 characters
COLUMNS = ["EntryID", "Subject", "SenderName", "SenderEmailAddress", SMTP, "ReceivedTime"]
COLUMNS += [PREVIEW, "UnRead", FLAG]
SEARCHED = ["subject", "fromname", "fromemail"]  # a Table filter can't match the body by LIKE
SWITCH = r"Software\Microsoft\Office\16.0\Outlook\Preferences"  # UseNewOutlook = 1: switched

NOT_WINDOWS = 'Outlook classic (COM) works only on Windows: set backend = "graph" instead.'
ELEVATED = "Attaché runs as administrator, and Windows then refuses COM: run it unelevated."
NEW_OUTLOOK = (
    "This PC uses the new Outlook app, which has no COM. Switch back to Outlook classic (turn "
    'off the "New Outlook" toggle), or set backend = "graph" for this account in accounts.toml.'
)
NO_PYWIN32 = "pywin32 isn't installed. Run mail commands with `attache skill mail`, which adds it."
MOVED = "No such item. Outlook ids change when an item moves, so list the folder again."
NOT_DRAFT = "That isn't an unsent draft."


class UnreachableError(RuntimeError, ValueError):
    """Outlook can't be reached, so nothing was sent: a ValueError, which mail.py reads so."""


def connect() -> Any:
    """Outlook.Application: the Outlook already running, else one COM starts."""
    if sys.platform != "win32":
        raise UnreachableError(NOT_WINDOWS)
    if elevated():
        raise UnreachableError(ELEVATED)
    try:
        import win32com.client
    except ImportError:
        raise UnreachableError(NO_PYWIN32) from None
    with contextlib.suppress(Exception):  # not running yet
        return win32com.client.GetActiveObject("Outlook.Application")
    try:
        return win32com.client.Dispatch("Outlook.Application")
    except Exception as error:
        problem = NEW_OUTLOOK if new_outlook() else f"Outlook classic didn't start: {error}"
        raise UnreachableError(problem) from None


def elevated() -> bool:
    """True when this process runs as administrator."""
    import ctypes

    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def new_outlook() -> bool:
    """True when this user has switched to the new Outlook app, or it (olk.exe) is running."""
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, SWITCH) as key:
            if winreg.QueryValueEx(key, "UseNewOutlook")[0]:
                return True
    except OSError:
        pass
    command = ["tasklist", "/FI", "IMAGENAME eq olk.exe", "/NH"]
    found = subprocess.run(command, capture_output=True, encoding="utf-8", errors="replace")
    return "olk.exe" in found.stdout.lower()


def local(value: Any) -> datetime | None:
    """A COM date as an aware local time, or None for Outlook's "no date". COM dates carry no
    zone and pywin32 labels them UTC whatever they are, so only the wall clock is kept."""
    if value is None or value.year >= NONE_YEAR:
        return None
    return datetime(*value.timetuple()[:6]).astimezone()


def utc(value: Any) -> str | None:
    """A COM date as ISO 8601 UTC, like 2026-10-18T16:42:00Z."""
    moment = local(value)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if moment else None


def wall(value: When) -> datetime:
    """An ISO time or a datetime as this PC's naive local time, which Outlook expects."""
    moment = datetime.fromisoformat(value) if isinstance(value, str) else value
    return moment.astimezone().replace(tzinfo=None)


def path(folder: Any) -> str:
    """A folder's path inside its store: Inbox/Receipts for \\\\you@outlook.com\\Inbox\\Receipts."""
    return "/".join(folder.FolderPath.lstrip("\\").split("\\")[1:])


def smtp(entry: Any) -> str:
    """An address entry's SMTP address. Exchange users have an X.500 one, so ask Exchange."""
    if entry is None:
        return ""
    if entry.Type == "EX" and (user := entry.GetExchangeUser()) is not None:
        return user.PrimarySmtpAddress
    return entry.Address or ""


def addresses(item: Any, kind: int) -> list[str]:
    """The SMTP addresses of an item's To (or CC) recipients."""
    return [smtp(r.AddressEntry) or r.Address for r in item.Recipients if r.Type == kind]


def attachments(item: Any) -> list[dict]:
    """An item's attachments, inline pictures too, as {name, size}."""
    return [{"name": a.FileName, "size": a.Size} for a in item.Attachments]


def dasl(unread: bool, query: Text) -> str:
    """A Table filter in DASL, the syntax that has no locale-dependent parts."""
    parts = ['"urn:schemas:httpmail:read" = 0'] if unread else []
    if query:
        like = query.replace("'", "''")
        fields = [f"\"urn:schemas:httpmail:{field}\" LIKE '%{like}%'" for field in SEARCHED]
        parts.append(f"({' OR '.join(fields)})")
    return "@SQL=" + " AND ".join(parts) if parts else ""


class Backend:
    """One Outlook classic account, through COM."""

    def __init__(self, account: str, settings: dict, home: Path, client: Any = None) -> None:
        self.account, self.settings, self.home = account, settings, home
        self._app = client

    @property
    def app(self) -> Any:
        """Outlook.Application, connected on first use."""
        if self._app is None:
            self._app = connect()
        return self._app

    @property
    def session(self) -> Any:
        """Outlook's MAPI namespace."""
        return self.app.Session

    def doctor(self) -> tuple[bool, str]:
        """Whether Outlook classic answers, and if not, why and what to do."""
        try:
            inbox = self.session.GetDefaultFolder(INBOX)
            return True, f"Outlook classic {self.app.Version}: {inbox.UnReadItemCount} unread"
        except Exception as error:
            return False, str(error)

    def login(self) -> None:
        """Nothing to do: COM uses the Outlook already signed in on this PC."""

    def folders(self) -> list[dict]:
        """Every mail folder with its unread and total counts."""
        return [
            {
                "folder": folder.Name,
                "path": path(folder),
                "unread": folder.UnReadItemCount,
                "total": folder.Items.Count,
            }
            for folder in self._walk()
        ]

    def list(
        self, folder: Text, unread: bool, limit: int, since: datetime | None, query: Text
    ) -> list[dict]:
        """Summaries, newest first: one folder (the Inbox by default), or with a query and no
        folder, every mail folder."""
        sources = list(self._walk()) if query and not folder else [self._folder(folder)]
        after = since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if since else ""
        found: list[dict] = []
        for source in sources:
            found += self._rows(source, dasl(unread, query), limit, after)
        found.sort(key=lambda summary: summary["received_utc"] or "", reverse=True)
        return found[:limit]

    def read(self, native: str, max_chars: int) -> dict:
        """One message with its body as plain text, capped at max_chars."""
        item = self._item(native)
        body = item.Body or ""
        sender = getattr(item, "Sender", None)
        return {
            "id": item.EntryID,
            "folder": path(item.Parent),
            "subject": item.Subject or "",
            "from": {
                "name": item.SenderName or "",
                "smtp": smtp(sender) or item.SenderEmailAddress,
            },
            "received_utc": utc(item.ReceivedTime),
            "preview": body,
            "unread": bool(item.UnRead),
            "flagged": item.FlagStatus == FLAGGED,
            "to": addresses(item, TO),
            "cc": addresses(item, CC),
            "body": body[:max_chars],
            "truncated": len(body) > max_chars,
            "attachments": attachments(item),
        }

    def move(self, natives: list[str], folder: str) -> list[str]:
        """Move items, returning their new ids."""
        target = self._folder(folder)
        return [self._item(native).Move(target).EntryID for native in natives]

    def flag(self, natives: list[str], on: bool) -> list[str]:
        """Set or clear the follow-up flag."""
        for native in natives:
            item = self._item(native)
            if on:
                item.MarkAsTask(MARK_NO_DATE)
            else:
                item.ClearTaskFlag()
            item.Save()
        return natives

    def mark_read(self, natives: list[str], on: bool) -> list[str]:
        """Mark items read, or unread when on is False."""
        for native in natives:
            item = self._item(native)
            item.UnRead = not on
            item.Save()
        return natives

    def draft(
        self, to: list[str], cc: list[str], subject: str, body: str, reply_to: Text, reply_all: bool
    ) -> dict:
        """Save a new message, or a reply to reply_to, in Drafts. A reply's body is only the
        text given: the original isn't quoted, so the draft is exactly what the owner approves."""
        if reply_to:
            original = self._item(reply_to)
            mail = original.ReplyAll() if reply_all else original.Reply()
        else:
            mail = self.app.CreateItem(MAIL_ITEM)
            mail.Subject = subject
            for kind, given in ((TO, to), (CC, cc or [])):
                for each in given:
                    mail.Recipients.Add(each).Type = kind
            mail.Recipients.ResolveAll()
        mail.Body = body
        mail.Save()
        return self.get_draft(mail.EntryID)

    def get_draft(self, native: str) -> dict:
        """A draft as Outlook holds it now, with everything that would be sent: a BCC or an
        attachment added in Outlook too."""
        item = self._draft(native)
        return {
            "id": item.EntryID,
            "to": addresses(item, TO),
            "cc": addresses(item, CC),
            "bcc": addresses(item, BCC),
            "subject": item.Subject or "",
            "body": item.Body or "",
            "attachments": attachments(item),
        }

    def send_draft(self, native: str) -> str:
        """Send a draft as plain text, so it carries only the body the owner saw and not an HTML
        part edited in elsewhere. Outlook files it in Sent Items under a new id, which COM
        doesn't give back, so this returns the draft's id. A ValueError means nothing was sent."""
        item = self._draft(native)
        item.BodyFormat = PLAIN
        item.Send()
        return native

    def agenda(self, days: int) -> list[dict]:
        """Calendar events from now until `days` ahead, with recurring meetings expanded."""
        start = datetime.now().astimezone().replace(second=0, microsecond=0)
        end = start + timedelta(days=days)
        items = self.session.GetDefaultFolder(CALENDAR).Items
        items.Sort("[Start]")
        items.IncludeRecurrences = True
        # Restrict reads dates in this PC's format; ISO is the one most locales parse, and the
        # loop checks each event again.
        found = items.Restrict(
            f"[Start] < '{end:%Y-%m-%d %H:%M}' AND [End] > '{start:%Y-%m-%d %H:%M}'"
        )
        events: list[dict] = []
        item = found.GetFirst()
        while item is not None:
            begins, ends = local(item.Start), local(item.End)
            if begins and begins >= end:
                break  # sorted by start, so nothing later is in the window
            if begins and ends and ends > start:
                events.append(self._event(item, begins, ends))
            item = found.GetNext()
        return events

    def tasks(self) -> list[dict]:
        """Open Outlook tasks, soonest due first."""
        items = self.session.GetDefaultFolder(TASKS).Items.Restrict("[Complete] = False")
        items.Sort("[DueDate]")
        return [
            {
                "id": task.EntryID,
                "subject": task.Subject or "",
                "due": due.date().isoformat() if (due := local(task.DueDate)) else None,
                "done": bool(task.Complete),
            }
            for task in items
        ]

    def respond(self, native: str, response: str, body: Text) -> None:
        """Accept, tentatively accept or decline a meeting: an invitation in the mail or an
        event from the agenda (an occurrence answers for its whole series)."""
        item = self._item(native)
        if item.Class == MEETING_REQUEST:
            item = item.GetAssociatedAppointment(True)
        if getattr(item, "Class", None) != APPOINTMENT:  # None: the meeting has gone
            raise ValueError("That isn't a meeting invitation.")
        reply = item.Respond(RESPONSES[response], True)  # True: no dialog
        if reply is None:  # the organiser asked for no reply; the calendar is updated
            return
        if body:
            reply.Body = body
        reply.Send()

    def create_event(
        self, subject: str, start: When, end: When, attendees: list[str], body: Text
    ) -> str:
        """Add an event to the calendar, inviting the attendees if there are any."""
        event = self.app.CreateItem(APPOINTMENT_ITEM)
        event.Subject = subject
        event.Start, event.End = wall(start), wall(end)
        event.Body = body or ""
        if attendees:
            event.MeetingStatus = MEETING
            for each in attendees:
                event.Recipients.Add(each)
            event.Recipients.ResolveAll()
        event.Save()  # first, so it has an EntryID
        native = event.EntryID
        if attendees:
            event.Send()
        return native

    def _walk(self, parent: Any = None) -> Iterator[Any]:
        """Every mail folder in the default store, parents before children."""
        for folder in (parent or self.session.DefaultStore.GetRootFolder()).Folders:
            if folder.DefaultItemType == MAIL_ITEM:
                yield folder
                yield from self._walk(folder)

    def _folder(self, name: Text) -> Any:
        """A mail folder by path (Inbox/Receipts) or name; the Inbox when there's none."""
        if not name:
            return self.session.GetDefaultFolder(INBOX)
        wanted = name.replace("\\", "/").strip("/").lower()
        for folder in self._walk():
            if wanted in (path(folder).lower(), folder.Name.lower()):
                return folder
        raise LookupError(f"No folder {name!r}. The folders command lists them.")

    def _rows(self, source: Any, where: str, limit: int, after: str) -> list[dict]:
        """Up to `limit` summaries from one folder's Table, newest first, received after."""
        table = source.GetTable(where) if where else source.GetTable()
        table.Columns.RemoveAll()
        for column in COLUMNS:
            table.Columns.Add(column)
        table.Sort("[ReceivedTime]", True)
        where_path, rows = path(source), []
        while not table.EndOfTable and len(rows) < limit:
            row = dict(zip(COLUMNS, table.GetNextRow().GetValues(), strict=True))
            received = utc(row["ReceivedTime"])
            if after and received and received < after:
                break
            rows.append(
                {
                    "id": row["EntryID"],
                    "folder": where_path,
                    "subject": row["Subject"] or "",
                    "from": {
                        "name": row["SenderName"] or "",
                        "smtp": row[SMTP] or row["SenderEmailAddress"] or "",
                    },
                    "received_utc": received,
                    "preview": row[PREVIEW] or "",
                    "unread": bool(row["UnRead"]),
                    "flagged": row[FLAG] == FLAGGED,
                }
            )
        return rows

    def _event(self, item: Any, begins: datetime, ends: datetime) -> dict:
        """An appointment as an Event."""
        invited = f"{item.RequiredAttendees or ''};{item.OptionalAttendees or ''}".split(";")
        return {
            "id": item.EntryID,
            "subject": item.Subject or "",
            "start": begins.isoformat(),
            "end": ends.isoformat(),
            "location": item.Location or "",
            "organizer": item.Organizer or "",
            "attendees": [name.strip() for name in invited if name.strip()],
            "is_recurring": bool(item.IsRecurring),
        }

    def _item(self, native: str) -> Any:
        """The item with this EntryID. A ValueError: mail.py knows nothing was sent."""
        session = self.session  # outside the try: a connection error says what it is
        try:
            return session.GetItemFromID(native)
        except Exception:
            raise ValueError(MOVED) from None

    def _draft(self, native: str) -> Any:
        """The unsent mail item with this EntryID; anything else is refused."""
        item = self._item(native)
        if item.Class != MAIL or item.Sent:
            raise ValueError(NOT_DRAFT)
        return item
