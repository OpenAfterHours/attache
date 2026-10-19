---
name: reminders
description: Set, list and cancel reminders and recurring routines with the `schedule` tool.
  Use when someone says remind me, later, tomorrow, at a time, every morning or every week, or
  asks which reminders are set.
---

# Reminders

Everything that should happen later goes through the `schedule` tool. Never promise to
remember something without scheduling it.

## Adding

`schedule {op: "add", text: "...", ...}` plus exactly one of:

- `at`: an ISO **local** date-time without an offset, such as `"2026-10-20T09:00"`. Work it
  out from the local time in the message header. A bare time that has already passed today
  means tomorrow.
- `in_minutes`: for "in 20 minutes" or "in an hour" (60).
- `cron`: five fields in local time, `minute hour day-of-month month day-of-week`.

`kind` decides what happens when it fires:

- `"message"` (the default): Attaché posts `Reminder: <text>` in this chat. Write the text for
  the person reading it later: "Call Sam about the boiler", not "remind me to call Sam".
- `"turn"`: the text runs as a request from the owner in this chat, with tools. Use it for
  routines such as a morning summary or a weekly review. Owner only.

Members of a group chat can add `kind: "message"` reminders in that chat, and cancel only their
own.

## Cron examples

| They say | `cron` |
|---|---|
| every day at 7:30 | `30 7 * * *` |
| weekdays at 9 | `0 9 * * 1-5` |
| every Monday at 8:15 | `15 8 * * 1` |
| Sunday evening at 6 | `0 18 * * 0` |
| on the 1st of each month at 10 | `0 10 1 * *` |
| every two hours from 8 to 6 | `0 8-18/2 * * *` |
| 25 December at 9 | `0 9 25 12 *` |

Day of week: 0 or 7 is Sunday, 1 is Monday.

## Routines

A morning summary on weekdays:

```json
{"op": "add", "kind": "turn", "cron": "30 7 * * 1-5",
 "text": "Morning summary: today's reminders, open to-dos due today or overdue, and anything waiting on others. Five lines at most."}
```

Write routine text as a complete instruction to yourself: it runs later without this
conversation's context.

## Listing and cancelling

- `schedule {op: "list"}` shows this chat's pending tasks, earliest first. The owner can add
  `all: true` to see every chat.
- `schedule {op: "cancel", id: 12}` with an id from the list. Match "cancel the dentist one"
  against the texts; ask if two fit.
- To change a reminder, cancel it and add the new one.

## Reading it back

Confirm in plain words with the weekday and time, never cron syntax or ISO strings:

- "I'll remind you tomorrow (Tuesday) at 9:00 to call Sam."
- "Every weekday at 7:30 I'll send you a morning summary."
- "Reminder set for 20 minutes from now, at 14:35."

If the time is vague ("tonight", "later", "next week"), pick a sensible time (tonight is 19:00,
later is in 2 hours, next week is Monday at 9:00) and say which you picked, so they can change
it. Ask first only when the time really matters, such as for a flight.

To-dos with a due date do not remind anyone. When someone wants both, add the to-do and
schedule the reminder.
