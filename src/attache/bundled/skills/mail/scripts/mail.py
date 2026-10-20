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
"""The mail skill's commands, the same for every account: Outlook (COM or Graph), Gmail and
Titan (IMAP). Each command prints exactly one JSON object, {"ok": true, ...} or
{"ok": false, "error": "..."}, and exits 0 or 1:

    uv run mail.py inbox --account all --unread
    uv run mail.py draft-reply gmail:1801234567890123456 --body "Thursday works."
    uv run mail.py send-draft gmail:1801234567890124000 --fingerprint 3f2a9c0d1e4b5a6c

The accounts are in <home>/skills/mail/accounts.toml (no secrets; `setup` copies the example).
Ids are "<account>:<native>", where the native part is whatever the backend needs to find the
item again. This file adds the prefix, the previews and approvals, the caps, `untrusted`, and
the audit log <home>/logs/mail.jsonl: one line per call, never a subject or a body.

Approvals. Sending a draft, answering an invitation and creating an event need the owner's
yes for exactly what will happen:
1. The command, without --fingerprint, writes <home>/.approvals/<F>.preview: plain text of
   what will be sent. F, the fingerprint, hashes the account, the item and that preview.
   A draft's preview and F come back with the draft itself.
2. The core's `ask {approves: F}` shows that file to the owner, verbatim, with Send and Not
   now. Send writes <home>/.approvals/<F>.
   The core writes the sha256 of the text it showed into that file.
3. The command with --fingerprint F checks that F is still the item's fingerprint, then claims
   the approval by renaming it to <F>.sending (atomic, so it is used once). It refuses one
   whose hash isn't that of its own preview made afresh (so a rewritten .preview file is no
   use), or that is older than 15 minutes. After the action <F>.sending and <F>.preview are
   deleted; previews of drafts never sent go after a day.

The backends are sibling modules (imap_backend, com_backend, graph_backend), imported only when
an account uses one, each with `Backend(account, settings, home)` and plain-dict results. A
ValueError (or NotImplementedError) from send_draft, respond or create_event means that nothing
left this machine, so the approval is put back. Any other exception means it may have gone:
<F>.sending stays, and a retry is refused, unless the backend has `guards_resends` (IMAP:
it never sends a recorded Message-ID twice, and decides itself).
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import shutil
import sys
import time
import tomllib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent  # <home>/skills/mail/scripts
BACKENDS = ("imap", "com", "graph")
FRESH = 15 * 60  # seconds an approval stays good
DAY = 24 * 60 * 60  # previews of drafts never sent are cleared after this
Json = dict[str, Any]


class MailError(Exception):
    """A call the rules don't allow as given; the message says what to do instead."""


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # one JSON object, not usage text and exit 2
        raise MailError(f"{self.prog}: {message}")

    def print_help(self, file: Any = None) -> None:  # -h: the usage, as the JSON's error
        raise MailError(self.format_help())


def open_backend(name: str, settings: Json, home: Path) -> Any:
    """The account's backend object, from a sibling module: Python puts a script's folder on
    sys.path. Tests replace this with fakes."""
    module = importlib.import_module(f"{settings['backend']}_backend")
    return module.Backend(name, settings, home)


class Mail:
    """One call: its accounts, the backends opened so far, and extra fields for its log line."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.opened: dict[str, Any] = {}
        self.log: Json = {}
        self._accounts: dict[str, Json] | None = None

    def accounts(self) -> dict[str, Json]:
        if self._accounts is None:
            path = self.home / "skills" / "mail" / "accounts.toml"
            if not path.is_file():
                raise MailError(f"no accounts yet: run `attache skill mail setup`, edit {path}")
            try:  # utf-8-sig: Notepad and PowerShell 5 may start the file with a BOM
                self._accounts = tomllib.loads(path.read_text(encoding="utf-8-sig"))
            except tomllib.TOMLDecodeError as error:
                raise MailError(f"{path}: {error}") from None
            for name, settings in self._accounts.items():
                good = isinstance(settings, dict) and settings.get("backend") in BACKENDS
                if not good or ":" in name:  # ":" ends the account part of an id
                    raise MailError(f"accounts.toml: [{name}] needs backend = imap, com or graph")
        return self._accounts

    def settings(self, name: str) -> Json:
        if name not in self.accounts():
            raise MailError(f"no account {name!r}; accounts.toml has {', '.join(self.accounts())}")
        return self.accounts()[name]

    def names(self, which: str) -> list[str]:
        """The accounts that --account means: one, or `all`."""
        if which == "all":
            return list(self.accounts())
        self.settings(which)  # refuses an account that isn't there
        return [which]

    def backend(self, name: str) -> Any:
        if name not in self.opened:
            self.opened[name] = open_backend(name, self.settings(name), self.home)
        return self.opened[name]

    def find(self, item_id: str) -> tuple[str, Any, str]:
        """(account, backend, native id) for an id such as "gmail:1801234567890123456"."""
        name, _, native = item_id.partition(":")
        if not native or name not in self.accounts():
            raise MailError(f"{item_id!r} is not a mail id: use the ids inbox and search give")
        return name, self.backend(name), native


def tag(account: str, item: Json) -> Json:
    """A backend's item as the agent sees it: account-prefixed id, capped preview, untrusted."""
    out = {**item, "account": account, "untrusted": True}
    if item.get("id"):
        out["id"] = f"{account}:{item['id']}"
    if "preview" in item:
        out["preview"] = " ".join(str(item["preview"]).split())[:200]  # characters
    return out


def each_account(mail: Mail, which: str, call: Callable[[Any], list[Json]]) -> Json:
    """call(backend) for each account, the items merged. With `all`, an account that fails is
    listed under `errors`, and one without the feature (agenda on IMAP) is left out."""
    items, errors = [], []
    for name in mail.names(which):
        try:
            items += [tag(name, item) for item in call(mail.backend(name))]
        except Exception as error:
            if which != "all":
                raise
            if not isinstance(error, NotImplementedError):
                errors.append({"account": name, "error": str(error) or type(error).__name__})
    return {"items": items, "errors": errors} if errors else {"items": items}


def each_id(mail: Mail, ids: list[str], key: str, call: Callable[[Any, str], list]) -> Json:
    """call(backend, native) for each id on its own, so one that fails doesn't hide the rest:
    the ids it gives back under `key`, and each failure under `errors`."""
    done, errors = [], []
    for item_id in ids:
        try:
            name, backend, native = mail.find(item_id)
            done += [f"{name}:{new}" for new in call(backend, native)]
        except Exception as error:
            errors.append({"id": item_id, "error": str(error) or type(error).__name__})
    if errors and not done:
        raise MailError("; ".join(f"{each['id']}: {each['error']}" for each in errors))
    return {key: done, "errors": errors} if errors else {key: done}


# Approvals: see the module docstring.


def fingerprint(account: str, native: str, preview: str) -> str:
    """16 hex digits of the sha256 of the account, the item and the preview the owner sees."""
    fields = {"account": account, "id": native, "preview": preview}
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def as_shown(lines: list[str], body: str) -> str:
    """A preview: one line per field, a blank line, the body. In the fields, control characters
    and line breaks become spaces, so a subject can't draw a fake To: line. Newlines are \\n,
    as the core's ask reads the file back before hashing what it showed."""
    fields = [re.sub(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]", " ", line) for line in lines]
    return "\n".join([*fields, "", body]).replace("\r\n", "\n").replace("\r", "\n")


def offer(mail: Mail, account: str, native: str, preview: str) -> str:
    """Write the preview for the core's ask to show; its fingerprint."""
    folder = mail.home / ".approvals"
    folder.mkdir(exist_ok=True)
    for old in folder.glob("*.preview"):  # drafts that were never sent
        if old.stat().st_mtime < time.time() - DAY:
            old.unlink(missing_ok=True)
    approves = fingerprint(account, native, preview)
    (folder / f"{approves}.preview").write_text(preview, encoding="utf-8")
    return approves


def claim(mail: Mail, backend: Any, account: str, native: str, preview: str, given: str) -> Path:
    """Take the owner's approval of this preview for this call: its <F>.sending file. A
    backend with `guards_resends` refuses what went before itself, so a <F>.sending left by
    an earlier try is no bar there."""
    expected = fingerprint(account, native, preview)
    if given != expected:
        raise MailError("it changed since it was approved: show it to the owner again")
    approval = mail.home / ".approvals" / expected
    sending = approval.with_suffix(".sending")
    if sending.exists() and not getattr(backend, "guards_resends", False):
        raise MailError("this may already have been sent; check Sent, then edit it to resend")
    try:
        os.replace(approval, sending)  # atomic: a second call finds no approval
    except FileNotFoundError:
        hint = f"show it to the owner with the ask tool (approves: {expected}) first"
        raise MailError(f"not approved: {hint}") from None
    seen = sending.read_text(encoding="utf-8").strip()  # the core's hash of what it showed
    stale = time.time() - sending.stat().st_mtime > FRESH
    if stale or seen != hashlib.sha256(preview.encode("utf-8")).hexdigest():
        sending.unlink()
        why = "the approval expired" if stale else "the preview changed since it was approved"
        raise MailError(f"{why}: ask again (approves: {expected})")
    mail.log["approved"] = True
    return sending


def act(sending: Path, action: Callable[[], Any]) -> Any:
    """Do the approved action; see the module docstring for what a failure leaves behind."""
    try:
        done = action()
    except (ValueError, NotImplementedError):
        os.replace(sending, sending.with_suffix(""))  # nothing went: the owner's yes stands
        raise
    except Exception as error:
        raise MailError(f"{error}; it may have been sent: check Sent first") from error
    sending.unlink(missing_ok=True)
    sending.with_suffix(".preview").unlink(missing_ok=True)
    return done


def draft_preview(account: str, backend: str, draft: Json) -> str:
    """Exactly what send-draft will send, as the owner sees it."""
    files = [f"{each['name']} ({each['size']} bytes)" for each in draft.get("attachments") or []]
    lines = [f"Send from {account} ({backend})"]
    for key in ("to", "cc", "bcc"):
        lines.append(f"{key.title()}: {', '.join(draft.get(key) or []) or 'none'}")
    lines += [f"Subject: {draft['subject']}", f"Attachments: {', '.join(files) or 'none'}"]
    return as_shown(lines, draft["body"])


# The commands: each takes (mail, args) and returns the result's fields.


def doctor(mail: Mail, args: argparse.Namespace) -> Json:
    found = []
    for name in mail.names(args.account):
        try:
            ok, detail = mail.backend(name).doctor()
        except Exception as error:  # a missing library or a bad setting
            ok, detail = False, f"{type(error).__name__}: {error}"
        backend = mail.settings(name)["backend"]
        found.append({"account": name, "backend": backend, "ok": ok, "detail": detail})
    return {"accounts": found}


def setup(mail: Mail, args: argparse.Namespace) -> Json:
    target = mail.home / "skills" / "mail" / "accounts.toml"
    created = not target.exists()
    if created:
        shutil.copyfile(HERE.parent / "accounts.example.toml", target)
    return {"path": str(target), "created": created}


def login(mail: Mail, args: argparse.Namespace) -> Json:
    # Never the agent, which would start a sign-in or wait on getpass. Its script tool runs
    # under `attache run`, which sets ATTACHE_WEB_URL; and on Windows NUL passes isatty().
    if "ATTACHE_WEB_URL" in os.environ or not sys.stdin.isatty():
        raise MailError(f"run this yourself in a terminal: attache skill mail login {args.account}")
    mail.backend(args.account).login()
    return {"account": args.account, "signed_in": True}


def folders(mail: Mail, args: argparse.Namespace) -> Json:
    return each_account(mail, args.account, lambda backend: backend.folders())


def search(mail: Mail, args: argparse.Namespace) -> Json:
    """search, and inbox, which is a search of the inbox without a query: the newest messages
    across the accounts, at most --limit."""
    limit = max(1, min(args.limit, 100))
    since = args.since.astimezone(UTC) if args.since else None

    def call(backend: Any) -> list[Json]:
        return backend.list(args.folder, args.unread, limit, since, args.query)

    result = each_account(mail, args.account, call)
    result["items"].sort(key=lambda item: item.get("received_utc") or "", reverse=True)
    del result["items"][limit:]
    return result


def read(mail: Mail, args: argparse.Namespace) -> Json:
    name, backend, native = mail.find(args.id)
    cap = max(1, min(args.max_chars, 50000))
    message = tag(name, backend.read(native, cap))
    body = str(message.get("body") or "")
    message.update(body=body[:cap], truncated=bool(message.get("truncated")) or len(body) > cap)
    return {"item": message}


def agenda(mail: Mail, args: argparse.Namespace) -> Json:
    result = each_account(mail, args.account, lambda b: b.agenda(max(1, min(args.days, 31))))
    result["items"].sort(key=lambda event: str(event.get("start") or ""))
    return result


def tasks(mail: Mail, args: argparse.Namespace) -> Json:
    return each_account(mail, args.account, lambda backend: backend.tasks())


def move(mail: Mail, args: argparse.Namespace) -> Json:
    if len(args.ids) > 20 and not args.many:  # --many only after the owner's yes
        raise MailError(f"moving {len(args.ids)} needs the owner's yes: ask, then add --many")
    return each_id(mail, args.ids, "moved", lambda b, native: b.move([native], args.to))


def flag(mail: Mail, args: argparse.Namespace) -> Json:
    return each_id(mail, args.ids, "changed", lambda b, native: b.flag([native], not args.off))


def mark_read(mail: Mail, args: argparse.Namespace) -> Json:
    return each_id(mail, args.ids, "changed", lambda b, n: b.mark_read([n], not args.off))


def shown(mail: Mail, account: str, draft: Json) -> Json:
    """A backend's draft (to, cc, bcc, subject, body, attachments) as the agent sees it, its
    preview written for the owner to approve."""
    preview = draft_preview(account, mail.settings(account)["backend"], draft)
    approves = offer(mail, account, draft["id"], preview)
    return {**draft, "id": f"{account}:{draft['id']}", "account": account, "fingerprint": approves}


def draft_reply(mail: Mail, args: argparse.Namespace) -> Json:
    name, backend, native = mail.find(args.id)
    draft = backend.draft(None, None, None, args.body, native, args.all)
    return {"draft": shown(mail, name, draft)}


def draft_new(mail: Mail, args: argparse.Namespace) -> Json:
    draft = mail.backend(args.account).draft(args.to, args.cc, args.subject, args.body, None, False)
    return {"draft": shown(mail, args.account, draft)}


def send_draft(mail: Mail, args: argparse.Namespace) -> Json:
    name, backend, native = mail.find(args.id)
    draft = backend.get_draft(native)  # as it is now, not as it was shown
    preview = draft_preview(name, mail.settings(name)["backend"], draft)
    if args.fingerprint is None:
        return {"needs_approval": offer(mail, name, native, preview), "preview": preview}
    sending = claim(mail, backend, name, native, preview, args.fingerprint)
    sent = act(sending, lambda: backend.send_draft(native))
    return {"sent": True, "id": f"{name}:{sent}" if sent else None}


def respond(mail: Mail, args: argparse.Namespace) -> Json:
    name, backend, native = mail.find(args.id)
    if mail.settings(name)["backend"] == "imap":  # before the owner is asked for nothing
        raise MailError("not in v1 for IMAP accounts")
    invitation = backend.read(native, 200)
    sender = (invitation.get("from") or {}).get("smtp", "")
    lines = [f"Reply '{args.response}' to an invitation in {name}"]
    lines.append(f"Invitation: {invitation.get('subject', '')} from {sender}")
    preview = as_shown(lines, args.body or "(no message)")
    if args.fingerprint is None:
        return {"needs_approval": offer(mail, name, native, preview), "preview": preview}
    sending = claim(mail, backend, name, native, preview, args.fingerprint)
    act(sending, lambda: backend.respond(native, args.response, args.body))
    return {"sent": True, "id": args.id}


def create_event(mail: Mail, args: argparse.Namespace) -> Json:
    backend, attendees = mail.backend(args.account), sorted(args.attendees)
    if mail.settings(args.account)["backend"] == "imap":  # before the owner is asked
        raise MailError("not in v1 for IMAP accounts")
    lines = [f"Create an event in {args.account}", f"Subject: {args.subject}"]
    lines.append(f"Start: {args.start.isoformat(' ', 'minutes')}")
    lines.append(f"End: {args.end.isoformat(' ', 'minutes')}")
    lines.append(f"Invite: {', '.join(attendees) or 'nobody'}")
    preview = as_shown(lines, args.body or "")
    if args.fingerprint is None:
        return {"needs_approval": offer(mail, args.account, "event", preview), "preview": preview}
    sending = claim(mail, backend, args.account, "event", preview, args.fingerprint)
    event = (args.subject, args.start, args.end, attendees, args.body)
    native = act(sending, lambda: backend.create_event(*event))
    return {"created": True, "id": f"{args.account}:{native}"}


HANDLERS = (doctor, setup, login, folders, search, read, agenda, tasks, move, flag)
HANDLERS += (mark_read, draft_reply, draft_new, send_draft, respond, create_event)
COMMANDS = {handler.__name__.replace("_", "-"): handler for handler in HANDLERS}  # mark-read
COMMANDS["inbox"] = search  # with no --query or --folder


def parser() -> Parser:
    """The commands and their arguments. Times are ISO; without a zone they are local time. A
    value starting with "-" goes after "=": --query=-in:chats."""
    top = Parser(prog="mail.py", description="Mail commands; each prints one JSON object.")
    sub = top.add_subparsers(dest="command", required=True)
    commands = {name: sub.add_parser(name) for name in COMMANDS}
    for name in ("doctor", "folders", "inbox", "search", "agenda", "tasks"):
        commands[name].add_argument("--account", default="all")
    for name in ("read", "draft-reply", "send-draft", "respond"):
        commands[name].add_argument("id")
    for name in ("move", "flag", "mark-read"):
        commands[name].add_argument("ids", nargs="+")
    for name in ("send-draft", "respond", "create-event"):
        commands[name].add_argument("--fingerprint")
    for name in ("inbox", "search"):
        commands[name].add_argument("--limit", type=int, default=25)
        commands[name].add_argument("--unread", action="store_true")
        commands[name].add_argument("--since", type=datetime.fromisoformat)
    for name in ("flag", "mark-read"):
        commands[name].add_argument("--off", action="store_true")
    commands["login"].add_argument("account")
    commands["search"].add_argument("--query", required=True)
    commands["inbox"].set_defaults(query=None, folder=None)
    commands["search"].add_argument("--folder")
    commands["read"].add_argument("--max-chars", type=int, default=8000)
    commands["agenda"].add_argument("--days", type=int, default=7)
    commands["move"].add_argument("--to", required=True)
    commands["move"].add_argument("--many", action="store_true")
    commands["draft-reply"].add_argument("--all", action="store_true")
    commands["respond"].add_argument("response", choices=["accept", "tentative", "decline"])
    for name in ("draft-new", "draft-reply", "respond", "create-event"):
        commands[name].add_argument("--body", required=name.startswith("draft"))
    for name in ("draft-new", "create-event"):
        commands[name].add_argument("--account", required=True)
        commands[name].add_argument("--subject", required=True)
    commands["draft-new"].add_argument("--to", nargs="+", required=True)
    commands["draft-new"].add_argument("--cc", nargs="*", default=[])
    commands["create-event"].add_argument("--start", type=datetime.fromisoformat, required=True)
    commands["create-event"].add_argument("--end", type=datetime.fromisoformat, required=True)
    commands["create-event"].add_argument("--attendees", nargs="*", default=[])
    return top


def touched(args: argparse.Namespace | None, result: Json) -> list[str]:
    """The ids a call was given and the ids it gave back, each once."""
    given = [getattr(args, "id", None), *getattr(args, "ids", [])]
    items = [result, result.get("draft") or {}, *result.get("items", [])]
    returned = [item.get("id") for item in items] + result.get("moved", [])  # COM ids change
    return [item_id for item_id in dict.fromkeys(given + returned) if isinstance(item_id, str)]


def audit(mail: Mail, args: argparse.Namespace | None, result: Json) -> None:
    """Append the call's line to <home>/logs/mail.jsonl. Never a subject or a body, so a call
    whose arguments didn't parse logs no error text: they might hold either."""
    ids = touched(args, result)
    account = getattr(args, "account", None) or (ids[0].partition(":")[0] if ids else None)
    when = datetime.now(UTC).isoformat(timespec="seconds")
    line = {"ts": when, "command": getattr(args, "command", None), "account": account}
    line |= {"ids": ids, "ok": result["ok"], **mail.log}
    if not result["ok"]:
        line["error"] = result["error"] if args else "invalid arguments"
    path = mail.home / "logs" / "mail.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as log:
        log.write(json.dumps(line, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):  # subjects in any script, even down a Windows pipe
        sys.stdout.reconfigure(encoding="utf-8")
    # $ATTACHE_HOME, which Attaché sets; else the home this script is installed in
    mail, args = Mail(Path(os.environ.get("ATTACHE_HOME") or HERE.parents[2])), None
    try:
        args = parser().parse_args(argv)
        result = {"ok": True, **COMMANDS[args.command](mail, args)}
    except Exception as error:  # every failure is one JSON object too
        result = {"ok": False, "error": str(error) or type(error).__name__}
    audit(mail, args, result)
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
