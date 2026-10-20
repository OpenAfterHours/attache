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
item again. This file adds the prefix, the fingerprints and approvals, the caps, `untrusted`,
and the audit log <home>/logs/mail.jsonl: one line per call, never a subject or a body. The
backends are sibling modules (imap_backend, com_backend, graph_backend), imported only when an
account uses one, each with `Backend(account, settings, home)`.

Sending, answering an invitation and creating an event need the owner's approval. The core's
`ask` tool, given `approves: <fingerprint>`, writes <home>/.approvals/<fingerprint> when the
owner picks Send. The fingerprint hashes what the owner was shown, so a draft that changed
after the yes is refused, and each approval is used once.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib
import json
import os
import shutil
import sys
import tomllib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent  # <home>/skills/mail/scripts
BACKENDS = ("imap", "com", "graph")
MANY = 20  # moving more messages than this at once needs --many, after the owner's yes
PREVIEW = 200  # characters of a summary's preview
Json = dict[str, Any]


class MailError(Exception):
    """A call the rules don't allow as given; the message says what to do instead."""


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # one JSON object, not usage text and exit 2
        raise MailError(f"{self.prog}: {message}")


def fingerprint(fields: Json) -> str:
    """16 hex digits of the sha256 of what the owner is shown and approves."""
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def draft_fingerprint(draft: Json) -> str:
    to, cc = sorted(draft["to"]), sorted(draft["cc"])
    return fingerprint({"to": to, "cc": cc, "subject": draft["subject"], "body": draft["body"]})


def open_backend(name: str, settings: Json, home: Path) -> Any:
    """The account's backend object. Tests replace this with fakes."""
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
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

    def approval(self, approved: str) -> Path:
        """The file the core's ask tool writes when the owner approves this fingerprint."""
        return self.home / ".approvals" / approved


def between(value: int, most: int) -> int:
    """A count from the agent, kept between 1 and `most`."""
    return max(1, min(value, most))


def tag(account: str, item: Json) -> Json:
    """A backend's item as the agent sees it: account-prefixed id, capped preview, untrusted."""
    out = {**item, "account": account, "untrusted": True}
    if item.get("id"):
        out["id"] = f"{account}:{item['id']}"
    if "preview" in item:
        out["preview"] = " ".join(str(item["preview"]).split())[:PREVIEW]
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


def by_account(mail: Mail, ids: list[str], call: Callable[[Any, list[str]], list]) -> list[str]:
    """call(backend, natives) once for each account among `ids`; the ids it acted on."""
    groups: dict[str, list[str]] = {}
    for item_id in ids:
        name, _, native = mail.find(item_id)
        groups.setdefault(name, []).append(native)
    done = []
    for name, natives in groups.items():
        done += [f"{name}:{native}" for native in call(mail.backend(name), natives)]
    return done


def approved(mail: Mail, expected: str, given: str | None, what: str) -> Json | None:
    """None when `given` is the current fingerprint and the owner approved it; else what the
    agent must do first."""
    if given is None:
        return {"needs_approval": expected}
    if given != expected:
        raise MailError(f"the {what} changed since it was approved: show it to the owner again")
    if not mail.approval(expected).is_file():
        raise MailError(f"not approved: show it to the owner with ask (approves: {expected})")
    mail.log["approved"] = True
    return None


def check_send_limit(mail: Mail, account: str, recipients: int) -> None:
    """Refuse a send that would pass the account's send_limit_hour, counted in the log."""
    if not (limit := mail.settings(account).get("send_limit_hour")):
        return
    since = (datetime.now(UTC) - timedelta(hours=1)).isoformat(timespec="seconds")
    path, count = mail.home / "logs" / "mail.jsonl", 0
    for text in path.read_text(encoding="utf-8").splitlines() if path.exists() else []:
        with contextlib.suppress(ValueError):  # a line cut short by a crash
            line = json.loads(text)
            if line["account"] == account and line["ok"] and line["ts"] >= since:
                count += line.get("recipients", 0)
    if count + recipients > limit:
        raise MailError(f"{account} sends to {limit} people an hour; {count} so far: try later")


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


def newest(mail: Mail, args: argparse.Namespace, folder: str | None, query: str | None) -> Json:
    """inbox and search: the newest messages across the accounts."""
    limit, after = between(args.limit, 100), args.since and args.since.astimezone(UTC)
    result = each_account(
        mail, args.account, lambda b: b.list(folder, args.unread, limit, after, query)
    )
    result["items"].sort(key=lambda summary: summary.get("received_utc") or "", reverse=True)
    result["items"] = result["items"][:limit]
    return result


def inbox(mail: Mail, args: argparse.Namespace) -> Json:
    return newest(mail, args, None, None)


def search(mail: Mail, args: argparse.Namespace) -> Json:
    return newest(mail, args, args.folder, args.query)


def read(mail: Mail, args: argparse.Namespace) -> Json:
    name, backend, native = mail.find(args.id)
    cap = between(args.max_chars, 50000)
    message = tag(name, backend.read(native, cap))
    body = str(message.get("body") or "")
    message.update(body=body[:cap], truncated=bool(message.get("truncated")) or len(body) > cap)
    return {"item": message}


def agenda(mail: Mail, args: argparse.Namespace) -> Json:
    result = each_account(
        mail, args.account, lambda backend: backend.agenda(between(args.days, 31))
    )
    result["items"].sort(key=lambda event: str(event.get("start") or ""))
    return result


def tasks(mail: Mail, args: argparse.Namespace) -> Json:
    return each_account(mail, args.account, lambda backend: backend.tasks())


def move(mail: Mail, args: argparse.Namespace) -> Json:
    if len(args.ids) > MANY and not args.many:
        raise MailError(f"moving {len(args.ids)} needs the owner's yes: ask, then add --many")
    return {"moved": by_account(mail, args.ids, lambda b, natives: b.move(natives, args.to))}


def flag(mail: Mail, args: argparse.Namespace) -> Json:
    return {"changed": by_account(mail, args.ids, lambda b, n: b.flag(n, not args.off))}


def mark_read(mail: Mail, args: argparse.Namespace) -> Json:
    return {"changed": by_account(mail, args.ids, lambda b, n: b.mark_read(n, not args.off))}


def shown(account: str, draft: Json) -> Json:
    """A backend's draft as the agent sees it, with the fingerprint the owner approves."""
    fields = {key: draft[key] for key in ("to", "cc", "subject", "body")}
    fields["fingerprint"] = draft_fingerprint(draft)
    return {"id": f"{account}:{draft['id']}", "account": account, **fields}


def draft_reply(mail: Mail, args: argparse.Namespace) -> Json:
    name, backend, native = mail.find(args.id)
    return {"draft": shown(name, backend.draft(None, None, None, args.body, native, args.all))}


def draft_new(mail: Mail, args: argparse.Namespace) -> Json:
    draft = mail.backend(args.account).draft(args.to, args.cc, args.subject, args.body, None, False)
    return {"draft": shown(args.account, draft)}


def send_draft(mail: Mail, args: argparse.Namespace) -> Json:
    name, backend, native = mail.find(args.draft_id)
    draft = shown(name, backend.get_draft(native))  # as it is now, not as it was shown
    if ask := approved(mail, draft["fingerprint"], args.fingerprint, "draft"):
        return {**ask, "preview": draft}
    mail.log["recipients"] = len(draft["to"]) + len(draft["cc"])
    check_send_limit(mail, name, mail.log["recipients"])
    sent = backend.send_draft(native)
    mail.approval(draft["fingerprint"]).unlink(missing_ok=True)  # each approval is used once
    return {"sent": True, "id": f"{name}:{sent}" if sent else None}


def respond(mail: Mail, args: argparse.Namespace) -> Json:
    _, backend, native = mail.find(args.id)
    preview = {"id": args.id, "response": args.response, "body": args.body or ""}
    if ask := approved(mail, fingerprint(preview), args.fingerprint, "reply"):
        return {**ask, "preview": preview}
    backend.respond(native, args.response, args.body)
    mail.approval(fingerprint(preview)).unlink(missing_ok=True)
    return {"sent": True, "id": args.id}


def create_event(mail: Mail, args: argparse.Namespace) -> Json:
    attendees = sorted(args.attendees)
    shown_fields = ("account", "subject", "start", "end", "body")
    preview = {key: str(getattr(args, key) or "") for key in shown_fields}
    preview["attendees"] = attendees
    if ask := approved(mail, fingerprint(preview), args.fingerprint, "event"):
        return {**ask, "preview": preview}
    backend = mail.backend(args.account)
    native = backend.create_event(args.subject, args.start, args.end, attendees, args.body)
    mail.approval(fingerprint(preview)).unlink(missing_ok=True)
    return {"created": True, "id": f"{args.account}:{native}"}


HANDLERS = (doctor, setup, login, folders, inbox, search, read, agenda, tasks, move, flag)
HANDLERS += (mark_read, draft_reply, draft_new, send_draft, respond, create_event)
COMMANDS = {handler.__name__.replace("_", "-"): handler for handler in HANDLERS}  # mark-read


def parser() -> Parser:
    """The commands and their arguments. Times are ISO; without a zone they are local time."""
    top = Parser(prog="mail.py", description="Mail commands; each prints one JSON object.")
    sub = top.add_subparsers(dest="command", required=True)
    commands = {name: sub.add_parser(name) for name in COMMANDS}
    for name in ("doctor", "folders", "inbox", "search", "agenda", "tasks"):
        commands[name].add_argument("--account", default="all")
    for name in ("read", "draft-reply", "respond"):
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
    commands["search"].add_argument("--folder")
    commands["read"].add_argument("--max-chars", type=int, default=8000)
    commands["agenda"].add_argument("--days", type=int, default=7)
    commands["move"].add_argument("--to", required=True)
    commands["move"].add_argument("--many", action="store_true")
    commands["draft-reply"].add_argument("--body", required=True)
    commands["draft-reply"].add_argument("--all", action="store_true")
    commands["send-draft"].add_argument("draft_id")
    commands["respond"].add_argument("response", choices=["accept", "tentative", "decline"])
    commands["respond"].add_argument("--body")
    for name, required in (("draft-new", True), ("create-event", False)):
        commands[name].add_argument("--account", required=True)
        commands[name].add_argument("--subject", required=True)
        commands[name].add_argument("--body", required=required)
    commands["draft-new"].add_argument("--to", nargs="+", required=True)
    commands["draft-new"].add_argument("--cc", nargs="*", default=[])
    commands["create-event"].add_argument("--start", type=datetime.fromisoformat, required=True)
    commands["create-event"].add_argument("--end", type=datetime.fromisoformat, required=True)
    commands["create-event"].add_argument("--attendees", nargs="*", default=[])
    return top


def audit(mail: Mail, args: argparse.Namespace | None, result: Json) -> None:
    """Append the call's line to <home>/logs/mail.jsonl: the command and the ids it touched.
    Never a subject or a body, so a call whose arguments didn't parse logs no error text."""
    ids = [getattr(args, key, None) for key in ("id", "draft_id")] + getattr(args, "ids", [])
    found = [result, result.get("draft") or {}, *result.get("items", [])]
    ids += [item.get("id") for item in found] + result.get("moved", [])  # COM ids change
    ids = [item_id for item_id in dict.fromkeys(ids) if isinstance(item_id, str)]
    line = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "command": getattr(args, "command", None),
        "account": getattr(args, "account", None) or (ids[0].partition(":")[0] if ids else None),
        "ids": ids,
        "ok": result["ok"],
        **mail.log,
    }
    if not result["ok"]:
        line["error"] = result["error"] if args else "invalid arguments"
    path = mail.home / "logs" / "mail.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as log:
        log.write(json.dumps(line, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):  # subjects in any script, even down a Windows pipe
        sys.stdout.reconfigure(encoding="utf-8")
    home = os.environ.get("ATTACHE_HOME") or HERE.parents[2]  # else the home this is in
    mail, args = Mail(Path(home)), None
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
