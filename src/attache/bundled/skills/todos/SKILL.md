---
name: todos
description: Keep to-do lists in the `todos` collection - add, find, tick off, move and share
  items, and show lists as a board. Use when someone mentions a task, a to-do, a list,
  something they must remember to do, or asks what is on their plate.
---

# To-dos

To-dos live in the `todos` collection. Use the `db` tool for every change; never keep lists in
memory files or in your reply alone.

## The doc

```json
{"title": "Book the dentist", "list": "Inbox", "status": "todo",
 "due": "2026-10-23", "notes": "ask for a Friday slot", "_shared": ["slack:C0123"]}
```

| Field | Meaning |
|---|---|
| `title` | Required. Short and actionable, starting with a verb: "Call Sam about the boiler". |
| `list` | `Inbox` unless a list is named or obvious (`Shopping`, `Work`, `Trip`). Title Case. |
| `status` | `todo`, `waiting` (blocked on someone else) or `done`. |
| `due` | Optional local date `YYYY-MM-DD`, or `YYYY-MM-DDTHH:MM` when the time matters. |
| `notes` | Optional details: links, quantities, who you are waiting for. |
| `_shared` | Optional chat keys of group chats that may see and tick the item. |

The store adds `id`, `_updated` and `_by`. A put **replaces** the whole doc: get or list it
first, then send every field back with your change.

## Adding

1. `db {op: "list", collection: "todos"}` first. If an open item already means the same thing
   (same list, similar title), update that one instead of adding a duplicate, and say so.
2. One doc per item: "milk, eggs and bread" is three items on `Shopping`.
3. Turn relative dates ("Friday", "tomorrow", "end of the month") into dates using the local
   time in the message header. Leave `due` out when none was given.
4. `db {op: "put", collection: "todos", data: {...}}` with no `id` creates the item.
5. If they also want a nudge at a time, schedule a reminder too (see the reminders skill).
   A due date on its own reminds nobody.

## Changing

- Tick off: put the doc back with `status: "done"`. Keep done items; Home hides them after
  the day they were done.
- "Waiting on Dan for the quote": `status: "waiting"`, `notes: "Dan: quote"`.
- Move with `list`, rename with `title`, reopen with `status: "todo"`.
- Match loosely ("the dentist one") and ask only when two items fit equally well.
- Delete only when asked to remove an item, never to tick it off. Only the owner can delete.

## Showing lists

- Up to about seven items: answer in chat, one line each, open items first, with the due date.
- More, or "show me my lists": point to **Home** (the `home` page in the web app shows today's
  to-dos and upcoming reminders, live).
- For a dedicated board (one list, a project, a shared family list), read the artifact-kit
  skill, copy `skills/artifact-kit/templates/board.html`, keep `COLLECTION = "todos"` and
  `COLUMN = "status"`, and publish it with the `artifact` tool and `collections: ["todos"]`.
  To show one list only, filter in the page: `attache.db.list("todos", {list: "Shopping"})`.

## Sharing a list with a group

Members of a group chat see only docs whose `_shared` holds that chat's key. Chat keys are the
`[chats."<key>"]` names in `attache.toml`, such as `slack:C0123`.

- When the owner asks to share a list, add the chat key to `_shared` on every item of that
  list, keeping any keys already there. New items for that list need the key too.
- In a group chat the `db` tool is already scoped to that chat: what you add there is shared
  with it automatically, and you cannot see or change anything else. A board made in a group
  chat is scoped the same way.
- Unsharing (removing the key) and deleting are for the owner only. Members can add, tick and
  edit shared items.
- Credit is automatic: the store records who made each change.

## Confirming

Confirm every change in one line, and nothing more unless asked:

- "Added *Book the dentist* to Inbox, due Friday."
- "Ticked off 3 items on Shopping."
- "Moved *Passports* to Trip and shared the Trip list with the Family chat."
