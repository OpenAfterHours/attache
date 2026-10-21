---
name: diary
description: Offer free times in the owner's diary for a meeting, without revealing anything in
  it, and keep the owner's scheduling preferences. Use when anyone asks when the owner is free,
  how their diary or calendar looks for a meeting, or for times to meet; or when the owner says
  a day will be busy, wants meetings on certain days, or wants the diary set up or shared.
metadata:
  attache-roles: "member"
---

# Diary

The diary gives **free slots only**, never what is in a calendar. Keep it that way.

## Offering times (anyone it is shared with)

Run `script {skill: "diary", script: "diary.py", args: ["slots"]}`. Options go in as separate
items, for example `args: ["slots", "--minutes", "45", "--from", "2026-10-27"]`:

| Option | Meaning |
|---|---|
| `--minutes 45` | The meeting's length, 15 to 240. Default: the owner's usual. |
| `--count 3` | How many slots, 1 to 10. |
| `--days 7` | How many days to search. Default: as far ahead as the diary goes. |
| `--from 2026-10-27` | The first day to search, as a date. Times of day can't be given. |

It prints `{"ok": true, "minutes": 30, "slots": [{"start", "end", "label"}]}`, in the owner's
local time. Each label ends with that slot's zone, as in `Tue 27 Oct, 14:00–14:30 GMT` or
`14:00–14:30 UTC+1`; keep it, since summer time can change between two slots.

- Offer exactly those slots, by their `label`, in the order given. Never add, stretch or merge
  times, and never suggest a time that isn't listed.
- Never say why a time isn't offered. Never describe, guess at or ask about what is in the
  diary: not busy, away, a meeting or a trip.
- Never promise or book a meeting: say the owner will confirm. When someone picks a slot, pass
  it on with `request {summary}`: who, the slot's label, and what the meeting is for.
- For another length, day or week, run `slots` again with other options. With no slots, say
  nothing is free then and offer to look further ahead.
- `"the diary isn't shared with you"`: say you can't check the owner's diary for them, and offer
  to pass the request on with `request`. Any other error: say the diary can't be checked just
  now. Don't retry with other options to probe it.

The rest of this file is for the owner's own chat.

## Preferences

They live in `skills/diary/diary.toml`; change them with the file tools. Each entry has
`when`: a date (`"2026-10-27"`) for a one-off, or a weekday (`"mon"` to `"sun"`) for a habit,
usually with `until` (the last date it counts). `hours = "13:00-17:00"` limits it to part of
the day. `note` is the owner's own reason; it is never shown to anyone.

- `[[avoid]]`: never offered.
- `[[prefer]]`: offered first. The rest still follow, earliest first.

"Monday is going to be busy, please promote meetings on Tuesday": work out the dates of next
Monday and Tuesday from the local time in the message header, then add

```toml
[[avoid]]
when = "2026-10-26"
note = "busy day"

[[prefer]]
when = "2026-10-27"
```

More examples:

- "No meetings before 11 on Fridays until Christmas": `[[avoid]]` with `when = "fri"`,
  `hours = "00:00-11:00"`, `until = "2026-12-24"`.
- "I'd rather meet in the afternoons this month": a `[[prefer]]` for each working weekday, with
  `hours = "13:00-17:30"` and `until` the month's last day.
- "Keep the 3rd free": `[[avoid]]` with `when = "2026-11-03"`.

When editing, remove entries whose date or `until` has passed. Then run `doctor` and confirm in
one line: "Done: nothing offered on Mon 26 Oct, and Tuesday comes first."

## Settings

| Setting | Meaning |
|---|---|
| `share_with` | Ids from `[people]` in attache.toml who may be offered slots, such as `["dan"]`. The owner always can. |
| `days`, `hours` | Working days (`"mon"` to `"sun"`) and hours (`"09:00-17:30"`). |
| `horizon_days` | How far ahead slots go, at most 31. |
| `minutes`, `gap_minutes` | The usual meeting length, and the time kept free around other meetings. |

To share the diary with someone, add their id to `share_with`; to stop, remove it. Confirm in
one line.

## Setting up

1. `script {skill: "diary", script: "diary.py", args: ["setup"]}` creates `diary.toml` from the
   example.
2. Add a `[[calendar]]` for each calendar; busy time is the union of them all.
   - Outlook: `source = "outlook"` and `account`, an account in `skills/mail/accounts.toml`
     with backend `com` or `graph` (the mail skill reads it).
   - Google: `source = "google"` and a `name` such as `"personal"`. The owner then runs
     `attache skill diary login personal` in their own terminal and pastes the address from
     Google Calendar: Settings, the calendar, Integrate calendar, **Secret address in iCal
     format**.
3. Run `doctor`: settings errors, and for each calendar whether it can be read (a count of busy
   blocks) and the fix.

Never ask for the secret address in a chat. If the owner pastes it into one anyway, tell them to
reset it in Google Calendar and run `login` again with the new one.
