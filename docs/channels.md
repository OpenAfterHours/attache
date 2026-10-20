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

## Limits

Turns from anyone but the owner are rate limited:

```toml
[limits]
per_sender_hour = 20   # turns per person per hour
per_chat_hour = 60     # turns per chat per hour
member_daily = 200     # turns per day across all non-owner senders
```

When a limit is hit, Attaché says so once and then stays quiet until the window resets.

## The sidecar protocol

One JSON object per line, UTF-8, in both directions. Lines that are not JSON, and objects with
an unknown `type`, are ignored. Anything the sidecar writes to stderr goes to Attaché's log.

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
  `ATTACHE_FILES`.
- `answer`: a button press or reaction answering an `ask`. Attaché accepts it only from the
  person who was asked.

### Attaché → sidecar (stdin)

```json
{"type": "send", "chat_id": "C0123", "text": "09:40 from Portsmouth.", "files": [],
 "reply_to": "1729.42"}
{"type": "ask", "chat_id": "D0ALEXDM", "ask_id": "a19",
 "text": "Sam asked: send the packing list as a checklist from now on?",
 "options": ["Approve", "Decline"]}
{"type": "typing", "chat_id": "C0123", "on": true}
{"type": "stop"}
```

- `send`: `files` are absolute paths on this machine. `reply_to` may be null.
- `ask`: show `text` with the options as buttons (Slack) or as a prompt answered by reaction,
  where 👍 means the first option and 👎 the second (Signal). A typed reply naming an option, or
  its number, also works on every channel.
- `typing` is optional; a sidecar may ignore it.
- `stop`: exit within 5 seconds. Attaché closes stdin straight after.

### Environment

| Variable | Meaning |
|---|---|
| `ATTACHE_HOME` | the home folder |
| `ATTACHE_CHANNEL` | the channel name, e.g. `slack` |
| `ATTACHE_FILES` | a folder for downloaded attachments: `<home>/files/inbox/<channel>/` |
| `ATTACHE_<KEY>` | each other value in `[channels.<name>]`, upper-cased |

### Lifecycle

Attaché runs `uv run <skill folder>/<script>`, where `<script>` comes from the skill's
`metadata: {attache-channel: "channel.py"}` and the working directory is the skill folder. If
the sidecar exits, Attaché restarts it after 1, 2, 4 … up to 60 seconds, and resets the delay
once it has stayed up for a minute. One channel failing never stops the others, the web page or
the scheduler.

## Writing a channel

1. Make a skill folder with a `SKILL.md` whose metadata names the script:
   `metadata: {attache-channel: "channel.py"}`.
2. Give the script a PEP 723 header for its dependencies.
3. Read secrets from the keychain, settings from `ATTACHE_*`, and speak the protocol above.
4. Add a `setup` subcommand that stores the secrets.
