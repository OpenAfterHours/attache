---
name: artifact-kit
description: Build artifacts - interactive HTML pages over live data, such as boards,
  checklists, trackers and dashboards - with the shared design tokens and templates. Read it
  before every call to the `artifact` tool, or when asked for a page, board or anything
  interactive.
---

# Artifact kit

An artifact is one HTML file that Attaché serves at `/a/<name>`. Before your scripts run it
injects `window.attache`, a bridge to the document store with live updates. The page runs
sandboxed and can reach only the collections it was published with.

## Rules

1. **Self-contained.** One file: inline the whole of `tokens.css` (in this folder) in a
   `<style>` at the top, then your own CSS and JS. No external scripts, styles, fonts or
   images unless the owner asks for one.
2. **Declare collections.** Put `<meta name="attache-collections" content="todos,_tasks">` in
   `<head>` and pass the same list as `collections` to the `artifact` tool.
3. **Data through `window.attache` only.** Read data at load and on change; never paste data
   that will change into the HTML.
4. **`textContent` for data.** Docs hold text from chats, emails and web pages. Build elements
   and set `textContent`; never put data into `innerHTML` or attributes like `href` unchecked.
5. **Both themes.** Use only the `var(--...)` color tokens; never hard-code a color.
6. **Phone width.** It must work at 360px wide: keep the viewport meta, wrap rows, no fixed
   widths.
7. **No secrets.** Never put tokens, passwords, keys or private notes in a page.
8. **Start from a template** (below) instead of a blank page.

The sandbox also means: no `localStorage` or cookies, no `alert`/`confirm`/`prompt`, and
links should use `target="_blank"`.

## The bridge

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
- `put` with `null` creates a doc and resolves to it, `id` included.
- Collections starting with `_` are read-only. `_tasks` lists pending reminders:
  `{id, text, due (epoch seconds), cron, kind, chat_key}`.
- After a change, re-render from `attache.db.list`; `attache.on` tells you when.
- A page cannot message you or the chat. For actions such as sending email, the person asks
  in the chat.

## Pages made in a group chat

They see only docs shared with that chat. They can add docs (shared with it automatically)
and edit shared ones, but cannot delete.

## Templates

- `templates/board.html`: cards in columns by one field, with add and move. Set `HEADING`,
  `COLLECTION`, `COLUMN`, `COLUMNS`, `TITLE` and `HIDDEN` at the top of its script, the
  `<title>`, and the `<meta name="attache-collections">` tag.
- `templates/home.html`: the Home page (`/a/home`): today's to-dos by status, an add form and
  upcoming reminders. When you change Home, publish it as `home` with
  `collections: ["todos", "_tasks"]`.

Read the template with `read_file` and adapt a copy; do not edit the templates themselves
unless the owner asks for a new look.

## Publishing

```json
{"name": "trip-checklist", "html": "<!doctype html>...", "collections": ["trip"]}
```

- The `artifact` tool returns `{"url": ...}`. Send that link in your reply.
- Publishing the same name again replaces the page; open copies reload in the web app.
- Names become lowercase-and-hyphens slugs; choose short, clear ones.

## Before you publish

- The meta tag and `collections` match, and the page uses no other collection.
- An empty state says how to add the first item ("Nothing yet. Add one above, or ask me.").
- Errors appear in the page, not only in the console.
- All data goes in with `textContent`; colors are tokens; it reads well at 360px.

## Changing the look

Every page shares `tokens.css`, so they look like one product. When the owner asks for a
different look, edit `tokens.css` and the templates here, then republish the pages they use.
