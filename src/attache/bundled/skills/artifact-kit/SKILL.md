---
name: artifact-kit
description: Build artifacts - interactive HTML pages over live data, such as boards,
  checklists, trackers and dashboards - with the shared design tokens and templates. Read it
  before every call to the `artifact` tool, or when asked for a page, board or anything
  interactive.
---

# Artifact kit

An artifact is one HTML file that Attaché shows in the Pages panel of its window. Before your
scripts run it injects `window.attache`, a bridge to the document store with live updates.
The page runs sandboxed and can reach only the collections its meta tag declares.

## Rules

1. **Self-contained.** One file: inline the whole of `tokens.css` (in this folder) in a
   `<style>` at the top, then your own CSS and JS. Pages cannot load anything from the
   network: no external scripts, styles, fonts or images (the server's policy blocks them).
   Images must be inline `data:` URLs or SVG.
2. **Declare collections.** `<meta name="attache-collections" content="todos,_tasks">` in
   `<head>` is the only place a page gets data access. Without it, every call is refused.
3. **Data through `window.attache` only.** Read data at load and on change; never paste data
   that will change into the HTML.
4. **`textContent` for data.** Docs hold text from chats, emails and web pages. Build elements
   and set `textContent`; never put data into `innerHTML` or into attributes such as `href`.
5. **Both themes.** Use only the `var(--...)` color tokens; never hard-code a color.
6. **Phone width.** It must work at 360px wide: keep the viewport meta, wrap rows, no fixed
   widths.
7. **No secrets.** Never put tokens, passwords, keys or private notes in a page.
8. **Start from a template** (below) instead of a blank page.

The sandbox also means: no `localStorage` or cookies, no `alert`/`confirm`/`prompt`, no
pop-ups or new windows, no form submissions (handle `submit` in JS with `preventDefault`), and
no links that leave the page.

## The bridge

`window.attache` is `{db, on}`:

```js
const todos = await attache.db.list("todos");                    // oldest first
const shop = await attache.db.list("todos", {list: "Shopping"}); // keep docs whose fields match
const one = await attache.db.get("todos", id);
const added = await attache.db.put("todos", null, {title: "Milk", list: "Shopping", status: "todo"});
await attache.db.put("todos", one.id, {...one, status: "done"});  // put replaces the whole doc
await attache.db.delete("todos", id);                            // owner pages only
const stop = attache.on("todos", ({id, doc}) => render());       // live; doc is null on delete
```

- Every call returns a promise that rejects with an `Error` carrying the server's message,
  such as `this page may not use collection 'notes'`. Show errors in the page.
- `put` with `null` creates a doc and resolves to it, `id` included. The store adds `id`,
  `_updated` (epoch seconds) and `_by` to every doc.
- After a change, re-render from `attache.db.list`; `attache.on` tells you when.
- A page cannot message anyone or ask you anything. For actions such as sending email, the
  person asks in the chat.

Collections starting with `_` are read-only. `_tasks` lists pending reminders, earliest
first, each `{id, chat_key, text, kind, due, cron, status, by, created}`:

| Field | Meaning |
|---|---|
| `id` | Number; what `schedule {op: "cancel"}` takes. |
| `chat_key` | The chat it fires in, such as `local:me`. |
| `text` | The reminder text, or the request a routine runs. |
| `kind` | `"message"` (sent as text) or `"turn"` (run as a request). |
| `due` | Next time it fires, in epoch seconds: `new Date(due * 1000)`. |
| `cron` | Five-field schedule for repeating tasks, or `null`. |
| `status` | Always `"pending"` here. |
| `by` | Who set it (a person id). |
| `created` | When it was set, in epoch seconds. |

`_tasks` has no live updates: refresh it on a timer (Home does every minute).

## Pages made by members or in a group chat

They see only docs shared with that chat (and that chat's `_tasks`). They can add docs
(shared with it automatically) and edit shared ones, but cannot delete.

## Templates

- `templates/board.html`: cards in columns by one field, with add and move. Set `HEADING`,
  `COLLECTION`, `COLUMN`, `COLUMNS`, `TITLE` and `HIDDEN` at the top of its script, the
  `<title>`, and the `<meta name="attache-collections">` tag.
- `templates/home.html`: the Home page (`home`): today's to-dos by status, an add form and
  upcoming reminders. Keep its `content="todos,_tasks"` when you change it.

Read the template with `read_file` (or Claude Code's Read) and adapt a copy; do not edit the
templates themselves unless the owner asks for a new look.

## Publishing

```json
{"name": "trip-checklist", "html": "<!doctype html>..."}
```

- The `artifact` tool takes only `name` and `html`, and returns `{"url": ...}`, a link such as
  `http://127.0.0.1:8765/#page=trip-checklist`. Give the person that link: it opens the page
  in the Pages panel of the Attaché window, the only place pages run.
- Publishing the same name again replaces the page; the open copy reloads.
- Names become lowercase-and-hyphens slugs, shown under Pages as words ("trip-checklist" is
  "Trip checklist"); choose short, clear ones.

## Before you publish

- The meta tag lists every collection the page uses, and no others.
- An empty state says how to add the first item ("Nothing yet. Add one above, or ask me.").
- Errors appear in the page, not only in the console.
- All data goes in with `textContent`; colors are tokens; it reads well at 360px.

## Changing the look

Every page shares `tokens.css`, so they look like one product. When the owner asks for a
different look, edit `tokens.css` and the templates here, then republish the pages they use.
