# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "icalendar>=6.1",
#     "keyring>=25",
#     "recurring-ical-events>=3.3",
# ]
# ///
"""The diary skill: free slots in the owner's calendars, never what fills them. Each command
prints exactly one JSON object, {"ok": true, ...} or {"ok": false, "error": "..."}, and exits
0 or 1:

    uv run diary.py slots --minutes 45 --count 3 --from 2026-10-27
    uv run diary.py doctor

The settings are in <home>/skills/diary/diary.toml (`setup` copies the example). Busy time is
the union of every [[calendar]]: an Outlook account, read through the mail skill's `agenda`;
or a Google calendar, read from its secret iCal address, which `login` keeps in the keychain
(service "attache-diary", username the calendar's name).

Privacy is structural. The functions that read a calendar return (start, end) pairs only, so
titles, places, attendees and descriptions are dropped where they are parsed; the notes of
[[prefer]] and [[avoid]] are dropped as the file is read. A caller without the owner's rights
gets generic errors, and no command ever prints the iCal address.

Who may ask (the core sets ATTACHE_ROLE and ATTACHE_SENDER, so arguments can't fake them):
with ATTACHE_ROLE absent (the owner, by hand in a terminal) or "owner", anything; with
"member", `slots` only, and only when ATTACHE_SENDER is in share_with.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from datetime import date, datetime, time, timedelta, tzinfo
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent  # <home>/skills/diary/scripts
SERVICE = "attache-diary"  # the keychain service of the secret iCal addresses
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
SOURCES = {"outlook": "account", "google": "name"}  # each source, and the key naming a calendar
FIXES = {
    "outlook": "check it with `attache skill mail doctor`: it needs backend com or graph",
    "google": "store its address again: `attache skill diary login {name}` in a terminal",
}
DEFAULTS = {"share_with": [], "days": list(DAYS[:5]), "hours": "09:00-17:30", "horizon_days": 14}
DEFAULTS |= {"minutes": 30, "gap_minutes": 15, "calendar": [], "prefer": [], "avoid": []}
GRID = timedelta(minutes=30)  # slots start on this grid from the opening hour
LEAD = timedelta(hours=1)  # no slot sooner than this from now
SPREAD, PER_DAY = timedelta(hours=2), 2  # offered slots are this far apart, this many a day
NOT_SHARED = "the diary isn't shared with you"
UNREADABLE = "the diary can't be read just now"
TZ: tzinfo | None = None  # None: this computer's zone, with its summer time; tests set one
Json = dict[str, Any]
Block = tuple[datetime, datetime]
Rule = tuple[str, date, time, time]  # when (a weekday or an ISO date), until, from, to
Slot = tuple[bool, datetime, datetime]  # (not preferred, start, end)


class DiaryError(Exception):
    """A refusal that anyone may see: it says nothing about the calendars."""


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # one JSON object, not usage text and exit 2
        raise DiaryError(f"{self.prog}: {message}")

    def print_help(self, file: Any = None) -> None:  # -h: the usage, as the JSON's error
        raise DiaryError(self.format_help())


# Time


def clock() -> datetime:
    """Now, as an aware local time. Tests replace this."""
    return datetime.now(TZ) if TZ else datetime.now().astimezone()


def local(moment: datetime) -> datetime:
    """A wall-clock time made aware, with the offset its own date has (summer time or not)."""
    return moment.replace(tzinfo=TZ) if TZ else moment.astimezone()


def at(day: date, clock_time: time) -> datetime:
    """That time of day on that day, local."""
    return local(datetime.combine(day, clock_time))


def aware(value: date) -> datetime:
    """A calendar's date or time as an aware datetime: a date is local midnight, and a time
    without a zone (iCal's "floating" time) is local."""
    if not isinstance(value, datetime):
        return at(value, time())
    return value if value.tzinfo else local(value)


def span(text: Any) -> tuple[time, time]:
    """ "09:00-17:30" as two times, earliest first; a ValueError if it isn't that."""
    opens, _, closes = str(text).partition("-")
    first, last = time.fromisoformat(opens.strip()), time.fromisoformat(closes.strip())
    if first >= last:
        raise ValueError(f"hours {text!r}: write HH:MM-HH:MM, earliest first")
    return first, last


# Settings


def load(home: Path) -> Json:
    """diary.toml with the defaults filled in, its [[prefer]] and [[avoid]] as Rules. A
    ValueError says what is wrong."""
    path = home / "skills" / "diary" / "diary.toml"
    if not path.is_file():
        raise FileNotFoundError(f"no diary yet: run `attache skill diary setup`, then edit {path}")
    try:  # utf-8-sig: Notepad and PowerShell 5 may start the file with a BOM
        cfg = DEFAULTS | tomllib.loads(path.read_text(encoding="utf-8-sig"))
        check(cfg)
        cfg["prefer"], cfg["avoid"] = rules(cfg["prefer"]), rules(cfg["avoid"])
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise ValueError(f"{path.name}: {error}") from None
    return cfg


def check(cfg: Json) -> None:
    """A ValueError naming the first setting that is wrong."""
    if not isinstance(cfg["share_with"], list):
        raise ValueError('share_with must be a list of people, such as ["dan"]')
    if not isinstance(cfg["days"], list) or not set(cfg["days"]) <= set(DAYS):
        raise ValueError(f"days must be a list from {', '.join(DAYS)}")
    span(cfg["hours"])
    for key, low, high in (("horizon_days", 1, 31), ("minutes", 15, 240), ("gap_minutes", 0, 120)):
        if type(cfg[key]) is not int or not low <= cfg[key] <= high:
            raise ValueError(f"{key} must be a whole number from {low} to {high}")
    if not cfg["calendar"]:
        raise ValueError("add a [[calendar]]: without one, every time would look free")
    for each in cfg["calendar"]:
        key = SOURCES.get(each.get("source"))
        if not key or not isinstance(each.get(key), str):
            raise ValueError('[[calendar]]: source "outlook" with account, or "google" with name')


def rules(entries: list[Json]) -> list[Rule]:
    """[[prefer]] or [[avoid]] entries as Rules: all day unless they give hours, and with no
    end unless they give until. Their notes are left behind here."""
    found = []
    for entry in entries:
        when = str(entry.get("when", "")).strip().lower()
        if when not in DAYS:  # else a date, written the one way covered() compares
            when = date.fromisoformat(when).isoformat()
        until = date.fromisoformat(str(entry["until"])) if "until" in entry else date.max
        hours = span(entry["hours"]) if "hours" in entry else (time.min, time.max)
        found.append((when, until, *hours))
    return found


def covered(found: list[Rule], day: date) -> list[Block]:
    """The times on this day that these rules cover."""
    names = (DAYS[day.weekday()], day.isoformat())
    return [
        (at(day, lo), at(day, hi))
        for when, until, lo, hi in found
        if when in names and day <= until
    ]


# Access


def owner() -> bool:
    """Whether the caller has the owner's rights: by hand (no ATTACHE_ROLE), or an owner turn."""
    return os.environ.get("ATTACHE_ROLE", "owner") == "owner"


def shared(cfg: Json) -> bool:
    """Whether this caller may be offered slots: the owner, or a member in share_with (the owner
    in a group chat acts as a member, and may still)."""
    sender = os.environ.get("ATTACHE_SENDER")
    member = os.environ.get("ATTACHE_ROLE") == "member"
    return owner() or (member and (sender == "owner" or sender in cfg["share_with"]))


# Reading calendars: each reader returns (start, end) pairs and nothing else.


def outlook_busy(home: Path, account: str, days: int) -> list[Block]:
    """The account's busy times, from the mail skill's agenda. Free and working-elsewhere
    items don't count; an item without show_as does."""
    script = home / "skills" / "mail" / "scripts" / "mail.py"
    argv = ["uv", "run", str(script), "agenda", "--account", account, "--days", str(days)]
    done = subprocess.run(argv, capture_output=True, encoding="utf-8", timeout=300)
    try:  # never its output in an error: that is the calendar itself
        found = json.loads(done.stdout)
    except ValueError:
        raise RuntimeError(f"the mail skill's agenda failed (exit {done.returncode})") from None
    if not found.get("ok"):
        raise RuntimeError(f"mail account {account}: {found.get('error')}")
    blocks = []
    for item in found["items"]:
        if item.get("account") == account and item.get("show_as") not in ("free", "elsewhere"):
            start, end = datetime.fromisoformat(item["start"]), datetime.fromisoformat(item["end"])
            blocks.append((aware(start), aware(end)))
    return blocks


class HttpsOnly(urllib.request.HTTPHandler):
    """Takes the place of plain http in the opener, so not even a redirect leaves https."""

    def http_open(self, req: Any) -> Any:
        raise urllib.error.URLError("not https")


def fetch(url: str) -> bytes:
    """The iCal file at a secret https address. Its errors never include the address. Tests
    replace this."""
    if not url.startswith("https://"):
        raise ValueError("that isn't an https:// address: copy the Secret address in iCal format")
    try:
        with urllib.request.build_opener(HttpsOnly).open(url, timeout=30) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        raise OSError(f"the calendar's address answered HTTP {error.code}") from None
    except Exception as error:
        raise OSError(f"the calendar can't be reached ({type(error).__name__})") from None


def ical_busy(data: bytes, first: datetime, last: datetime) -> list[Block]:
    """The busy times in an iCal file between first and last, recurrences expanded.
    Transparent (free) and cancelled events don't count; an all-day event blocks its days."""
    import icalendar
    import recurring_ical_events

    try:  # never the parser's message in an error: it may quote the file
        events = recurring_ical_events.of(icalendar.Calendar.from_ical(data)).between(first, last)
        blocks = []
        for event in events:
            free = str(event.get("TRANSP", "")).upper() == "TRANSPARENT"
            if free or str(event.get("STATUS", "")).upper() == "CANCELLED":
                continue
            start, end = aware(event.start), aware(event.end)
            if not isinstance(event.start, datetime):  # all day: older versions end it at once
                end = max(end, aware(event.start + timedelta(days=1)))
            blocks.append((start, end))
        return blocks
    except Exception:
        raise ValueError("the calendar didn't read as iCal") from None


def google_busy(name: str, first: datetime, last: datetime) -> list[Block]:
    """The busy times in a Google calendar, from its secret address in the keychain."""
    import keyring

    if not (url := keyring.get_password(SERVICE, name)):
        raise LookupError(f"no iCal address for {name}: run `attache skill diary login {name}`")
    return ical_busy(fetch(url), first, last)


def read(home: Path, calendar: Json, now: datetime, last: date) -> list[Block]:
    """One [[calendar]]'s busy times from now until the start of `last`."""
    if calendar["source"] == "outlook":
        return outlook_busy(home, calendar["account"], (last - now.date()).days)
    return google_busy(calendar["name"], now, at(last, time()))


# Slots


def free(
    cfg: Json, blocks: list[Block], now: datetime, minutes: int, first: date, last: date
) -> list[Slot]:
    """Every slot from first until last: on working days and hours, on the grid, clear of busy
    time (widened by gap_minutes) and of [[avoid]], and at least LEAD from now. Each is
    (False if preferred, start, end), so preferred slots sort first."""
    length, gap = timedelta(minutes=minutes), timedelta(minutes=cfg["gap_minutes"])
    opens, closes = span(cfg["hours"])
    widened = [(start - gap, end + gap) for start, end in blocks]
    found = []
    for offset in range((last - first).days):
        day = first + timedelta(days=offset)
        if DAYS[day.weekday()] not in cfg["days"]:
            continue
        blocked, preferred = widened + covered(cfg["avoid"], day), covered(cfg["prefer"], day)
        start = datetime.combine(day, opens)
        while start + length <= datetime.combine(day, closes):
            begins, ends = local(start), local(start + length)  # each with its own offset
            clear = all(ends <= busy_from or begins >= busy_to for busy_from, busy_to in blocked)
            if clear and begins >= now + LEAD:
                inside = any(lo <= begins and ends <= hi for lo, hi in preferred)
                found.append((not inside, begins, ends))
            start += GRID
    return found


def spread(found: list[Slot], count: int) -> list[Block]:
    """The first `count` slots, preferred first, then the rest, each earliest first (the sort
    is stable); at most PER_DAY a day, SPREAD apart."""
    chosen: list[Block] = []
    for _, begins, ends in sorted(found, key=lambda slot: slot[0]):
        same_day = [start for start, _ in chosen if start.date() == begins.date()]
        if len(same_day) < PER_DAY and all(abs(begins - start) >= SPREAD for start in same_day):
            chosen.append((begins, ends))
        if len(chosen) == count:
            break
    return chosen


# The commands: each takes (home, args) and returns the result's fields.


def slots(home: Path, args: argparse.Namespace) -> Json:
    for name, low, high in (("minutes", 15, 240), ("count", 1, 10), ("days", 1, 31)):
        if getattr(args, name) is not None and not low <= getattr(args, name) <= high:
            raise DiaryError(f"--{name} must be from {low} to {high}")
    cfg = load(home)
    if not shared(cfg):
        raise DiaryError(NOT_SHARED)
    now, horizon = clock(), timedelta(days=cfg["horizon_days"])
    first = max(args.start or now.date(), now.date())
    last = min(first + timedelta(days=args.days or cfg["horizon_days"]), now.date() + horizon)
    if last <= first:
        raise DiaryError(f"the diary looks {cfg['horizon_days']} days ahead at most")
    blocks = [block for calendar in cfg["calendar"] for block in read(home, calendar, now, last)]
    minutes = args.minutes or cfg["minutes"]
    chosen = spread(free(cfg, blocks, now, minutes, first, last), args.count)
    shown = []
    for begins, ends in chosen:
        label = f"{begins:%a} {begins.day} {begins:%b}, {begins:%H:%M}\u2013{ends:%H:%M}"
        shown.append({"start": begins.isoformat(), "end": ends.isoformat(), "label": label})
    return {"timezone": now.tzname(), "minutes": minutes, "slots": shown}


def setup(home: Path, args: argparse.Namespace) -> Json:
    target = home / "skills" / "diary" / "diary.toml"
    created = not target.exists()
    if created:
        shutil.copyfile(HERE.parent / "diary.example.toml", target)
    return {"path": str(target), "created": created}


def login(home: Path, args: argparse.Namespace) -> Json:
    # Never the agent: the address must not pass through a chat, and on Windows its NUL stdin
    # passes isatty(). The core sets ATTACHE_ROLE for every script it runs.
    if {"ATTACHE_ROLE", "ATTACHE_WEB_URL"} & set(os.environ) or not sys.stdin.isatty():
        raise DiaryError(f"run this yourself in a terminal: attache skill diary login {args.name}")
    import keyring

    url = getpass.getpass(f"Secret address in iCal format for {args.name} (not shown): ").strip()
    now = clock()
    blocks = ical_busy(fetch(url), now, now + timedelta(days=14))  # it works before it is kept
    keyring.set_password(SERVICE, args.name, url)
    return {"calendar": args.name, "saved": True, "busy_blocks": len(blocks)}


def doctor(home: Path, args: argparse.Namespace) -> Json:
    cfg, now = load(home), clock()
    last = now.date() + timedelta(days=cfg["horizon_days"])
    found = []
    for calendar in cfg["calendar"]:
        entry = {"source": calendar["source"], "name": calendar[SOURCES[calendar["source"]]]}
        try:
            entry |= {"ok": True, "busy_blocks": len(read(home, calendar, now, last))}
        except Exception as error:
            fix = FIXES[calendar["source"]].format(name=entry["name"])
            entry |= {"ok": False, "error": str(error) or type(error).__name__, "fix": fix}
        found.append(entry)
    return {"calendars": found}


COMMANDS = {"slots": slots, "setup": setup, "login": login, "doctor": doctor}


def parser() -> Parser:
    """The commands and their arguments. No argument holds a time of day: members can't pass
    ":" through the script tool."""
    top = Parser(prog="diary.py", description="Free slots in the diary; each prints one JSON.")
    sub = top.add_subparsers(dest="command", required=True)
    commands = {name: sub.add_parser(name) for name in COMMANDS}
    for name in ("--minutes", "--days"):
        commands["slots"].add_argument(name, type=int)
    commands["slots"].add_argument("--count", type=int, default=3)
    commands["slots"].add_argument("--from", dest="start", type=date.fromisoformat)
    commands["login"].add_argument("name")
    return top


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):  # the labels' dash, even down a Windows pipe
        sys.stdout.reconfigure(encoding="utf-8")
    # $ATTACHE_HOME, which Attaché sets; else the home this script is installed in
    home = Path(os.environ.get("ATTACHE_HOME") or HERE.parents[2])
    try:
        args = parser().parse_args(argv)
        if args.command != "slots" and not owner():
            raise DiaryError(f"only the owner can run {args.command}")
        result = {"ok": True, **COMMANDS[args.command](home, args)}
    except DiaryError as error:
        result = {"ok": False, "error": str(error)}
    except Exception as error:  # details are the owner's: they name calendars and accounts
        detail = str(error) or type(error).__name__
        result = {"ok": False, "error": detail if owner() else UNREADABLE}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
