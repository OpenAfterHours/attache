"""Outlook through Microsoft Graph, for mail.py: msal signs in, urllib makes the requests.

Signing in is a one-time browser login, `attache skill mail login <account>`, which the owner
runs in their own terminal; msal then renews the token silently. The token cache is encrypted
for this user by msal-extensions (DPAPI on Windows) at <home>/.mail/graph-cache.bin. A
Microsoft sign-in lapses after 90 days without use or when the password changes; doctor names
the cause.

Ids are immutable ids (Prefer: IdType="ImmutableId"), which survive moves, and bodies arrive as
plain text (Prefer: outlook.body-content-type="text"). When Graph is busy (429, 503), a request
is tried again after its Retry-After. A ValueError from a send, a reply or a new event means
nothing went out (GraphRefusedError, NotSentError), so mail.py can let the approval stand.

msal and msal-extensions are imported only inside functions. Tests pass `client`, an object
whose `request(method, path, body)` stands in for Graph and whose `app` stands in for msal's
PublicClientApplication.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

Text = str | None
When = datetime | str

GRAPH = "https://graph.microsoft.com/v1.0"
# msal asks for offline_access (the refresh token) itself, and refuses it in this list.
SCOPES = ["Mail.ReadWrite", "Mail.Send", "Calendars.ReadWrite", "Tasks.ReadWrite"]
PREFER = 'IdType="ImmutableId", outlook.body-content-type="text"'
SUMMARY = "id,subject,from,receivedDateTime,bodyPreview,isRead,flag,parentFolderId"
RESPONSES = {"accept": "accept", "tentative": "tentativelyAccept", "decline": "decline"}
EPOCH = "1900-01-01T00:00:00Z"
LOGIN_TIMEOUT = 300  # seconds to finish signing in, in the browser
TRIES, PATIENCE = 3, 30  # a busy Graph gets at most 3 tries and 30 seconds of waiting in all

NO_CLIENT_ID = (
    'Set client_id = "<app id>" for this account in accounts.toml: the Application (client) '
    "ID of an app registered in Microsoft Entra, with http://localhost as a redirect URI."
)
NO_BROWSER = "Signing in to Microsoft needs a web browser: run the login where one can open."
CAUSES = {  # Microsoft's AADSTS error codes, as the cause and the fix
    700082: "The Microsoft sign-in lapsed after 90 days without use. {login}",
    50173: "The Microsoft sign-in ended when your password changed. {login}",
    65001: "This account hasn't granted the app's permissions. {login}, and accept them",
    90094: (
        "Your organisation blocks unverified apps: an admin has to approve this app in "
        "Microsoft Entra first. Then {login}"
    ),
    700016: (
        "Microsoft doesn't know this client_id in that tenant. "
        "Check client_id and tenant for this account in accounts.toml."
    ),
}


class GraphError(RuntimeError):
    """Graph answered with an HTTP error, which can come after it acted (a 5xx)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Microsoft Graph refused the request ({status}): {message}")
        self.status = status


class GraphRefusedError(GraphError, ValueError):
    """Graph refused the request (a 4xx, or 503 Unavailable), so it certainly didn't act. As a
    ValueError, it tells mail.py that nothing was sent."""


class NotSentError(ValueError):
    """The request never reached Graph (no sign-in, or no connection), so nothing was sent."""


def failure(error: urllib.error.HTTPError) -> GraphError:
    """Graph's HTTP error as an exception, with Graph's own message."""
    try:
        message = json.loads(error.read())["error"]["message"]
    except (ValueError, KeyError, TypeError):
        message = str(error.reason)
    refused = error.code < 500 or error.code == 503
    return (GraphRefusedError if refused else GraphError)(error.code, message)


def retry_after(error: urllib.error.HTTPError) -> float:
    """How long Graph asks for before trying again, in seconds; 1 when it doesn't say."""
    try:
        return max(0.0, float((error.headers or {}).get("Retry-After", 1)))
    except ValueError:  # an HTTP date, which Graph doesn't send
        return 1.0


def retried(method: str, status: int) -> bool:
    """Whether to try again: Graph throttled (429) or was briefly down (503), or a gateway timed
    out (504) on a GET or PATCH. After a 504 a POST, such as a send, may have gone through."""
    return status in (429, 503) or (status == 504 and method in ("GET", "PATCH"))


def q(native: str) -> str:
    """An id quoted for a URL path, so no id can reach another endpoint."""
    return quote(native, safe="")


def msg(native: str) -> str:
    """The path of one message."""
    return f"/me/messages/{q(native)}"


def encode(params: dict[str, Any]) -> str:
    """A URL query string for Graph's $ parameters."""
    return "?" + urlencode(params, quote_via=quote, safe="$")


def stamp(moment: datetime) -> str:
    """An aware (or local naive) datetime as ISO 8601 UTC."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def local(when: dict) -> str:
    """Graph's {dateTime, timeZone: "UTC"} as an ISO local time with its offset."""
    moment = datetime.fromisoformat(when["dateTime"][:19]).replace(tzinfo=UTC)
    return moment.astimezone().isoformat()


def utc(value: When) -> dict:
    """An ISO time or a datetime as Graph's {dateTime, timeZone}."""
    moment = datetime.fromisoformat(value) if isinstance(value, str) else value
    when = moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")
    return {"dateTime": when, "timeZone": "UTC"}


def addresses(message: dict, key: str) -> list[str]:
    """The addresses in toRecipients or ccRecipients."""
    found = [r.get("emailAddress") or {} for r in message.get(key) or []]
    return [e["address"] for e in found if e.get("address")]


def recipients(addresses: list[str]) -> list[dict]:
    """Addresses as Graph recipients."""
    return [{"emailAddress": {"address": each}} for each in addresses]


def text(body: str | None) -> dict:
    """A plain-text body for Graph."""
    return {"contentType": "Text", "content": body or ""}


class Backend:
    """One Outlook account, through Microsoft Graph."""

    def __init__(self, account: str, settings: dict, home: Path, client: Any = None) -> None:
        self.account, self.settings, self.home = account, settings, home
        self._client = client
        self._app: Any = None
        self.request = client.request if client is not None else self._http

    def doctor(self) -> tuple[bool, str]:
        """Whether Graph answers, and if not, why and what to do."""
        try:
            self._token()
            inbox = self.request("GET", "/me/mailFolders/inbox?$select=unreadItemCount", None)
        except Exception as error:
            return False, str(error)
        return True, f"Microsoft Graph: signed in, {inbox.get('unreadItemCount', 0)} unread"

    def login(self) -> None:
        """Sign in once in the browser; msal listens on localhost for the answer."""
        import webbrowser

        try:
            webbrowser.get()
        except webbrowser.Error:
            raise PermissionError(NO_BROWSER) from None
        app = self._msal()
        try:
            result = app.acquire_token_interactive(
                SCOPES,
                login_hint=self.settings.get("user"),
                prompt="select_account",
                timeout=LOGIN_TIMEOUT,
            )
        except Exception as error:  # msal raises when the browser or its answer fails
            raise PermissionError(f"The Microsoft sign-in didn't finish: {error}") from None
        if "access_token" not in result:
            raise PermissionError(self._cause(result))

    def folders(self) -> list[dict]:
        """Every mail folder with its unread and total counts."""
        return [
            {
                "folder": folder["displayName"],
                "path": path,
                "unread": folder.get("unreadItemCount", 0),
                "total": folder.get("totalItemCount", 0),
            }
            for folder, path in self._walk()
        ]

    def list(
        self, folder: Text, unread: bool, limit: int, since: datetime | None, query: Text
    ) -> list[dict]:
        """Summaries, newest first: one folder (the Inbox by default), or with a query and no
        folder, the whole mailbox."""
        if query and not folder:
            base, where = "/me/messages", None
        else:
            folder_id, where = self._folder(folder)
            base = f"/me/mailFolders/{q(folder_id)}/messages"
        after = stamp(since) if since else EPOCH
        params: dict[str, Any] = {"$top": limit, "$select": SUMMARY}
        if query:  # $search can't be combined with $filter or $orderby
            params["$search"] = '"' + query.replace('"', "") + '"'
        else:  # Graph wants the $orderby property first in $filter
            wanted = [f"receivedDateTime ge {after}"] + (["isRead eq false"] if unread else [])
            params |= {"$filter": " and ".join(wanted), "$orderby": "receivedDateTime desc"}
        found = self.request("GET", base + encode(params), None).get("value", [])
        if query:
            found = [m for m in found if m.get("receivedDateTime", "") >= after]
            found = [m for m in found if not (unread and m.get("isRead"))]
            found.sort(key=lambda m: m.get("receivedDateTime", ""), reverse=True)
        names = {f["id"]: path for f, path in self._walk()} if where is None and found else {}
        return [self._summary(m, where or names.get(m.get("parentFolderId"), "")) for m in found]

    def read(self, native: str, max_chars: int) -> dict:
        """One message with its body as plain text, capped at max_chars."""
        fields = f"{SUMMARY},toRecipients,ccRecipients,body,hasAttachments"
        message = self.request("GET", f"{msg(native)}?$select={fields}", None)
        parent = f"/me/mailFolders/{q(message['parentFolderId'])}?$select=displayName"
        folder = self.request("GET", parent, None).get("displayName", "")
        body = (message.get("body") or {}).get("content") or ""
        attachments = self._attachments(native) if message.get("hasAttachments") else []
        return {
            **self._summary(message, folder),
            "to": addresses(message, "toRecipients"),
            "cc": addresses(message, "ccRecipients"),
            "body": body[:max_chars],
            "truncated": len(body) > max_chars,
            "attachments": attachments,
        }

    def move(self, natives: list[str], folder: str) -> list[str]:
        """Move messages, returning their ids (immutable ids stay the same)."""
        target = {"destinationId": self._folder(folder)[0]}
        return [self.request("POST", f"{msg(n)}/move", target)["id"] for n in natives]

    def flag(self, natives: list[str], on: bool) -> list[str]:
        """Set or clear the follow-up flag."""
        status = {"flag": {"flagStatus": "flagged" if on else "notFlagged"}}
        for native in natives:
            self.request("PATCH", msg(native), status)
        return natives

    def mark_read(self, natives: list[str], on: bool) -> list[str]:
        """Mark messages read, or unread when on is False."""
        for native in natives:
            self.request("PATCH", msg(native), {"isRead": on})
        return natives

    def draft(
        self, to: list[str], cc: list[str], subject: str, body: str, reply_to: Text, reply_all: bool
    ) -> dict:
        """Save a new message, or a reply to reply_to, in Drafts. A reply's body is only the
        text given: the PATCH replaces the quoted original, so the draft is what's approved."""
        if reply_to:
            action = "createReplyAll" if reply_all else "createReply"
            created = self.request("POST", f"{msg(reply_to)}/{action}", {})
            self.request("PATCH", msg(created["id"]), {"body": text(body)})
        else:
            new = {
                "subject": subject,
                "body": text(body),
                "toRecipients": recipients(to),
                "ccRecipients": recipients(cc or []),
            }
            created = self.request("POST", "/me/messages", new)
        return self.get_draft(created["id"])

    def get_draft(self, native: str) -> dict:
        """A draft as Graph holds it now, with everything that would be sent: a BCC or an
        attachment added elsewhere too. Anything but a draft is refused."""
        fields = "id,subject,body,toRecipients,ccRecipients,bccRecipients,isDraft"
        message = self.request("GET", f"{msg(native)}?$select={fields}", None)
        if not message.get("isDraft"):
            raise ValueError("That isn't an unsent draft.")
        return {
            "id": message["id"],
            "to": addresses(message, "toRecipients"),
            "cc": addresses(message, "ccRecipients"),
            "bcc": addresses(message, "bccRecipients"),
            "subject": message.get("subject") or "",
            "body": (message.get("body") or {}).get("content") or "",
            "attachments": self._attachments(native),
        }

    def send_draft(self, native: str) -> str:
        """Send a draft with only the text body the owner saw, which replaces an HTML part
        edited in elsewhere. Returns its id, an immutable id, which the copy in Sent Items
        should keep. A ValueError means nothing was sent."""
        draft = self.get_draft(native)
        self.request("PATCH", msg(native), {"body": text(draft["body"])})
        self.request("POST", f"{msg(native)}/send", None)
        return native

    def agenda(self, days: int) -> list[dict]:
        """Calendar events from now until `days` ahead; calendarView expands recurrences."""
        now = datetime.now(UTC)
        params = {
            "startDateTime": stamp(now),
            "endDateTime": stamp(now + timedelta(days=days)),
            "$orderby": "start/dateTime",
            "$top": 100,
            "$select": "id,subject,start,end,location,organizer,attendees,type",
        }
        return [self._event(e) for e in self._all("/me/calendarView" + encode(params))]

    def tasks(self) -> list[dict]:
        """Open tasks in Microsoft To Do, from every list but flagged email."""
        found = []
        for todo in self._all("/me/todo/lists"):
            if todo.get("wellknownListName") == "flaggedEmails":  # that's mail, not tasks
                continue
            listing = f"/me/todo/lists/{q(todo['id'])}/tasks"
            for task in self._all(listing + encode({"$filter": "status ne 'completed'"})):
                due = (task.get("dueDateTime") or {}).get("dateTime") or ""
                found.append(
                    {
                        "id": task["id"],
                        "subject": task.get("title") or "",
                        "due": due[:10] or None,
                        "done": task.get("status") == "completed",
                    }
                )
        return found

    def respond(self, native: str, response: str, body: Text) -> None:
        """Accept, tentatively accept or decline a meeting: an invitation in the mail or an
        event from the agenda."""
        answer = {"comment": body or "", "sendResponse": True}
        event = q(self._event_id(native))
        self.request("POST", f"/me/events/{event}/{RESPONSES[response]}", answer)

    def create_event(
        self, subject: str, start: When, end: When, attendees: list[str], body: Text
    ) -> str:
        """Add an event to the calendar; Graph invites the attendees."""
        event = {
            "subject": subject,
            "body": text(body),
            "start": utc(start),
            "end": utc(end),
            "attendees": [
                {"emailAddress": {"address": each}, "type": "required"} for each in attendees or []
            ],
        }
        return self.request("POST", "/me/events", event)["id"]

    def _msal(self) -> Any:
        """msal's PublicClientApplication, with the encrypted token cache."""
        if self._client is not None:
            return self._client.app
        if self._app is None:
            client_id = self.settings.get("client_id")
            if not client_id:
                raise ValueError(NO_CLIENT_ID)
            import msal
            from msal_extensions import PersistedTokenCache, build_encrypted_persistence

            cache_file = self.home / ".mail" / "graph-cache.bin"
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache = PersistedTokenCache(build_encrypted_persistence(str(cache_file)))
            authority = f"https://login.microsoftonline.com/{self.settings.get('tenant', 'common')}"
            self._app = msal.PublicClientApplication(
                client_id, authority=authority, token_cache=cache
            )
        return self._app

    def _token(self) -> str:
        """An access token from the cache, renewed silently when it has expired."""
        app = self._msal()
        accounts = app.get_accounts(username=self.settings.get("user"))
        result = None
        if accounts:
            result = app.acquire_token_silent_with_error(SCOPES, account=accounts[0])
        if not result or "access_token" not in result:
            raise NotSentError(self._cause(result))
        return result["access_token"]

    def _cause(self, result: dict | None) -> str:
        """Why a sign-in failed, and the fix."""
        login = f"Sign in again in your own terminal: attache skill mail login {self.account}"
        if not result:
            return f"Not signed in to Microsoft yet. {login}"
        for code in result.get("error_codes") or []:
            if code in CAUSES:
                return CAUSES[code].format(login=login)
        return f"Microsoft refused the sign-in ({result.get('error')}). {login}"

    def _http(self, method: str, path: str, body: dict | None = None) -> dict:
        """One Graph request, tried again while Graph is busy and `retried` allows it. Only
        Graph's own URLs are used: a next page's link or a path."""
        url = path if path.startswith(GRAPH + "/") else GRAPH + path
        headers = {"Authorization": f"Bearer {self._token()}", "Prefer": PREFER}
        if body is not None:
            headers["Content-Type"] = "application/json"
        data = b"" if body is None else json.dumps(body).encode("utf-8")  # POST needs a length
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        waited = 0.0
        for attempt in range(1, TRIES + 1):
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    return json.loads(response.read() or b"{}")  # /send answers with nothing
            except json.JSONDecodeError:  # a 2xx all the same, so Graph may have acted
                raise GraphError(200, "the answer wasn't JSON") from None
            except urllib.error.HTTPError as error:
                wait = retry_after(error)
                if attempt == TRIES or not retried(method, error.code) or waited + wait > PATIENCE:
                    raise failure(error) from None
                waited += wait
                time.sleep(wait)
            except urllib.error.URLError as error:  # only while connecting or writing the request
                raise NotSentError(f"Microsoft Graph can't be reached: {error.reason}") from None
        raise AssertionError("the last try returns or raises")

    def _all(self, path: str) -> Iterator[dict]:
        """Every item of a listing, following its next-page links."""
        while path:
            page = self.request("GET", path, None)
            yield from page.get("value", [])
            path = page.get("@odata.nextLink")

    def _walk(self, parent: str | None = None, prefix: str = "") -> Iterator[tuple[dict, str]]:
        """Every mail folder with its path, like Inbox/Receipts, parents before children."""
        listing = f"/me/mailFolders/{q(parent)}/childFolders" if parent else "/me/mailFolders"
        for folder in self._all(listing + "?$top=100"):
            path = prefix + folder["displayName"]
            yield folder, path
            if folder.get("childFolderCount"):
                yield from self._walk(folder["id"], path + "/")

    def _folder(self, name: Text) -> tuple[str, str]:
        """(id, path) of a folder by path (Inbox/Receipts) or name; the Inbox when there's
        none."""
        if not name:
            return "inbox", "Inbox"
        wanted = name.replace("\\", "/").strip("/").lower()
        for folder, path in self._walk():
            if wanted in (path.lower(), folder["displayName"].lower()):
                return folder["id"], path
        raise LookupError(f"No folder {name!r}. The folders command lists them.")

    def _summary(self, message: dict, folder: str) -> dict:
        """A Graph message as a Summary."""
        sender = (message.get("from") or {}).get("emailAddress") or {}
        return {
            "id": message["id"],
            "folder": folder,
            "subject": message.get("subject") or "",
            "from": {"name": sender.get("name") or "", "smtp": sender.get("address") or ""},
            "received_utc": message.get("receivedDateTime"),
            "preview": message.get("bodyPreview") or "",
            "unread": not message.get("isRead", True),
            "flagged": (message.get("flag") or {}).get("flagStatus") == "flagged",
        }

    def _event(self, event: dict) -> dict:
        """A Graph event as an Event."""
        organizer = (event.get("organizer") or {}).get("emailAddress") or {}
        invited = [a.get("emailAddress") or {} for a in event.get("attendees") or []]
        return {
            "id": event["id"],
            "subject": event.get("subject") or "",
            "start": local(event["start"]),
            "end": local(event["end"]),
            "location": (event.get("location") or {}).get("displayName") or "",
            "organizer": organizer.get("name") or organizer.get("address") or "",
            "attendees": [e.get("name") or e.get("address") or "" for e in invited],
            "is_recurring": event.get("type") in ("occurrence", "exception"),
        }

    def _attachments(self, native: str) -> list[dict]:
        """A message's attachments, inline pictures too, as {name, size}."""
        listing = self.request("GET", f"{msg(native)}/attachments?$select=name,size", None)
        return [{"name": a.get("name"), "size": a.get("size")} for a in listing.get("value", [])]

    def _event_id(self, native: str) -> str:
        """The event behind a meeting invitation in the mail; an agenda id is one already."""
        try:
            path = f"{msg(native)}?$expand=microsoft.graph.eventMessage/event"
            invitation = self.request("GET", path, None)
        except GraphError:
            return native
        return (invitation.get("event") or {}).get("id") or native
