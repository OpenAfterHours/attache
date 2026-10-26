---
icon: lucide/settings
---

# Configuration

Attaché reads its settings from `attache.toml` in the home folder: `~/.attache/attache.toml`,
or the folder that `--home` or `ATTACHE_HOME` points to. `attache init` writes a commented
starting file.

## Editing in the web workspace

Open **Settings** in the [web workspace](workspace.md) to inspect the saved configuration
and the configuration Attaché is currently running. The forms and **Editable settings
(TOML)** view change the same supported fields: assistant name, model endpoints and routing,
web settings, rate limits, and whether existing channels are enabled.

Review the changes before saving. Attaché validates the proposal, checks that the file has
not changed since you loaded it, and saves it atomically with a previous-version backup.
If another editor changed the file, reload Settings and review your changes again. A restore
uses the same validation and conflict checks as a save.

The TOML view is an editable selection, not the complete source file. Comments, credentials,
identity rules and other file-managed options are preserved in the original file. Secret
values are never sent to the browser; endpoint credentials show their configured status.
Create new endpoints or channels, or edit options outside the selection, in `attache.toml`.
Some unusual TOML layouts cannot be changed safely in the browser; Settings will tell you
to edit that field in the file instead.

Saving does not restart Attaché or change an active conversation. Settings indicates when
a restart is needed. Stop and restart `attache` or `attache run`, then open the new launch
link; if you installed the startup service, restart it using the service's normal controls.

Settings is available only in the owner's local interface. Generated pages and public
share links cannot access it.

## Managing the configuration file

After upgrading Attaché, run `attache init` to add new bundled skills; it never overwrites
your files.

`attache init` also makes the home folder yours alone, an existing home included, so running
it after an upgrade locks down a home made by an older Attaché too. On macOS and Linux that
is `chmod 700`. On Windows it is an access list that lets in only you and SYSTEM, with
inheritance off, so everyone else, Administrators included, loses access to everything in
it. It does this only to a folder that is new, empty or already a home (it holds
`attache.toml`). It leaves any other folder as it is and says so, in case `--home` named
the wrong one.

Attaché reads the file once, when it starts, so restart `attache` or `attache run` after you
change it. Run `attache doctor` to check your edits; a mistyped key is reported with the keys
that section accepts:

```console
$ attache run
attache.toml: /home/sam/.attache/attache.toml: unknown key prot in [web]; known: host, port
```

## The file `attache init` writes

```toml
# Attaché settings. Restart `attache run` after editing.
# People and chats are keyed by platform ids, never by display names.

[assistant]
name = "Attaché"  # how the assistant names itself; also its trigger word in group chats

[owner]
name = "Sam"

# Your own sender id on each chat app, so your direct messages there reach you as the owner.
# Ids can also go straight in [owner] (or [people.<id>]), like slack = "U0123".
[owner.ids]
slack = ""

[routing]
own_chats = "claude"        # your own chat (terminal and web) and your direct messages
group_chats = "claude-api"  # group chats, and anyone but you

[web]
host = "127.0.0.1"  # only this machine can reach the web page
port = 8765
# Share links, for people off this machine, through Tailscale Funnel (docs/configuration.md).
# Off until public_url is set; then run `tailscale funnel --bg <public_port>`.
# public_url = "https://pc.tailnet.ts.net"  # https:// and your machine's name, no path
# public_port = 8766                        # serves share links only; default port + 1

# Models. api: anthropic | openai-chat | openai-responses | claude-cli. Other keys, such as
# max_tokens, are passed to the backend as options.
[endpoints.claude]
api = "claude-cli"  # your Claude Code login
model = "claude-sonnet-5-5"

[endpoints.claude-api]
api = "anthropic"
model = "claude-sonnet-5-5"
key_env = "ANTHROPIC_API_KEY"

# [endpoints.openai]
# api = "openai-responses"
# model = "gpt-5.4"
# key_env = "OPENAI_API_KEY"

# [endpoints.local]
# api = "openai-chat"
# base_url = "http://localhost:11434/v1"
# model = "qwen3:32b"

# People you share chats with. Keys other than name, role and ids are their ids per chat app.
# [people.sam]
# name = "Sam"
# role = "member"  # member | guest
# slack = "U0123"

# Group chats Attaché answers in, keyed "<channel>:<chat id>". Unlisted groups are ignored.
# [chats."slack:C0123"]
# name = "Family"
# engage = "mention"  # mention | always | a regular expression matched against the text
# members = ["sam"]   # person ids from [people]
# guests = "ignore"   # ignore | public
# endpoint = "claude-api"
```

## `[routing]`: which model answers which chat

Every chat is answered by one *endpoint*, a named `[endpoints.<name>]` block. `[routing]`
picks the two defaults:

| Key | Used for | Default |
|---|---|---|
| `own_chats` | Your terminal and web chat, and your direct messages on chat apps. | `claude`, or whatever you passed to `attache init --endpoint` |
| `group_chats` | Every group chat, and direct messages from anyone but you. | `claude-api` |

A chat listed under [`[chats]`](#chats-group-chats) can name its own `endpoint`, which wins
over both. Your own messages in a group chat also go to `group_chats`, because other people
can read the reply.

So does a page that asks the assistant with `attache.ask`, even one of your own pages. That
turn is the page's, not yours: it sees only the docs shared with the page's chat, and for
one of your own pages that means the docs shared with `local:me`, never everything. See
[Pages](pages.md#asking-from-a-page).

The split exists because of Anthropic's terms: a Claude Pro or Max login is for your own use,
so the `claude-cli` backend answers only your own chats and refuses every other turn. Anything
that serves other people needs an API key. Keep `group_chats` on an API endpoint.

Some common setups:

=== "Claude Code login (default)"

    ```toml
    [routing]
    own_chats = "claude"        # your Claude Code login
    group_chats = "claude-api"  # an API key, per Anthropic's terms
    ```

=== "Claude API key only"

    ```toml
    [routing]
    own_chats = "claude-api"
    group_chats = "claude-api"
    ```

=== "Local model only"

    ```toml
    [routing]
    own_chats = "local"
    group_chats = "local"

    [endpoints.local]
    api = "openai-chat"
    base_url = "http://localhost:11434/v1"
    model = "qwen3:32b"
    ```

Every name you use must have an `[endpoints.<name>]` block; `attache doctor` reports one
that doesn't.

## `[endpoints.<name>]`: models

Each endpoint picks a wire protocol (`api`), a model, and where to find it. The block's name
is yours to choose; it is what `[routing]` and `[chats]` refer to.

| Key | Meaning |
|---|---|
| `api` | Required. `claude-cli`, `anthropic`, `openai-chat` or `openai-responses`. |
| `model` | The model name, as the provider spells it. |
| `base_url` | Optional. The server to talk to, for anything other than the provider's own API. |
| `key_env` | Optional. The name of the environment variable that holds the API key. The key itself never goes in `attache.toml`. |
| `timeout` | Optional. Seconds a turn may take before Attaché gives up. Default `900`. |
| `max_steps` | Optional, not `claude-cli`. The most model calls one of your own turns may make (see below). Default `100`. Other people's turns never get more than `20`. |
| `max_tokens` | Optional, `anthropic` only. The most tokens a single model response may use. Default `4096`. |
| `bin` | Optional, `claude-cli` only. The Claude Code command, if it isn't `claude` on your `PATH`. |

If `key_env` is set but the variable is empty, the first turn on that endpoint fails with a
message naming the variable, and `attache doctor` reports it before that.

**Long jobs.** On the `anthropic`, `openai-chat` and `openai-responses` routes, Attaché runs the
tool loop, and a turn keeps going for as long as it is getting somewhere. It is wound up when
it goes round in circles (the same call has given the same result three times; the model is
told the second time), reaches `max_steps`, or has used most (80%) of its `timeout`. Winding
up never just cuts it off: the model is told to stop and gets one last step, with no tools, to
say what it did and what is left. The reply ends by saying why it stopped; say "continue" and
the next turn picks the work up from there. `timeout` stays the hard limit, and **Stop** (or
Esc, or Ctrl+C in the terminal) ends a turn at once. For a job longer than one turn should
take, raise both `max_steps` and `timeout`, or give it to a [background job](jobs.md).
On `claude-cli`, Claude Code runs its own loop, and only `timeout` and **Stop** bound it.

| `api` | Use it for | Who runs the tool loop |
|---|---|---|
| `claude-cli` | Claude Code (`claude -p`) on your own login. Your own chats only. | Claude Code |
| `anthropic` | The Claude API, or any server that speaks the Anthropic Messages API | Attaché |
| `openai-chat` | Ollama, LM Studio, vLLM, llama.cpp, OpenRouter, Groq, Azure | Attaché |
| `openai-responses` | OpenAI reasoning models with tools | Attaché |

### `claude-cli`: Claude Code on your login

```toml
[endpoints.claude]
api = "claude-cli"
model = "claude-sonnet-5-5"
# bin = "claude"   # the Claude Code command, if it has another name or isn't on PATH
```

Install [Claude Code](https://code.claude.com/docs/en/overview) and log in once with `claude`.
Attaché then runs `claude -p` for each turn of your own chats. Claude Code runs its own tool
loop with its own Read, Write, Edit, Glob, Grep, Bash, WebFetch and WebSearch tools, and
reaches Attaché's tools (to-dos, reminders, pages, skill scripts) through an MCP endpoint on
the local web server. `model` is passed to `claude --model`.

- It keeps one Claude Code session per chat and resumes it, so a long chat doesn't resend
  its history. A fresh session gets the recent conversation (the last 30 messages, up to
  20,000 characters) as a transcript.
- It runs in a folder next to your home, `~/.attache-claude`, so a `CLAUDE.md` or
  `.claude/settings.json` inside `~/.attache` never loads as Claude Code project settings.
  Your own `~/.claude` settings still apply.
- It needs no `key_env`. If `ANTHROPIC_API_KEY` is set in the environment Attaché starts in,
  Claude Code uses that key instead of your login.

What this backend may do is covered in [Security](security.md#the-claude-cli-backend).

### `anthropic`: the Claude API

```toml
[endpoints.claude-api]
api = "anthropic"
model = "claude-sonnet-5-5"
key_env = "ANTHROPIC_API_KEY"
# max_tokens = 4096
```

Create a key in the Claude Console and put it in the environment before you start Attaché:

=== "macOS and Linux"

    ```bash
    export ANTHROPIC_API_KEY="sk-ant-..."
    attache run
    ```

=== "Windows (PowerShell)"

    ```powershell
    $env:ANTHROPIC_API_KEY = "sk-ant-..."
    attache run
    ```

Set `base_url` to use any other server that speaks the Anthropic Messages API.

### `openai-chat`: Ollama and other compatible servers

Chat Completions is the format most self-hosted and third-party servers share. For a model
running in [Ollama](https://ollama.com) on this machine:

```bash
ollama pull qwen3:32b
```

```toml
[endpoints.local]
api = "openai-chat"
base_url = "http://localhost:11434/v1"
model = "qwen3:32b"
```

A local server needs no key, so leave `key_env` out. Pick a model that supports tool
calling: Attaché's to-dos, reminders and pages all work through tools.

For a hosted service, set its `base_url` and the variable that holds its key:

```toml
[endpoints.openrouter]
api = "openai-chat"
base_url = "https://openrouter.ai/api/v1"
model = "..."  # the model id, as OpenRouter lists it
key_env = "OPENROUTER_API_KEY"
```

### `openai-responses`: OpenAI reasoning models

OpenAI's reasoning models need the Responses API for tool calls, so they have their own
adapter:

```toml
[endpoints.openai]
api = "openai-responses"
model = "gpt-5.4"
key_env = "OPENAI_API_KEY"
```

Attaché sends each request with `store` off, so OpenAI keeps no conversation state: the
reasoning comes back encrypted, and Attaché replays it from its own copy of the turn.

## `[web]`: the local web page

```toml
[web]
host = "127.0.0.1"
port = 8765
```

| Key | Meaning |
|---|---|
| `host` | `127.0.0.1` (the default), `localhost` or `::1`. Attaché serves this machine only, and refuses to start with any other address. |
| `port` | The port for the web page. Default `8765`. Change it if something else uses that port. |
| `public_url` | Optional. The `https://` address where people off this machine open share links, such as `https://pc.tailnet.ts.net`: a host and perhaps a port, no path. Empty or left out, sharing is off. |
| `public_port` | The port on `127.0.0.1` that serves share links when `public_url` is set. Default `port + 1`, so `8766` (`65534` when `port` is `65535`). It must differ from `port`. |

If a port is taken when you start, Attaché stops with a message saying so:

```console
$ attache run
Port 8765 is in use. Is `attache run` already running?
```

### Share links through Tailscale Funnel

A page a member made, or one made in a group chat, can be shared with a link that works off
this machine, for 7 days and while the page stays as it is. Attaché still listens on this
machine only: [Tailscale Funnel](https://tailscale.com/kb/1223/funnel) carries the requests
from the internet to it.

1. Install Tailscale, log in, and allow Funnel for this machine in your tailnet's settings.
2. Set `public_url` to the machine's Funnel address:

    ```toml
    [web]
    port = 8765
    public_url = "https://pc.tailnet.ts.net"
    # public_port = 8766   # the default: port + 1
    ```

3. Restart Attaché, then point Funnel at `public_port`, which serves share links only:

    ```console
    $ tailscale funnel --bg 8766
    ```

**Point Funnel at `public_port`, never at `port`.** The server on `public_port` has only the
share link routes and what a shared page needs to run (its data and live updates); your
chat, your own pages and Claude Code's MCP endpoint are not there at all, and it answers only
requests for the `public_url` host. `tailscale funnel reset` turns it off. What a share link
can reach is in [Pages](pages.md) and [Security](security.md).

## `[assistant]` and `[owner]`

```toml
[assistant]
name = "Attaché"

[owner]
name = "Sam"

[owner.ids]
slack = "U0123ABCD"
```

- `[assistant] name` is how the assistant names itself, and the word that addresses it in a
  group chat.
- `[owner] name` is your name. Everything typed in the terminal or on the web page comes from
  you.
- `[owner.ids]` holds your own sender id on each chat app, so your direct messages there
  reach you as the owner. An id can also go straight in `[owner]`, as `slack = "U0123ABCD"`.
  Empty ids are ignored.

## `[people]`

!!! info "Phase 2"

    Chat apps (Slack and Signal) arrive in phase 2. Until then the only chat is your own, in
    the terminal and on the web page, and `[people]` and `[chats]` have nothing to act on.
    Attaché already reads them, so you can see how group chats will work.

People are keyed by an id you choose, and found by their sender id on each chat app, never
by display name:

```toml
[people.jo]
name = "Jo"
role = "member"   # member | guest
slack = "U04JOJOJO"
```

| Key | Meaning |
|---|---|
| `name` | How Attaché labels them in the conversation. Defaults to the id. |
| `role` | `member` (the default) or `guest`, for direct chats with them. In a group, their role comes from that chat's `members`. |
| `ids` | A table of channel ids, as an alternative to plain keys. |
| any other key | Their sender id on that channel, such as `slack = "U04JOJOJO"`. |

Someone who isn't listed can't start a direct chat with Attaché: their messages are ignored.

## `[chats]`: group chats

Attaché answers only in the group chats listed here, keyed `"<channel>:<chat id>"`. It
ignores every other group.

```toml
[chats."slack:C0123"]
name = "Family"
engage = "mention"
members = ["jo"]
guests = "ignore"
endpoint = "claude-api"
```

| Key | Meaning | Default |
|---|---|---|
| `name` | A name for the chat, used in the prompt. | the chat key |
| `engage` | When Attaché answers: `mention`, `always`, or a regular expression searched in each message. | `mention` |
| `members` | Person ids from `[people]` who are members in this chat. | none |
| `guests` | `ignore` or `public`: whether Attaché answers people who aren't members. | `ignore` |
| `endpoint` | The endpoint for this chat, instead of `[routing] group_chats`. | none |

With `engage = "mention"`, Attaché answers a native mention or a reply to one of its
messages, or a message containing its name as a word, in any case and with or without the
accent (`attache` matches `Attaché`). Messages it doesn't answer are still kept as context
for later turns.

In a listed group, you are the owner, the people in `members` are members, and everyone else
is a guest. With `guests = "ignore"`, guests' messages are neither answered nor stored. With
`public`, Attaché answers them too, without any tools. What each role may do is in
[Security](security.md#roles).
