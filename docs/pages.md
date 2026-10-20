---
icon: lucide/layout-dashboard
---

# Pages

A page (Attaché calls it an *artifact*) is an HTML file that the assistant writes and the web
page shows. A to-do board, a trip checklist, a reading tracker: anything interactive. Pages
read and write your real data through a small bridge, so a tick on a board changes the same
to-do the assistant sees, and every open copy updates at once.

## Getting a page

Ask for one in the chat:

- `make me a board for my Trip list`
- `I want a page to track the books I'm reading, with a rating for each`
- `change Home so waiting items come first`

The assistant reads the `artifact-kit` skill, starts from one of its templates, and publishes
the page with its `artifact` tool. It replies with a link like
`http://127.0.0.1:8765/#page=trip-board`, which opens the page in the Pages panel of the
Attaché window. Every page is listed there, by name.

**Home** is a page too. `attache init` writes it to `artifacts/home.html`: today's to-dos by
status, a form to add one, and the upcoming reminders. Ask for a different layout and the
assistant edits it like any other page.

Pages live in `~/.attache/artifacts/`, one file each, and are committed to git with the rest
of your home. Publishing a page under a name that exists replaces it. Names are lowercase
letters, digits and hyphens (the assistant turns "Trip board" into `trip-board`), and the
Pages panel shows them as words.

## Writing a page by hand

Any `<name>.html` file you save in `~/.attache/artifacts/` is a page, as long as the name is
lowercase letters, digits and hyphens. It shows up in the Pages panel the next time the list
refreshes: after the next reply, or when you reload the web page.

Here is a complete page that keeps a list of notes:

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="attache-collections" content="notes">
  <title>Notes</title>
  <style>
    :root { color-scheme: light dark; font-family: system-ui, sans-serif; }
    body { margin: 16px; }
    form { display: flex; gap: 8px; }
    input { flex: 1; min-width: 0; }
  </style>
</head>
<body>
  <form id="add">
    <input id="text" placeholder="A note" required>
    <button>Add</button>
  </form>
  <ul id="list"></ul>
  <p id="error" role="alert"></p>
  <script>
    const list = document.getElementById("list");
    const error = document.getElementById("error");

    async function render() {
      try {
        const notes = await attache.db.list("notes");
        list.replaceChildren(...notes.map((note) => {
          const item = document.createElement("li");
          item.textContent = note.text;  // never innerHTML: data is untrusted text
          return item;
        }));
        if (!notes.length) list.textContent = "Nothing yet. Add one above, or ask me.";
      } catch (err) {
        error.textContent = err.message;
      }
    }

    document.getElementById("add").addEventListener("submit", async (event) => {
      event.preventDefault();  // pages can't submit forms; handle them in script
      const input = document.getElementById("text");
      try {
        await attache.db.put("notes", null, {text: input.value});
        input.value = "";
      } catch (err) {
        error.textContent = err.message;
      }
    });

    attache.on("notes", render);  // re-render on every change, from anywhere
    render();
  </script>
</body>
</html>
```

Save it as `~/.attache/artifacts/notes.html`, and the assistant can work with the same notes:
"add a note: call the plumber" appears on the open page straight away.

## The meta tag

A page names the collections it uses in its `<head>`:

```html
<meta name="attache-collections" content="todos,_tasks">
```

This is the only place a page gets data access. A page can reach the collections listed
there and no others, and a page without the tag can reach none. Calls to anything else fail
with an error such as `this page may not use collection 'notes'`.

## The bridge: `attache.db` and `attache.on`

When Attaché serves a page, it adds a script just before `</head>` that gives the page a
global `attache` object, so it is ready for any script in the page's `<body>`.

```js
const todos = await attache.db.list("todos");                     // every doc, oldest first
const shop = await attache.db.list("todos", {list: "Shopping"});  // only docs whose fields match
const one = await attache.db.get("todos", id);
const added = await attache.db.put("todos", null, {title: "Milk", list: "Shopping", status: "todo"});
await attache.db.put("todos", one.id, {...one, status: "done"});  // put replaces the whole doc
await attache.db.delete("todos", id);
const stop = attache.on("todos", ({collection, id, doc}) => render());  // doc is null on delete
stop();                                                            // stop listening
```

| Call | What it does |
|---|---|
| `attache.db.list(collection, where?)` | Every doc in the collection, oldest first. With `where`, only the docs whose fields equal those values. |
| `attache.db.get(collection, id)` | One doc. Rejects if there is none. |
| `attache.db.put(collection, id, data)` | Replaces the doc with `data` and resolves to the stored doc. With `id` set to `null`, creates a doc with a new id. |
| `attache.db.delete(collection, id)` | Deletes a doc. |
| `attache.on(collection, callback)` | Calls `callback({collection, id, doc})` on every change to the collection, whoever made it. `doc` is `null` for a deletion. Returns a function that stops listening. |

Every `attache.db` call returns a promise, which rejects with an `Error` carrying the
server's message. Show errors in the page, not only in the console.

### Documents

Documents are JSON objects in named collections. The store adds three fields to every doc:

| Field | Meaning |
|---|---|
| `id` | The doc's id. A short random one when you create a doc with `null`. |
| `_updated` | When it last changed, in epoch seconds. |
| `_by` | Who wrote it: a person id such as `owner`, or `web:<page>` for a change made on a page. |

`put` replaces the whole document, so read it first and send every field back with your
change, as in `{...one, status: "done"}`.

The `todos` collection is the one the assistant and Home use; the `todos` skill describes its
fields (`title`, `list`, `status`, `due`, `notes`). Any other name makes a new collection.

Collections whose names start with `_` are read-only. `_tasks` lists the pending reminders
and routines, earliest first, as
`{id, chat_key, text, kind, due, cron, status, by, created}`; `due` is in epoch seconds, so
`new Date(task.due * 1000)` gives the time. `_tasks` sends no live updates, so refresh it on a
timer: Home does every minute.

## The sandbox

Pages are written by the assistant, and may quote emails, web pages and other people's
messages, so Attaché treats every page as untrusted HTML. It serves each one with a strict
Content Security Policy:

```text
sandbox allow-scripts allow-forms; default-src 'self' 'unsafe-inline' data:;
img-src 'self' data:; font-src 'self' data:; connect-src 'self'; form-action 'none';
base-uri 'none'; frame-ancestors 'self'
```

What that means for a page:

- **No network.** It can't load scripts, styles, fonts or images from the internet, or send
  anything there. Put everything inline in the one file; images must be inline `data:` URLs
  or SVG. The only files a page loads are Attaché's own: the bridge, and the fonts that the
  kit's `tokens.css` declares, which Attaché serves at `/fonts/`.
- **Its own origin.** The sandbox gives the page an opaque origin, so it can't read the
  Attaché window, other pages, or their storage. There are no cookies and no `localStorage`.
- **Only declared data.** It reaches Attaché only through the bridge, with a token for that
  page alone, and only the collections in its meta tag.
- **No pop-ups or dialogs.** No new windows, no `alert`, `confirm` or `prompt`, and no form
  submissions (handle `submit` in script, as above). Links can't take the window elsewhere.
- **Only inside Attaché.** A page opens only in the Pages panel. Opened in a tab of its own,
  or framed by another site, it is refused.

Each page's token is derived from the token in the link Attaché printed, so it changes every
time Attaché starts.

!!! tip "Text, never HTML"

    Documents hold text from chats, emails and web pages. Put data into the page with
    `textContent`, never `innerHTML`, and never into attributes such as `href`.

## The look

Every page the assistant makes inlines `tokens.css` from the `artifact-kit` skill and uses only
its `var(--...)` tokens, so pages look like one product and match the Attaché window: the
OpenAfterHours night-sky look, dark by default and light when your system prefers it. A page
you write by hand can do the same: paste the kit's `tokens.css` into a `<style>` at the top.

To change the look of every page at once, ask for it: the assistant edits the kit tokens in
`tokens.css` and the templates, then republishes the pages that use them. The `--oah-*`
brand values in that file come from the OpenAfterHours design system and stay as they are.

## Pages from group chats

!!! info "Phase 2"

    Group chats arrive with Slack and Signal. In phase 1 every page is yours.

A page published by a member, or by anyone in a group chat, is tied to that chat. It sees only
the documents shared with that chat (and that chat's `_tasks`). It can add documents, which
are shared with the chat automatically, and edit shared ones, but it can't delete. A member
can't replace one of your pages, or a page that belongs to another chat.
