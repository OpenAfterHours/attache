---
icon: lucide/square-terminal
---

# Terminal

There are two ways to chat with Attaché in a terminal:

- `attache chat` (or plain `attache`) is a line-by-line chat that works anywhere, even in a
  pipe or over SSH.
- `attache skill tui` is a full-screen app, like Claude Code.

Both show what Attaché is doing while it works on a reply, which can take 15 to 30 seconds
when it checks your calendar and mail.

Only your own chats show this. Group chats and other people never see steps or thinking.

## The step trail in `attache chat`

While a turn of yours runs, Attaché prints short, indented lines of plain text under your
message. They are added as the turn goes and never redrawn, so typing while it works is safe.

```
what's on tomorrow, and has Dan replied about the boiler?
  working…
  ∴ Calendar first, then mail from Dan.
  · script mail agenda  1.1s
  · script mail search  0.9s
  · script mail read  0.4s
attaché> Tomorrow you have the dentist at 9. Dan replied on Friday: …
  14.2s · 3.1k tokens · claude
```

| Line | Means |
|---|---|
| `working…` | The turn has started. |
| `· script mail agenda  1.1s` | A step: a tool Attaché used, and how long it took. Labels name the tool and what it worked on (a skill, a file, a collection), never the contents. Claude Code's own tools, such as `Bash ls -la` or `Read src/app.py`, show without a time. |
| `∴ Calendar first, then mail from Dan.` | Thinking, as its first sentence, at most 100 characters. |
| `14.2s · 3.1k tokens · claude` | After the reply: how long the turn took, the tokens it used, and the route it went to. Without a token count it is `14.2s · claude`. |
| `stopped · 3.2s` | The turn was stopped, and sent no reply. |

**Ctrl+C** while a turn runs stops that turn. With nothing running, Ctrl+C quits, as before.

The web page shows the same steps while a turn runs, with a **Stop** button (or Esc).

Stop and Ctrl+C stop what you asked for:
- the turn that is running, a scheduled one in your chat included (a morning triage, say);
- the messages you sent while it ran, which were waiting behind it.

The full thinking text is in the full-screen app (ctrl+o), and on the web page when you hover
over the thinking summary.

Type `/new` to [start a new chat](quickstart.md#start-a-new-chat). A turn that is running
stops first, and the web page clears too. In the full-screen app, `/new` works the same, but
the earlier messages stay on its screen until it restarts.

## The full-screen app

```bash
attache skill tui
```

The conversation scrolls, the input box stays at the bottom, and a live status line sits
above it:

```
Attaché · local:me · claude
you      what's on tomorrow, and has Dan replied about the boiler?
▸ Thinking · Calendar first, then mail from Dan.   ctrl+o
● script mail agenda  ✓ 1.1s
● script mail search  ✓ 0.9s
⠹ script mail search · 4s · esc to stop
Message Attaché
ctrl+o steps · esc stop · ctrl+l log · ctrl+d quit
```

After the turn, the reply appears under its steps, and the status line shows
`✓ done · 14.2s · 3.1k tokens · claude`.

### Installing

It is the `tui` skill, built on [Textual](https://textual.textualize.io/). Its first run
downloads Textual and httpx (a few MB) through uv, outside Attaché, so Attaché itself keeps
no extra dependency. A home made before the skill existed gets it with `attache init`, which
adds new bundled skills and never overwrites your files.

### How it starts Attaché

The app starts Attaché itself, as `attache run`, and stops it when you quit. So stop any other
`attache run` or `attache chat` first, and the login service if you installed one
(`attache skill service uninstall`).
If one is already running, the app shows its message and exits: "Attaché may already be
running; stop it or use its web page."

It reads the link Attaché prints (`Open http://127.0.0.1:8765/#t=…`) and talks to Attaché the
way the web page does. The link's token stays in the app's memory, never in a file, and
changes every run. Attaché's own output goes to the log pane (ctrl+l).

### Keys

| Key | Does |
|---|---|
| Enter | Send the message. |
| Esc | Stop the turn that is running, and the messages waiting behind it, as Stop does. |
| ctrl+o | Show every turn's thinking in full, or fold it again. |
| 1 to 9 | Answer the open question, while the input box is empty. |
| ctrl+l | Show or hide Attaché's own log. |
| ctrl+d or ctrl+q | Quit. Attaché stops too. |

Questions (Approve or Decline, Send or Not now) show their options as numbered lines. Press
the number to answer.

## Thinking

Thinking shows only on the `claude-cli` route, which is your Claude Code login. It comes from
Claude Code's own event stream as the turn runs, so there is nothing to set up.

The `anthropic`, `openai-chat` and `openai-responses` routes show steps, but no thinking.
