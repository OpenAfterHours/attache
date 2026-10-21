---
name: tui
description: The full-screen terminal app for the owner's own chat, like Claude Code - a
  scrolling transcript, an input box pinned at the bottom, a live status line, and each turn's
  steps folded under it. Use when the owner asks for a full-screen terminal, a nicer terminal
  chat, or how to see what Attaché is doing while it works. Owner only.
---

# The full-screen app

The owner runs it in their own terminal:

```
attache skill tui
```

Never run it with the `script` tool: it needs a terminal to draw in, and it starts its own
Attaché, so from inside a turn it would only find the port taken.

## What to tell the owner

- **It starts Attaché itself**, as `attache run`, and stops it on quitting. So stop any
  `attache run`, `attache chat` or service first: if one is running, the app says "Attaché
  may already be running; stop it or use its web page." and exits.
- The first run downloads Textual and httpx (a few MB) through uv.
- An older home gets this skill with `attache init`, which adds new bundled skills and never
  overwrites.

## Keys

| Key | Does |
|---|---|
| Enter | send the message |
| Esc | stop the turn that is running, and the messages waiting behind it |
| ctrl+o | show every turn's thinking in full, or fold it again |
| 1 to 9 | answer the open question, while the input box is empty |
| ctrl+l | show or hide Attaché's own log |
| ctrl+d, ctrl+q | quit, which stops Attaché too |

A question shows its options as numbered lines; pressing the number answers it.

## How it connects

It reads the link `attache run` prints (`Open http://127.0.0.1:<port>/#t=<token>`) and talks
to that Attaché the way the web page does. The token stays in the app's memory, never in a
file, and changes every run. It shows the owner's own chat (`local:me`) only, the same chat
as the terminal and the web page.

More: https://openafterhours.github.io/attache/terminal/
