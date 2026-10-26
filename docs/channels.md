# Channels

A channel connects Attaché to a chat app. The terminal and the web page are built in. Every
other channel is a skill that ships a small program, a **sidecar**, which Attaché starts with
`uv run`. A sidecar has its own dependencies (declared in its PEP 723 header), so the core never
installs them, and it can be written in any language as long as it speaks the protocol below.

Phase 2 ships two: **Slack** (`skills/slack`) and **Signal** (`skills/signal`). A third,
**email** (`skills/email`), lets the people you list write to Attaché by email; see
[Email](#email).

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

## Email

The `email` channel watches the inbox of one account of the [mail skill](mail.md) and turns
each new message that asks for `@attache` into a direct message from its sender. Attaché's
answer goes back by email, as a reply in the same thread.

### Setting it up

1. Set the account up in the mail skill and check it: `attache skill mail doctor` says `ok`.
   Give it a `user = "you@example.com"` line in `accounts.toml` if it has none (Outlook
   accounts often don't), so that mail from your own address is never taken for a request.
   If your home was made before the email channel (0.0.2 or earlier), update the mail skill
   first: see [Updating the mail skill](mail.md#updating-the-mail-skill). Until then the
   channel refuses to start, and says so.
2. Turn the channel on and list who may write:

    ```toml
    [channels.email]
    enabled = true
    account = "gmail"          # a section of skills/mail/accounts.toml
    # every = "60"             # seconds between checks
    # mention = "@attache"     # what a message must contain; case and accents don't matter
    # signature = "..."        # the line under each reply; by default it says to start
    #                          # an answer with the mention
    # authserv_id = "mx.google.com"   # Gmail: the server whose results count (below)

    [people.dan]
    name = "Dan"
    email = "dan@example.com"  # the From address, which is also the chat id
    ```

3. Restart `attache run`. The first start answers nothing already in the inbox: only mail
   that arrives from then on. The channel checks every minute, at most 50 messages at a time,
   oldest first (a backlog is worked through over the next checks), and keeps what it has
   seen in `files/inbox/email/.state.json`, so a restart answers nothing twice.

Chat keys look like `email:dan@example.com`: each sender is a direct chat of their own, never
a group. Case doesn't matter in the address. The person's role applies as in any direct
message, so a `guest` is ignored.

**An email address in `[owner.ids]` is refused**, and so is an email chat as your
`approvals` chat. An email proves at best its sender's domain, and owner rights include
files, the shell and your memory. Write to Attaché in your own chats.

### Who gets an answer

A message is passed on only when all of these hold:

- **Its new part mentions Attaché.** The new part is the text above the quoted history (`On …
  wrote:`, `>` lines, `-----Original Message-----` or Outlook's `From:`/`Sent:` block); that
  history is cut before Attaché sees the text, which is at most 4,000 characters. A mention
  in the subject or in the quoted history doesn't count, so "thanks" in a thread called
  "@attache run the VA" is not a new request. To ask again, write `@attache` again.
- **It isn't one of Attaché's own.** A new part holding the reply signature is skipped, and
  so is anything automatic (below), so two assistants can't answer each other for ever.
  Replies sent through IMAP (Gmail, Titan) also carry `Auto-Submitted: auto-replied`.
- **A person sent it**, not the account itself: no `Auto-Submitted` (other than `no`),
  `List-Id` or `List-Unsubscribe`, and no `Precedence` of `bulk`, `list` or `junk`.
- **The sender is proven.** The topmost `Authentication-Results` header must say
  `dmarc=pass` with `header.from` equal to the From address's domain. The receiving server
  puts its own header on top; any further down may have been written by the sender, so they
  never count. Gmail (`mx.google.com`) and Microsoft 365 add one to every message: use the
  channel only with a provider that does. With `authserv_id` set, the topmost header's first
  field, its server name before the first `;`, must be exactly that name (case aside):
  `mx.google.com` for Gmail. Microsoft 365 names no server there, so leave it unset. On Exchange (Graph, or Outlook classic on an Exchange mailbox),
  `X-MS-Exchange-Organization-AuthAs: Internal` also counts. Exchange removes that header
  from mail coming from outside, so it means Exchange took the message as internal, not that
  a person is proven: a relay inside your organisation, or a delegate using Send As or Send
  on Behalf, gets it too. Everything else fails, so mail that was forwarded, or comes from a
  domain without DMARC, gets no answer.
- **Attaché knows the address**: it is someone's `email` under `[people]`.

Only one of these skips leaves a trace: a message that asks for Attaché from an unproven
sender is logged by its id ("sender not authenticated"), never its address or subject. Mail
that was never for Attaché (no mention, a newsletter, your own) is skipped without a line, as
Attaché's log shows in your terminal. An unknown address is the core's to ignore.

**Not supported**: mail that doesn't reach the inbox straight from the sender's server. The
topmost `Authentication-Results` may then not be your provider's verdict on the sender, so
these must not be relied on:

- Gmail's "Check mail from other accounts" (POP) fetching another mailbox into this one;
- mail imported, or moved in from another account or archive;
- a filtering gateway (Mimecast, Proofpoint and the like) in front of the provider, which
  then judges the gateway, not the sender.

### Replies

- A reply answers the message it is about: the one the turn came from, in that thread. A
  message from a reminder or a background job (the VA finishing, say) answers that person's
  latest message.
- It goes to that message's From address and nobody else: never to its `Reply-To`, never Cc
  or reply-all. The draft is checked before it goes; one addressed any other way stays
  unsent in Drafts.
- If that message can't be found any more, because you moved or filed it (Outlook classic
  and Titan ids change then), the reply goes as a new email to the same address, under
  "Re:" and its subject, with the same check. The same happens if making the reply fails
  part way (Graph busy, say); a reply draft half made by then stays unsent in Drafts, to
  delete there.
- Attaché never writes to an address that hasn't written to it. With no message from an
  address, the reply is dropped, with a warning in the log.
- Replies are text: files are not attached. A question with options ends with "Reply 1 for
  Approve, 2 for Decline."
- Each reply is sent straight away, as the answer to that person's own request, logged by
  message id, and audited in `logs/mail.jsonl` with ids only. The mail skill's Send button
  is for your own mail, not for these.

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
starts again it asks the owner once more. Right after a start no sidecar may be up yet and no
page open, so while nothing can show the question it tries again every 30 seconds, until the
request's time runs out; only then does the member get "did not answer". Buttons from before
the restart no longer answer anything.

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

## Activity: how a turn is going

While a turn with owner rights runs, Attaché reports how it is going: its steps (each tool it
calls) and the model's thinking. Only your own turns report it, never a group's or a
member's. The terminal shows it as a trail of plain, indented lines under the turn, and the
web page in its "Thinking…" bubble.

Inside Attaché, the router and the tool loop call `ChannelManager.activity(chat_key, kind,
**data)` and await it. It reaches each channel of that chat's name that has a method
`async activity(chat_id, kind, data) -> None`; a channel without one is skipped. Today only the
two `local` channels have it, the terminal and the web page. The sidecar protocol has no
activity event, so a chat app never sees steps or thinking.

| `kind` | `data` | When |
|---|---|---|
| `started` | `route`: the endpoint's name | a turn begins |
| `step` | `label`: a short name for the call, at most 80 characters, never its contents (`script mail inbox`, `Bash ls -la`); `seconds`: how long it took, or `null` if not known | after each tool call |
| `thinking` | `text`: the full text the model sent | when the model thinks out loud |
| `finished` | `seconds`; `tokens`: input plus output, or `null`; `route`; `stopped`: true if it was stopped | after the reply was sent, or after a stop |
| `new` | none | the owner started the chat afresh with `/new`, after its turns were stopped; the web page reloads the chat |

**A rule for channel authors:** a channel that gains `activity` must never show a step's label
or the thinking in a chat other people can read. Thinking can quote your mail, and step labels
name your files and commands.

**Stopping a turn.** `ChannelManager.stop_turns(chat_key)` cancels what is running for that
chat's messages and returns whether anything was running. That includes messages queued
behind the running turn, waiting for it to finish: Stop means stop what I asked. The turn
then ends with `finished` and `stopped: true`, and sends no reply. Ctrl+C in `attache chat` stops a turn of yours this
way; with none running, it quits as before. The web page's Stop button does the same, and so
does `/new` before it starts the chat afresh. It never cancels the task that calls it.

## Writing a channel

1. Make a skill folder with a `SKILL.md` whose metadata names the script:
   `metadata: {attache-channel: "channel.py"}`.
2. Give the script a PEP 723 header for its dependencies.
3. Read secrets from the keychain, settings from `ATTACHE_*`, and speak the protocol above,
   `ask` included. (Inside Attaché every channel has an `ask`; `SidecarChannel` turns it into
   the `ask` command.)
4. Add a `setup` subcommand that stores the secrets.
