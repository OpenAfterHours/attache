---
icon: lucide/calendar-clock
---

# Diary

The `diary` skill answers "how is Phil's diary looking for a meeting?" with free times and
nothing else. It reads your Outlook or Google calendars, or both, takes out the busy time,
follows your preferences, and offers a few slots. The titles, attendees, places and
descriptions of your meetings never reach the assistant, so it can't repeat them to anyone,
however it is asked.

It is a member skill: people you share it with can ask in their own chats, and by email
through the `email` channel. They get slots; only you can change how it works.

## What is and isn't revealed

| Offered | Never shown |
|---|---|
| Free slots: start, end and a label such as `Tue 27 Oct, 14:00–14:30 GMT` | Titles, attendees, organisers, places and descriptions |
| Your time zone, in each label | Why a time isn't free, or which calendar it is in |
| The meeting length asked for | The notes on your preferences |
| | The secret address of a Google calendar |

How it holds:

- **At the source.** The code that reads a calendar keeps each event's start and end and
  drops everything else right where it is parsed. Nothing else is passed on, so no question,
  however clever, can get it out.
- **Free time counts as free.** Outlook items shown as free or "working elsewhere", and Google
  events marked free or cancelled, don't block anything. Tentative and out-of-office items
  do, and so does an item whose status isn't known.
- **Errors say nothing.** Someone you share with sees "the diary can't be read just now", never
  what went wrong. You see the details, in your own chat and in the terminal.
- **Notes stay in the file.** The `note` on a preference is for you; it is never printed.
- **The secret address stays in the keychain.** No command prints it, and its errors never
  include it.

What someone can still work out: free slots are what is left after the busy time. A person who
runs `slots` many times, with different `--from`, `--minutes` and `--count`, can map your free
and busy time fairly closely, though never what a meeting is or who it is with. Preferred days
come first, so the order hints at your preferences too. `share_with` is the control: share the
diary only with people you would tell "Tuesday morning is no good" anyway.

## Setting up

Run these in a terminal, not in a chat. The first run downloads the skill's libraries.

1. Create the settings file:

    ```
    attache skill diary setup
    ```

    It copies `diary.example.toml` to `~/.attache/skills/diary/diary.toml` and prints the
    path.

2. Add your calendars, below. Busy time is the union of every `[[calendar]]`; delete the
   example ones you don't use. A diary with no calendar is refused, since every time would
   look free.

3. Check it:

    ```
    attache skill diary doctor
    ```

    Each calendar says `ok` with a count of its busy blocks (never what they are), or what is
    wrong and how to fix it.

### Outlook

The diary reads Outlook through the [mail skill](mail.md), so set up that account first, with
backend `com` (Outlook classic on this PC) or `graph`. Then name it:

```toml
[[calendar]]
source = "outlook"
account = "outlook"   # the [section] of skills/mail/accounts.toml
```

The diary runs the mail skill's `agenda` for that account and keeps only start and end times.
IMAP accounts (Gmail, Titan) have no calendar here; for a Google calendar, use its secret
address, below.

The mail skill must be up to date: the diary relies on its `agenda` saying whether each item
shows as free or busy. An older copy of the skill doesn't, so every item counts as busy,
including the ones you marked free. See
[Updating the mail skill](mail.md#updating-the-mail-skill).

### Google

Google Calendar gives every calendar a private address for reading it in iCal format. The
diary reads that address over HTTPS, with recurring events expanded.

1. Open [Google Calendar](https://calendar.google.com/) on the web. Under **Settings** (the
   gear), **Settings for my calendars**, choose the calendar.
2. Under **Integrate calendar**, copy the **Secret address in iCal format**. It ends in
   `basic.ics`.
3. Add the calendar, with a name of your choice:

    ```toml
    [[calendar]]
    source = "google"
    name = "personal"
    ```

4. Store the address:

    ```
    attache skill diary login personal
    ```

    Paste it when asked; it isn't shown. The diary checks that it is an `https://` address
    and that it reads as a calendar, then keeps it in your system keychain (service
    `attache-diary`, user `personal`). It is never written to a file.

Anyone with the secret address can read your whole calendar, titles and all. Never paste it
into a chat. If it has been seen, use **Reset** next to it in Google Calendar, then run `login`
again with the new one. A calendar that answers `HTTP 404` has usually been reset this way.

## Settings

All in `diary.toml`:

| Setting | Default | Meaning |
|---|---|---|
| `share_with` | `[]` | Who may be offered slots: ids from `[people]` in `attache.toml`. You always can. |
| `days` | `["mon", "tue", "wed", "thu", "fri"]` | Working days. |
| `hours` | `"09:00-17:30"` | Working hours. |
| `horizon_days` | `14` | How far ahead slots go, at most 31. |
| `minutes` | `30` | The meeting length when none is asked for. |
| `gap_minutes` | `15` | Time kept free before and after other meetings. |

How slots are chosen:

- Only on working days and inside working hours, starting on a half-hour grid from the
  opening time.
- Never overlapping busy time, widened by `gap_minutes`, or an `[[avoid]]`.
- Never sooner than an hour from now.
- Slots inside a `[[prefer]]` come first, then the rest; each group earliest first.
- Spread out: at most two a day, at least two hours apart. Three are offered unless more are
  asked for (up to ten).

Times are in this computer's time zone, and stay right across the change to and from summer
time. Each label ends with its own slot's zone: its short name, such as `BST` or `CET`, or,
where the system's name is long (as on Windows), its offset, such as `UTC+1`. An all-day event
that isn't marked free blocks the whole day.

## Preferences

Tell the assistant in your own chat, or edit `diary.toml` yourself. Each preference has a
`when`: a date for a one-off, or a weekday (`"mon"` to `"sun"`) for a habit, usually with
`until`, the last date it counts. `hours` limits it to part of the day. `note` is your own
reminder of why, and is never shown to anyone.

- `[[avoid]]`: never offered.
- `[[prefer]]`: offered first. Other times still follow, so people get a choice.

"Monday is going to be busy, please promote meetings on Tuesday", said on Wednesday 21 October:

```toml
[[avoid]]
when = "2026-10-26"
note = "finishing the board report"

[[prefer]]
when = "2026-10-27"
```

Monday is then never offered, and Tuesday's free times come before the rest.

More:

```toml
# "No meetings before 11 on Fridays until Christmas"
[[avoid]]
when = "fri"
hours = "00:00-11:00"
until = "2026-12-24"

# "Afternoons suit me best this month": one entry per working day
[[prefer]]
when = "mon"
hours = "13:00-17:30"
until = "2026-11-30"

# "Keep the 3rd free"
[[avoid]]
when = "2026-11-03"
note = "dentist, then the school play"
```

Entries whose date or `until` has passed are ignored. When the assistant edits preferences it
removes them, runs `doctor`, and confirms in one line.

## Sharing

Add people by their id from `[people]` in `attache.toml`:

```toml
share_with = ["dan", "priya"]
```

- Dan asks in his own chat, or emails "@attache is Phil free for 45 minutes next week?". He
  gets slots, and only slots.
- Anyone else is told the diary isn't shared with them, and the assistant offers to pass the
  request on to you.
- Nobody can book a time through the diary, not even the assistant. When someone picks a slot,
  the assistant passes it on to you with `request`; you send the invitation.
- In a group chat, everyone in the group sees the slots offered to the person who asked.
  That includes you: you get slots in a group chat too, though you act there with a member's
  rights. The jobs skill is deliberately stricter there.
- Members can't run `setup`, `login` or `doctor`, or change the settings.

Remove someone from `share_with` to stop. Nothing else needs undoing.

## Commands

Each prints one JSON object, `{"ok": true, ...}` or `{"ok": false, "error": "..."}`.

| Command | Who | Does |
|---|---|---|
| `slots [--minutes 30] [--count 3] [--days 14] [--from 2026-10-27]` | You, and `share_with` | Free slots. Minutes 15 to 240, count 1 to 10, days 1 to 31 and never past `horizon_days`. `--from` is a date; times of day can't be given. |
| `setup` | You | Copies the example settings, if there are none yet. |
| `login NAME` | You, in a terminal | Stores a Google calendar's secret address in the keychain, after checking it. |
| `doctor` | You | Settings errors, and each calendar's state with a count of busy blocks and the fix. |

Run them as `attache skill diary slots --from 2026-10-27`; the assistant uses the `script`
tool.

## Troubleshooting

| You see | Do |
|---|---|
| `diary.toml: ...` | Fix the setting it names, then run `doctor`. |
| `mail account outlook: ...` | Run `attache skill mail doctor --account outlook`. |
| `the mail skill's agenda failed (exit N)` | The mail skill isn't installed or didn't start: run `attache skill mail doctor`. |
| `no iCal address for personal` | Run `attache skill diary login personal`. |
| `the calendar's address answered HTTP 404` | The secret address was reset: copy the new one and run `login` again. |
| `the calendar didn't read as iCal` | Copy the **Secret address in iCal format**, not the embed code or a page's address. |
| No slots at all | Check `days`, `hours`, `horizon_days` and your `[[avoid]]` entries. |
