# Channels

A channel connects Attaché to a chat app. The terminal and the web page are built in. Every
other channel is a skill that ships a small program, a **sidecar**, which Attaché starts with
`uv run`. A sidecar has its own dependencies (declared in its PEP 723 header), so the core never
installs them, and it can be written in any language as long as it speaks the protocol below.

Phase 2 ships two: **Slack** (`skills/slack`) and **Signal** (`skills/signal`).

## Turning a channel on

Channels are off until you enable them in `attache.toml`:

```toml
[channels.slack]
enabled = true

[channels.signal]
enabled = true
number = "+447700900123"     # every other key reaches the sidecar as ATTACHE_<KEY>
```

`enabled` is `true` or `false`; every other value must be a string, because it reaches the
sidecar as an environment variable. The name `local` is taken by the terminal and the web page.

Secrets never go in `attache.toml`. Each sidecar keeps its own in your operating system's
keychain (Windows Credential Manager, macOS Keychain, Secret Service on Linux), under the service
name `attache-<channel>`. Set them once with the skill's setup command, for example
`uv run ~/.attache/skills/slack/channel.py setup`.

## Who's who

People are matched by their platform id, never their display name:

```toml
[owner]
name = "Alex"
approvals = "slack:D0ALEXDM"   # where approval requests go; default "local:me"

[owner.ids]
slack = "U0ALEX"
signal = "a1b2c3d4-…"          # Signal account id (ACI uuid)

[people.sam]
name = "Sam"
slack = "U0SAM"

[chats."signal:Z3JvdXBpZA=="]
name = "Family"
members = ["sam"]
```

`approvals` is `local:me` or your own direct chat with the bot on an enabled channel. It has to
be yours alone: a turn you approve runs with your rights, and replies there. A chat listed under
`[chats]` is refused, so approval questions never land in a group.

## Limits

Turns from anyone but the owner are rate limited:

```toml
[limits]
per_sender_hour = 20   # turns per person per hour
per_chat_hour = 60     # turns per chat per hour
member_daily = 200     # turns per day across all your members
guest_daily = 50       # turns per day across all guests and pages' attache.ask
```

Each is a whole number of at least 1. The two daily budgets are separate pools: guests in a
public chat (`guests = "public"`) and requests from pages (`attache.ask`) draw on
`guest_daily`, so they can never use up the turns your members have. When a limit is hit,
Attaché says so once and then stays quiet until the window resets.

## The sidecar protocol

One JSON object per line, UTF-8, in both directions; a line may be up to 16 MiB. Lines that are
not JSON, and objects with an unknown `type`, are ignored (and logged).

### Sidecar → Attaché (stdout)

```json
{"type": "ready", "self_id": "U0BOT", "name": "Attaché"}
{"type": "message", "chat_id": "C0123", "message_id": "1729.42", "sender_id": "U0SAM",
 "sender_name": "Sam", "text": "@Attaché what time is the ferry?", "is_group": true,
 "mentioned": true, "files": []}
{"type": "answer", "ask_id": "a19", "sender_id": "U0ALEX", "choice": "Approve"}
{"type": "log", "level": "warning", "text": "reconnecting"}
```

- `message`: `is_group` is false for direct messages. `mentioned` is true for a native mention
  or a reply to the bot. `files` are absolute paths of attachments the sidecar downloaded into
  `ATTACHE_FILES`; a path anywhere else is dropped. When a message gets no turn (its sender
  isn't admitted, a guest Attaché doesn't answer, nothing triggered it, or a rate limit), its
  files are deleted, since nothing will read them. Fetching attachments only after a message
  is admitted is planned (#33); until then a sidecar downloads them first, within its caps.
- `answer`: a button press or reaction answering an `ask`. Attaché accepts it only from the
  person who was asked, and replies with `answered`.

### Attaché → sidecar (stdin)

```json
{"type": "send", "chat_id": "C0123", "text": "09:40 from Portsmouth.", "files": [],
 "reply_to": "1729.42"}
{"type": "ask", "chat_id": "D0ALEXDM", "ask_id": "a19",
 "text": "Sam asked: send the packing list as a checklist from now on?",
 "options": ["Approve", "Decline"]}
{"type": "answered", "ask_id": "a19", "sender_id": "U0ALEX", "accepted": true}
{"type": "stop"}
```

- `send`: `files` are absolute paths on this machine. `reply_to` may be null.
- `ask`: a sidecar must handle it. Show `text` with the options as buttons (Slack) or as a
  prompt answered by reaction, where 👍 means the first option and 👎 the second (Signal). With
  neither, send the text followed by "Reply 1 for Approve, 2 for Decline." See
  [Questions](#questions) for typed replies.
- `answered`: the result of an `answer`, with its `sender_id` (the platform id) echoed back.
  `accepted` is false when the person was not the one asked, the question had closed, or the
  choice was not an option. Slack updates the question's message only when it is true; a
  sidecar may ignore it.
- `stop`: exit within 5 seconds. Attaché closes stdin straight after. Exit too whenever stdin
  closes: that is how a sidecar learns Attaché has gone.

### Questions

When Attaché asks someone a question, it goes to every surface of that chat, and each shows
it its own way through its `ask`: the sidecar's buttons or reactions, the open web page's
buttons, or numbered text in the terminal. Each says whether anyone could see it, and the
question counts as shown once one surface has (a running sidecar, an open page, the
terminal). `ChannelManager.ask` returns as soon as it is shown, with a task that gives the
answer, so the question is stored in the chat's history only then, and a member's `request`
replies "Asked Alex" knowing that is true. If no surface could show it, because the sidecar is
down or no page is open, `ask` returns `None` at once instead of waiting, and the tool's own
reply says Attaché couldn't reach the owner just now.

A member's `request` is kept until the owner answers, so it survives a restart: when Attaché
starts again it asks the owner once more. One that ran out of time meanwhile, or that no
surface can show right then, gets "did not answer". Buttons from before the restart no longer
answer anything.

A button or reaction answers its own question. A typed reply naming an option, or its number,
answers only when exactly one open question in that chat waits for the person typing. With two
or more open, a typed "Approve" could mean either, so it is an ordinary message; use the
buttons. That stops a member from aiming your typed answer at their own request.

### Environment

A sidecar runs as you, with your files and your environment: every variable reaches it except
the model API keys, which are Attaché's own secrets (every endpoint's `key_env`,
`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` and `OPENAI_API_KEY` are removed). It also gets:

| Variable | Meaning |
|---|---|
| `ATTACHE_HOME` | the home folder |
| `ATTACHE_CHANNEL` | the channel name, e.g. `slack` |
| `ATTACHE_FILES` | a folder for downloaded attachments: `<home>/files/inbox/<channel>/` |
| `ATTACHE_WEB_URL` | Attaché's web page, e.g. `http://127.0.0.1:8765` |
| `ATTACHE_ENABLED` | always `True` |
| `ATTACHE_<KEY>` | each other value in `[channels.<name>]`, upper-cased |

### Lifecycle

Attaché runs `uv run <skill folder>/<script>`, where `<script>` comes from the skill's
`metadata: {attache-channel: "channel.py"}`: a plain file name in the skill's folder (letters,
digits, `_`, `-` and `.`; not a path). A skill whose name is anything else is skipped, with a
warning. The working directory is the skill folder, and the sidecar runs in its own process
group, so Ctrl+C in Attaché's terminal reaches it only as `stop`.

If the sidecar exits, Attaché restarts it after 1, 2, 4 … up to 60 seconds, and resets the delay
once it has stayed up for a minute. If reading its output fails (a line over 16 MiB, say),
Attaché kills the sidecar and everything it started before restarting it; it does the same when
a sidecar is still running 5 seconds after `stop`. One channel failing never stops the others,
the web page or the scheduler.

A sidecar that cannot run (no tokens yet, no number) should print why on stderr and then wait
for `stop` instead of exiting, so it isn't restarted every minute.

### Logging

What a sidecar writes to stderr appears on Attaché's stderr, the terminal it runs in. `ready`
and `log` events, ignored lines and restarts go to Attaché's log, the `attache` logger. Under
`attache run` and `attache chat` it shows INFO and above (other libraries show warnings only).

## Writing a channel

1. Make a skill folder with a `SKILL.md` whose metadata names the script:
   `metadata: {attache-channel: "channel.py"}`.
2. Give the script a PEP 723 header for its dependencies.
3. Read secrets from the keychain, settings from `ATTACHE_*`, and speak the protocol above,
   `ask` included. (Inside Attaché every channel has an `ask`; `SidecarChannel` turns it into
   the `ask` command.)
4. Add a `setup` subcommand that stores the secrets.
