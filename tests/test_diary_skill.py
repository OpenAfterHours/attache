"""The diary skill (diary.py): free slots only, who may ask, the owner's preferences, and both
calendar sources, against a fake `mail.py agenda`, a fake keychain and a fake fetch. Nothing
leaves this machine, and no calendar's content reaches the output."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import urllib.error
from datetime import UTC, date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from attache import skills

FOLDER = skills.BUNDLED / "diary"
URL = "https://calendar.google.com/calendar/ical/me%40gmail.com/private-0a1b2c3d/basic.ics"
SECRETS = ("Project Nightingale", "Board room 4", "ceo@example.com", "Chief Exec", "Layoffs")
NOTE = "interview at Acme"
CONFIG = """
share_with = ["dan"]

[[calendar]]
source = "outlook"
account = "work"
"""
GOOGLE = """
share_with = ["dan"]

[[calendar]]
source = "google"
name = "personal"
"""


class London(tzinfo):
    """Europe/London in autumn 2026, without tzdata: summer time until 02:00 on 25 October."""

    def utcoffset(self, dt: datetime | None) -> timedelta:
        summer = dt is not None and dt.replace(tzinfo=None) < datetime(2026, 10, 25, 2)
        return timedelta(hours=1 if summer else 0)

    def dst(self, dt: datetime | None) -> timedelta:
        return self.utcoffset(dt)

    def tzname(self, dt: datetime | None) -> str:
        return "BST" if self.utcoffset(dt) else "GMT"


LONDON = London()


def at(*fields: int) -> datetime:
    return datetime(*fields, tzinfo=LONDON)


NOW = at(2026, 10, 21, 10, 10)  # a Wednesday; the clocks go back on Sunday the 25th


def load_diary() -> ModuleType:
    path = FOLDER / "scripts" / "diary.py"
    spec = importlib.util.spec_from_file_location("attache_diary", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def diary(home: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """The script, run by the owner by hand (no ATTACHE_ROLE), in London, at NOW, under uv, in a
    chat's files folder as the script tool runs it."""
    module = load_diary()
    monkeypatch.setattr(module, "TZ", LONDON)
    monkeypatch.setattr(module, "clock", lambda: NOW)
    for name in ("ATTACHE_ROLE", "ATTACHE_SENDER", "ATTACHE_CHAT", "ATTACHE_WEB_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("UV", str(home.parent / "bin" / "uv.exe"))  # uv sets it for its children
    chat = home / "files" / "email-dan-example-com"
    chat.mkdir(parents=True)
    monkeypatch.chdir(chat)  # and back afterwards, wherever main went
    return module


def configure(home: Path, text: str = CONFIG) -> None:
    path = home / "skills" / "diary" / "diary.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def as_member(monkeypatch: pytest.MonkeyPatch, sender: str, role: str = "member") -> None:
    """What the core sets for a member's turn (contract C1)."""
    monkeypatch.setenv("ATTACHE_ROLE", role)
    monkeypatch.setenv("ATTACHE_SENDER", sender)


def output(diary: ModuleType, capsys: Any, *argv: str) -> str:
    """Everything the command printed: one JSON line, its exit code agreeing with "ok"."""
    code = diary.main(list(argv))
    out = capsys.readouterr().out
    assert out.count("\n") == 1 and code == (0 if json.loads(out)["ok"] else 1)
    return out


def run(diary: ModuleType, capsys: Any, *argv: str) -> dict[str, Any]:
    return json.loads(output(diary, capsys, *argv))


def event(start: str, end: str, account: str = "work", **fields: Any) -> dict[str, Any]:
    """An agenda item as mail.py prints it, with everything a confidential invite carries."""
    item = {"id": f"{account}:AAMkAD1", "account": account, "untrusted": True, "start": start}
    item |= {"end": end, "subject": "Project Nightingale", "location": "Board room 4"}
    item |= {"organizer": "Chief Exec", "attendees": ["ceo@example.com"], "is_recurring": False}
    return item | fields


class Agenda:
    """subprocess.run, answering as `uv run mail.py agenda` does; it records each argv and the
    folder it runs in."""

    def __init__(self, items: list[dict[str, Any]], stdout: str | None = None, code: int = 0):
        self.stdout = json.dumps({"ok": True, "items": items}) if stdout is None else stdout
        self.code, self.calls, self.folders = code, [], []

    def __call__(self, argv: list[str], **options: Any) -> SimpleNamespace:
        self.calls.append(argv)
        self.folders.append(options.get("cwd"))
        return SimpleNamespace(returncode=self.code, stdout=self.stdout, stderr="")


def agenda(monkeypatch: pytest.MonkeyPatch, diary: ModuleType, *items: Any, **kw: Any) -> Agenda:
    fake = Agenda(list(items), **kw)
    monkeypatch.setattr(diary.subprocess, "run", fake)
    return fake


def starts(result: dict[str, Any]) -> list[str]:
    return [slot["start"] for slot in result["slots"]]


# The skill's files


def test_the_header_declares_the_dependencies() -> None:
    text = (FOLDER / "scripts" / "diary.py").read_text(encoding="utf-8")
    header = re.search(r"# /// script\n(.*?)# ///", text, re.S)
    assert header
    for package in ("icalendar", "recurring-ical-events", "keyring"):
        assert f'"{package}>=' in header[1], package


def test_skill_md_is_for_members_and_puts_their_part_first() -> None:
    text = (FOLDER / "SKILL.md").read_text(encoding="utf-8")
    meta, body = skills.parse(text)
    assert meta["metadata"]["attache-roles"] == "member"
    owner_part = body.index("The rest of this file is for the owner")
    assert body.index('args: ["slots"]') < owner_part < body.index("[[avoid]]")
    member_part = body[:owner_part]
    for rule in ("Never say why", "Never promise", "`request"):
        assert rule in member_part, rule
    assert "private-" not in text and "ical/" not in text  # nothing secret: members read it all


def test_setup_copies_the_example_once(diary: ModuleType, home: Path, capsys: Any) -> None:
    (home / "skills" / "diary").mkdir(parents=True)
    first = run(diary, capsys, "setup")
    assert first["created"] and Path(first["path"]) == home / "skills" / "diary" / "diary.toml"
    assert not run(diary, capsys, "setup")["created"]
    cfg = diary.load(home)  # the example works as it is
    assert cfg["calendar"] == [{"source": "outlook", "account": "outlook"}]
    assert cfg["share_with"] == [] and cfg["prefer"] == [] and cfg["avoid"] == []
    text = (FOLDER / "diary.example.toml").read_text(encoding="utf-8")
    assert "https://" not in text
    configure(home, re.sub(r"^# (\[\[|\w+ = )", r"\1", text, flags=re.M))  # all of it, in use
    cfg = diary.load(home)
    assert [each["source"] for each in cfg["calendar"]] == ["outlook", "google"]
    assert len(cfg["prefer"]) == len(cfg["avoid"]) == 1


# Who may ask


def test_the_owner_may_ask(diary: ModuleType, home: Path, capsys: Any, monkeypatch) -> None:
    configure(home)
    agenda(monkeypatch, diary)
    assert run(diary, capsys, "slots")["ok"]  # by hand: no ATTACHE_ROLE
    monkeypatch.setenv("ATTACHE_ROLE", "owner")
    assert run(diary, capsys, "slots")["ok"]
    as_member(monkeypatch, "owner")  # the owner in a group chat acts as a member, and may still
    assert run(diary, capsys, "slots")["ok"]


def test_members_only_in_share_with(diary: ModuleType, home: Path, capsys: Any, monkeypatch):
    configure(home)
    fake = agenda(monkeypatch, diary)
    as_member(monkeypatch, "dan")
    assert run(diary, capsys, "slots")["ok"]
    calls = len(fake.calls)
    for sender, role in (("sam", "member"), ("dan", "guest")):
        as_member(monkeypatch, sender, role)
        assert run(diary, capsys, "slots") == {"ok": False, "error": diary.NOT_SHARED}
    assert len(fake.calls) == calls  # refused before any calendar is read


def test_members_cant_set_up_log_in_or_run_doctor(
    diary: ModuleType, home: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    as_member(monkeypatch, "dan")
    for command in (["setup"], ["login", "personal"], ["doctor"]):
        result = run(diary, capsys, *command)
        assert result == {"ok": False, "error": f"only the owner can run {command[0]}"}
    assert not (home / "skills" / "diary" / "diary.toml").exists()


def test_a_member_never_sees_details(diary: ModuleType, home: Path, capsys: Any, monkeypatch):
    as_member(monkeypatch, "dan")
    assert run(diary, capsys, "slots")["error"] == diary.UNREADABLE  # no diary.toml yet
    configure(home, CONFIG + f'\n[[avoid]]\nwhen = "someday"\nnote = "{NOTE}"\n')
    assert run(diary, capsys, "slots")["error"] == diary.UNREADABLE
    monkeypatch.delenv("ATTACHE_ROLE")
    error = run(diary, capsys, "slots")["error"]  # the owner is told what is wrong
    assert error.startswith("diary.toml:") and "someday" in error and NOTE not in error


# Slots


def test_working_days_hours_and_an_hour_from_now(diary: ModuleType, home: Path) -> None:
    configure(home)
    found = diary.free(diary.load(home), [], NOW, 30, date(2026, 10, 21), date(2026, 10, 28))
    begins = [start for _, start, _ in found]
    assert begins[0] == at(2026, 10, 21, 11, 30)  # 11:00 is sooner than an hour from 10:10
    assert {start.weekday() for start in begins} == {0, 1, 2, 3, 4}  # no weekend
    assert all(time(9) <= start.time() and end.time() <= time(17, 30) for _, start, end in found)
    thursday = [start.time() for start in begins if start.day == 22]
    assert thursday[0] == time(9) and thursday[-1] == time(17) and len(thursday) == 17  # grid
    assert all(later for later, _, _ in found)  # nothing preferred, so all sort later


def test_busy_time_is_kept_clear_with_its_gap(diary: ModuleType, home: Path) -> None:
    configure(home, "gap_minutes = 15\n" + CONFIG)
    cfg, thursday = diary.load(home), (date(2026, 10, 22), date(2026, 10, 23))
    meeting = (at(2026, 10, 22, 13, 0), at(2026, 10, 22, 14, 0))
    begins = {start.time() for _, start, _ in diary.free(cfg, [meeting], NOW, 30, *thursday)}
    assert {time(12), time(14, 30)} <= begins
    assert not {time(12, 30), time(13), time(13, 30), time(14)} & begins
    day = (at(2026, 10, 22, 0, 0), at(2026, 10, 23, 0, 0))  # an all-day event from Outlook
    assert diary.free(cfg, [day], NOW, 30, *thursday) == []


PREFERENCES = f"""
[[avoid]]
when = "2026-10-26"
note = "{NOTE}"

[[avoid]]
when = "thu"
hours = "09:00-12:00"
until = "2026-12-31"

[[avoid]]
when = "wed"
until = "2026-10-01"

[[prefer]]
when = "2026-10-27"
hours = "13:00-17:00"

[[prefer]]
when = "fri"
until = "2026-10-20"
"""


def test_avoid_and_prefer(diary: ModuleType, home: Path) -> None:
    configure(home, CONFIG + PREFERENCES)
    cfg = diary.load(home)
    found = diary.free(cfg, [], NOW, 30, date(2026, 10, 21), date(2026, 10, 30))
    assert not [start for _, start, _ in found if start.day == 26]  # avoided by date
    thursday = [start.time() for _, start, _ in found if start.day == 22]
    assert thursday[0] == time(12)  # avoided by weekday, until the end of the year
    assert [start for _, start, _ in found if start.day == 21]  # that avoid has expired
    preferred = [start for later, start, _ in found if not later]
    assert preferred[0] == at(2026, 10, 27, 13, 0) and preferred[-1] == at(2026, 10, 27, 16, 30)
    picked = diary.spread(found, 4)  # preferred first, then the earliest; Friday's has expired
    assert [start for start, _ in picked] == [
        at(2026, 10, 27, 13, 0),
        at(2026, 10, 27, 15, 0),
        at(2026, 10, 21, 11, 30),
        at(2026, 10, 21, 13, 30),
    ]


def test_slots_are_spread_out(diary: ModuleType, home: Path) -> None:
    configure(home)
    found = diary.free(diary.load(home), [], NOW, 60, date(2026, 10, 21), date(2026, 11, 4))
    picked = [start for start, _ in diary.spread(found, 10)]
    assert len(picked) == 10 and picked == sorted(picked)
    for day in {start.date() for start in picked}:
        same = [start for start in picked if start.date() == day]
        assert len(same) <= 2 and (len(same) < 2 or same[1] - same[0] >= timedelta(hours=2))
    assert picked[:3] == [at(2026, 10, 21, 11, 30), at(2026, 10, 21, 13, 30), at(2026, 10, 22, 9)]


def test_local_times_follow_summer_time(diary: ModuleType, home: Path, capsys, monkeypatch):
    configure(home)
    agenda(monkeypatch, diary)
    friday = run(diary, capsys, "slots", "--from", "2026-10-23", "--count", "1")
    assert friday["slots"] == [
        {
            "start": "2026-10-23T09:00:00+01:00",
            "end": "2026-10-23T09:30:00+01:00",
            "label": "Fri 23 Oct, 09:00\u201309:30 BST",
        }
    ]
    monday = run(diary, capsys, "slots", "--from", "2026-10-26", "--count", "1", "--minutes", "45")
    assert set(monday) == {"ok", "minutes", "slots"} and monday["minutes"] == 45
    assert starts(monday) == ["2026-10-26T09:00:00+00:00"]  # 09:00 still, after the clocks change
    assert monday["slots"][0]["label"] == "Mon 26 Oct, 09:00\u201309:45 GMT"  # asked in BST


def test_a_slots_zone_is_short_on_windows_too(diary: ModuleType) -> None:
    for name, offset, shown in (
        ("BST", timedelta(hours=1), "BST"),
        ("GMT Daylight Time", timedelta(hours=1), "UTC+1"),  # Windows' names
        ("GMT Standard Time", timedelta(0), "UTC+0"),
        ("India Standard Time", timedelta(hours=5, minutes=30), "UTC+5:30"),
        ("-03", timedelta(hours=-3), "UTC-3"),
        ("Newfoundland Daylight Time", -timedelta(hours=2, minutes=30), "UTC-2:30"),
    ):
        assert diary.zone(datetime(2026, 10, 21, 9, tzinfo=timezone(offset, name))) == shown


def test_argument_bounds(diary: ModuleType, home: Path, capsys: Any, monkeypatch) -> None:
    configure(home)
    agenda(monkeypatch, diary)
    for bad in (
        ["--minutes", "14"],
        ["--minutes", "241"],
        ["--count", "0"],
        ["--count", "11"],
        ["--days", "0"],
        ["--days", "32"],
        ["--minutes", "half"],
        ["--from", "next week"],
        ["--from", "2026-11-04"],  # past the 14 days the diary looks ahead
        ["--at", "14"],
    ):
        assert not run(diary, capsys, "slots", *bad)["ok"], bad
    widest = run(diary, capsys, "slots", "--minutes", "240", "--count", "10", "--days", "31")
    assert len(widest["slots"]) == 10 and max(starts(widest)) < "2026-11-04"  # within 14 days
    shortest = run(diary, capsys, "slots", "--minutes", "15", "--count", "1", "--days", "1")
    assert starts(shortest) == ["2026-10-21T11:30:00+01:00"]
    past = run(diary, capsys, "slots", "--from", "2026-10-01", "--count", "1")
    assert starts(past) == ["2026-10-21T11:30:00+01:00"]  # from today, never before


# Outlook, through the mail skill's agenda


def test_outlook_busy_time_and_nothing_else(diary: ModuleType, home: Path, capsys, monkeypatch):
    configure(home, "gap_minutes = 0\n" + CONFIG + PREFERENCES)  # Thursday mornings are avoided
    thursday = [  # each would leave a slot if it didn't count
        event("2026-10-22T12:00:00+01:00", "2026-10-22T13:00:00+01:00", show_as="busy"),
        event("2026-10-22T13:00:00+01:00", "2026-10-22T14:00:00+01:00", show_as="tentative"),
        event("2026-10-22T14:00:00+01:00", "2026-10-22T15:00:00+01:00", show_as="oof"),
        event("2026-10-22T15:00:00+01:00", "2026-10-22T17:30:00+01:00"),  # no show_as: busy
        event("2026-10-23T09:00:00+01:00", "2026-10-23T17:30:00+01:00", show_as="free"),
        event("2026-10-23T09:00:00+01:00", "2026-10-23T17:30:00+01:00", show_as="elsewhere"),
        event("2026-10-23T09:00:00+01:00", "2026-10-23T17:30:00+01:00", account="home"),
    ]
    fake = agenda(monkeypatch, diary, *thursday)
    out = output(diary, capsys, "slots", "--from", "2026-10-22", "--days", "2", "--count", "10")
    mail = str(home / "skills" / "mail" / "scripts" / "mail.py")
    uv = str(home.parent / "bin" / "uv.exe")  # the uv this runs under: $UV
    assert fake.calls == [[uv, "run", mail, "agenda", "--account", "work", "--days", "3"]]
    assert fake.folders == [home]  # never the chat's files folder
    result = json.loads(out)
    assert all(set(slot) == {"start", "end", "label"} for slot in result["slots"])
    assert not [start for start in starts(result) if start.startswith("2026-10-22")]
    assert starts(result)[:2] == ["2026-10-23T09:00:00+01:00", "2026-10-23T11:00:00+01:00"]
    for secret in (*SECRETS, NOTE, "AAMkAD1", "tentative"):
        assert secret not in out, secret


def test_uv_is_never_looked_for_in_the_chats_folder(
    diary: ModuleType, home: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(home)
    fake = agenda(monkeypatch, diary)
    monkeypatch.delenv("UV")  # run by hand with python: look uv up on PATH, from the home folder
    looked = []
    found = str(home.parent / "tools" / "uv.exe")
    monkeypatch.setattr(diary.shutil, "which", lambda name: looked.append(Path.cwd()) or found)
    assert run(diary, capsys, "slots")["ok"]
    assert looked == [home] and fake.calls[0][0] == found and fake.folders == [home]
    monkeypatch.setattr(diary.shutil, "which", lambda name: None)
    assert run(diary, capsys, "slots")["error"].startswith("uv isn't on PATH")


def test_outlook_errors_say_nothing_of_the_calendar(
    diary: ModuleType, home: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(home)
    agenda(monkeypatch, diary, stdout="Traceback ... Project Nightingale, Board room 4", code=1)
    out = output(diary, capsys, "slots")
    assert json.loads(out)["error"] == "the mail skill's agenda failed (exit 1)"
    as_member(monkeypatch, "dan")
    assert run(diary, capsys, "slots")["error"] == diary.UNREADABLE
    gone = json.dumps({"ok": False, "error": "Outlook isn't running"})
    agenda(monkeypatch, diary, stdout=gone, code=1)
    assert run(diary, capsys, "slots")["error"] == diary.UNREADABLE
    monkeypatch.delenv("ATTACHE_ROLE")
    assert run(diary, capsys, "slots")["error"] == "mail account work: Outlook isn't running"


# Google, from the secret iCal address

ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Attache tests//EN
BEGIN:VEVENT
UID:standup@example.com
DTSTART:20261022T090000Z
DTEND:20261022T100000Z
RRULE:FREQ=WEEKLY;COUNT=3
SUMMARY:Project Nightingale
LOCATION:Board room 4
DESCRIPTION:Layoffs
ATTENDEE;CN=Chief Exec:mailto:ceo@example.com
END:VEVENT
BEGIN:VEVENT
UID:focus@example.com
DTSTART:20261023T080000Z
DTEND:20261023T163000Z
TRANSP:TRANSPARENT
SUMMARY:Project Nightingale focus time
END:VEVENT
BEGIN:VEVENT
UID:offsite@example.com
DTSTART:20261027T090000Z
DTEND:20261027T173000Z
STATUS:CANCELLED
SUMMARY:Project Nightingale offsite
END:VEVENT
BEGIN:VEVENT
UID:holiday@example.com
DTSTART;VALUE=DATE:20261026
SUMMARY:Layoffs
END:VEVENT
END:VCALENDAR
"""


@pytest.fixture
def keychain(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], str]:
    """A keychain in memory, as `keyring` is imported inside functions."""
    stored: dict[tuple[str, str], str] = {}
    module = SimpleNamespace(
        get_password=lambda service, name: stored.get((service, name)),
        set_password=lambda service, name, value: stored.__setitem__((service, name), value),
    )
    monkeypatch.setitem(sys.modules, "keyring", module)
    return stored


def utc(blocks: list[tuple[datetime, datetime]]) -> set[tuple[datetime, datetime]]:
    return {(start.astimezone(UTC), end.astimezone(UTC)) for start, end in blocks}


def test_ical_recurrences_and_what_doesnt_count(diary: ModuleType) -> None:
    pytest.importorskip("recurring_ical_events")
    blocks = diary.ical_busy(ICS, NOW, at(2026, 11, 4, 0, 0))
    assert utc(blocks) == {
        (datetime(2026, 10, 22, 9, tzinfo=UTC), datetime(2026, 10, 22, 10, tzinfo=UTC)),
        (datetime(2026, 10, 29, 9, tzinfo=UTC), datetime(2026, 10, 29, 10, tzinfo=UTC)),
        (datetime(2026, 10, 26, 0, tzinfo=UTC), datetime(2026, 10, 27, 0, tzinfo=UTC)),
    }  # the third standup is past the window; the free and the cancelled events don't count


def test_google_slots_reveal_nothing(
    diary: ModuleType, home: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch, keychain: dict
) -> None:
    pytest.importorskip("recurring_ical_events")
    configure(home, GOOGLE)
    keychain[("attache-diary", "personal")] = URL
    fetched = []
    monkeypatch.setattr(diary, "fetch", lambda url: fetched.append(url) or ICS)
    as_member(monkeypatch, "dan")
    out = output(diary, capsys, "slots", "--from", "2026-10-22", "--days", "6", "--count", "10")
    assert fetched == [URL]
    found = starts(json.loads(out))
    thursday = [start for start in found if start.startswith("2026-10-22")]
    assert thursday == ["2026-10-22T09:00:00+01:00", "2026-10-22T11:30:00+01:00"]  # 10-11 busy
    assert not [start for start in found if start.startswith("2026-10-26")]  # all day
    assert [start for start in found if start.startswith("2026-10-23")]  # transparent
    assert [start for start in found if start.startswith("2026-10-27")]  # cancelled
    for secret in (*SECRETS, URL, "private-0a1b2c3d", "standup"):
        assert secret not in out, secret


def test_google_errors_never_show_the_address_or_the_file(
    diary: ModuleType, home: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch, keychain: dict
) -> None:
    configure(home, GOOGLE)
    hint = "no iCal address for personal: run `attache skill diary login personal`"
    assert run(diary, capsys, "slots")["error"] == hint
    keychain[("attache-diary", "personal")] = URL

    def unreachable(url: str) -> bytes:
        raise OSError("the calendar's address answered HTTP 404")

    monkeypatch.setattr(diary, "fetch", unreachable)
    out = output(diary, capsys, "slots")
    assert "HTTP 404" in out and URL not in out
    as_member(monkeypatch, "dan")
    assert run(diary, capsys, "slots")["error"] == diary.UNREADABLE
    pytest.importorskip("recurring_ical_events")
    monkeypatch.delenv("ATTACHE_ROLE")
    broken = b"BEGIN:VCALENDAR\nSUMMARY Project Nightingale, Board room 4\nEND:VCALENDAR\n"
    monkeypatch.setattr(diary, "fetch", lambda url: broken)
    out = output(diary, capsys, "slots")
    assert json.loads(out)["error"] == "the calendar didn't read as iCal"


def test_fetch_is_https_only_and_never_names_the_address(
    diary: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError, match="https") as refused:
        diary.fetch(URL.replace("https:", "http:"))
    assert "private-" not in str(refused.value)
    opener = diary.urllib.request.build_opener(diary.HttpsOnly)
    with pytest.raises(urllib.error.URLError, match="not https"):  # nor through a redirect
        opener.open("http://127.0.0.1:9/basic.ics", timeout=1)

    class Opener:
        def __init__(self, error: Exception) -> None:
            self.error = error

        def open(self, url: str, timeout: float) -> Any:
            raise self.error

    for error, expected in (
        (urllib.error.HTTPError(URL, 404, f"Not Found: {URL}", {}, None), "answered HTTP 404"),
        (ValueError(f"URL can't contain control characters: {URL}"), "can't be reached"),
    ):
        monkeypatch.setattr(diary.urllib.request, "build_opener", lambda *h, e=error: Opener(e))
        with pytest.raises(OSError) as failed:
            diary.fetch(URL)
        assert expected in str(failed.value) and "private-" not in str(failed.value)


# login and doctor


def test_login_is_for_a_terminal_and_keeps_the_address(
    diary: ModuleType, home: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch, keychain: dict
) -> None:
    monkeypatch.setattr(diary.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setenv("ATTACHE_ROLE", "owner")  # the agent, on the owner's turn
    assert "in a terminal" in run(diary, capsys, "login", "personal")["error"]
    monkeypatch.delenv("ATTACHE_ROLE")
    monkeypatch.setattr(diary.getpass, "getpass", lambda prompt: URL.replace("https:", "http:"))
    assert "https://" in run(diary, capsys, "login", "personal")["error"] and not keychain
    monkeypatch.setattr(diary.getpass, "getpass", lambda prompt: f"  {URL}\n")
    monkeypatch.setattr(diary, "fetch", lambda url: b"ics")
    monkeypatch.setattr(diary, "ical_busy", lambda data, first, last: [(first, last)])
    out = output(diary, capsys, "login", "personal")
    assert json.loads(out) == {"ok": True, "calendar": "personal", "saved": True, "busy_blocks": 1}
    assert keychain == {("attache-diary", "personal"): URL} and URL not in out
    monkeypatch.setattr(diary.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    assert "in a terminal" in run(diary, capsys, "login", "personal")["error"]


def test_doctor_counts_and_fixes(
    diary: ModuleType, home: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch, keychain: dict
) -> None:
    configure(home, CONFIG + PREFERENCES + GOOGLE.replace('share_with = ["dan"]', ""))
    agenda(monkeypatch, diary, event("2026-10-22T12:00:00+01:00", "2026-10-22T13:00:00+01:00"))
    out = output(diary, capsys, "doctor")
    work, personal = json.loads(out)["calendars"]
    assert work == {"source": "outlook", "name": "work", "ok": True, "busy_blocks": 1}
    assert not personal["ok"] and "attache skill diary login personal" in personal["fix"]
    for secret in (*SECRETS, NOTE):
        assert secret not in out, secret
    configure(home, 'days = ["monday"]\n' + CONFIG)
    assert run(diary, capsys, "doctor")["error"].startswith("diary.toml: days must be")


@pytest.mark.parametrize(
    "setting",
    [
        'share_with = "dan"',  # "da" in "dan" would let someone else in
        'hours = "17:30-09:00"',
        "horizon_days = 60",  # the mail skill's agenda reads at most 31 days
        "minutes = 5",
        'gap_minutes = "15"',
        '[[prefer]]\nwhen = "tuesday"',
        '[[avoid]]\nwhen = "mon"\nhours = "9-5"',
        '[[calendar]]\nsource = "icloud"\nname = "x"',
    ],
)
def test_settings_are_checked(diary: ModuleType, home: Path, setting: str) -> None:
    configure(home, setting + "\n" + CONFIG if setting[0] != "[" else CONFIG + setting)
    with pytest.raises(ValueError, match=r"^diary\.toml: "):
        diary.load(home)


def test_a_diary_without_calendars_is_refused(diary: ModuleType, home: Path) -> None:
    configure(home, 'share_with = ["dan"]\n')
    with pytest.raises(ValueError, match="every time would look free"):
        diary.load(home)
