---
icon: lucide/rocket
---

# Quickstart

This gets Attaché running on your machine and takes you through a first conversation. It
takes about five minutes.

## What you need

- **[uv](https://docs.astral.sh/uv/getting-started/installation/).** It installs Attaché,
  fetches Python 3.11 or later if you don't have it, and runs skill scripts.
- **git.** Attaché keeps its home folder in git, so every change to a skill is a commit you
  can undo. Without git it still runs, but keeps no history.
- **A model.** The default setup uses [Claude Code](https://code.claude.com/docs/en/overview)
  (the `claude` command) on your own login, for your own chats. To use an API key or a local
  model instead, edit `attache.toml` after `attache init`; see
  [Configuration](configuration.md).

Attaché runs on Windows, macOS and Linux.

## Install

=== "Try it"

    ```bash
    uvx attache
    ```

    The first run asks your name, creates `~/.attache`, and opens a chat. uv keeps the
    package in its cache, so later runs start straight away.

=== "Keep it"

    ```bash
    uv tool install attache
    ```

    This puts the `attache` command on your `PATH`. Upgrade later with
    `uv tool upgrade attache`, then run `attache init` to add any new bundled skills; it never
    overwrites your files. To start Attaché at login, run `attache skill service install`.

!!! note "The latest, unreleased version"

    To try what's on `main` before it reaches PyPI, install from GitHub instead:

    ```bash
    uvx --from git+https://github.com/OpenAfterHours/attache attache
    uv tool install git+https://github.com/OpenAfterHours/attache
    ```

## Set up your home folder

`attache init` creates your home folder, `~/.attache`, and fills it in:

```console
$ attache init --name Sam
Home: /home/sam/.attache
  created attache.toml
  created .gitignore
  created artifacts/home.html
  created memory/USER.md
  created memory/ATTACHE.md
  created skills/artifact-kit
  created skills/reminders
  created skills/skill-writer
  created skills/todos
Next:
  1. Tell Attaché about yourself in /home/sam/.attache/memory/USER.md.
  2. Check the endpoints in /home/sam/.attache/attache.toml ("claude" uses your Claude Code login).
  3. Run `attache` to chat here, or `attache run` for the web page alone.
```

Running plain `attache` when there is no home yet does the same, after asking
`Your name [sam]:`, and then starts the chat.

`attache init` never overwrites a file, so it is safe to run again. It takes these options:

| Option | What it does |
|---|---|
| `--name NAME` | Your name. The default is your user name. |
| `--endpoint NAME` | The endpoint your own chats use, written into `[routing] own_chats`. The default is `claude`, your Claude Code login. The other one ready to use is `claude-api`; for the others, uncomment their block in `attache.toml` afterwards. |
| `--home PATH` | Put the home folder somewhere else. Every command takes `--home`, or reads `ATTACHE_HOME`. |

What ends up in the home folder:

| Path | What it holds |
|---|---|
| `attache.toml` | Settings: models, people and chats. See [Configuration](configuration.md). |
| `memory/USER.md` | What Attaché knows about you. Fill in your time zone and preferences; Attaché adds lasting facts as you share them. |
| `memory/ATTACHE.md` | The assistant's persona. Edit it to change its manner. |
| `memory/chats/` | One notes file per chat. |
| `skills/` | Your copies of the bundled skills. See [Skills](skills.md). |
| `artifacts/home.html` | The Home page. See [Pages](pages.md). |
| `files/` | A folder per chat for files, where skill scripts run. |
| `attache.db` | Messages, to-dos and other documents, reminders. Created on first run. |

The home folder is a git repository. After each of your turns, Attaché commits `skills/`,
`memory/`, `artifacts/` and `attache.toml`. The database and `files/` are not committed.

## Start it

```bash
attache        # the terminal chat, plus the web page
```

```console
$ attache
Open http://127.0.0.1:8765/#t=Q2x1ZS1vbmx5LWFuLWV4YW1wbGUtdG9rZW4
Chat here, or open http://127.0.0.1:8765/#t=Q2x1ZS1vbmx5LWFuLWV4YW1wbGUtdG9rZW4
Type /new for a new chat, /quit to leave.
```

`attache chat` does the same. To run the web page and reminders without a terminal chat,
use `attache run`, and stop it with ++ctrl+c++.

## Open the web page

Open the link Attaché printed. The part after `#t=` is a token that lets the page talk to
Attaché. It is random and changes every time Attaché starts, so other programs and web sites
on your machine can't drive your assistant.

The page takes the token out of the address bar and remembers it for other tabs while that
run lasts. After a restart, open the new link; a tab still holding the old token says
*Open Attaché from the link printed in your terminal*.

The web page has the chat, and a Pages panel that opens on **Home**: today's to-dos by
status, a form to add one, and the upcoming reminders.

## Have a first chat

Type in the terminal or on the web page. They are one conversation: a message typed in
either place gets its reply in both. In the terminal, replies start with `attaché>`.

Some things to try:

- `add a to-do: book the dentist on Friday`, and watch it appear on Home.
- `remind me in 20 minutes to stretch`. When it comes due, the chat shows
  `Reminder (set by Sam): Stretch`.
- `every weekday at 7:30, give me a summary of today's to-dos and reminders`. This sets a
  routine that runs as a request each morning.
- `what's on my plate?`
- `from now on, anything I need to buy goes on the Shopping list`. Attaché writes rules
  like this into a skill, here most likely `todos`, and `attache skills log` shows the commit.
- `make me a board for my Trip list`. The new page appears under Pages.

Reminders fire while Attaché is running, with `attache` or `attache run`. Any that came due
while it was stopped fire once when it starts again.

Type `/quit` or `/exit`, or press ++ctrl+c++, to leave.

## Start a new chat

Type `/new`, or choose **New chat** on the web page, to start the conversation afresh. If
Attaché is working on something in this chat, it stops first. From then on it doesn't see
the earlier messages, and the web page clears. The messages are still kept in `attache.db`,
though. Nothing else changes: your memory files, to-dos, reminders and pages
stay as they are.

`/new` works the same in your own direct messages on a chat app. In a group chat, or from
anyone else, it is an ordinary message.

## Check your setup

`attache doctor` checks the setup and prints one line per check:

```console
$ attache doctor
✓ home folder /home/sam/.attache
✓ attache.toml parses
✓ endpoint claude
! endpoint claude-api needs ANTHROPIC_API_KEY
✓ git installed
✓ uv installed
✓ web port 8765 free
```

| Mark | Meaning |
|---|---|
| `✓` | Fine. |
| `!` | Needed only for group chats, which arrive in phase 2. Safe to ignore for now. |
| `i` | A note, such as `web port 8765 in use: Attaché may already be running`. |
| `✗` | A problem. `attache doctor` then exits with code 1. |

It checks that the home folder exists and `attache.toml` parses; that each endpoint your chats
use is defined and has what it needs (the `claude` command for `claude-cli`, the environment
variable named by `key_env`, or the OS credential named by `key_ref`, for the others); that
git and uv are installed; and that the web port is free. Credential checks are local and do
not test authentication with the provider.

For desktop API keys, use `attache skill credentials login ENDPOINT` in your own terminal,
then restart Attaché. Run `attache init` first after upgrading to add the credentials skill.
Existing environment keys keep working; migration is optional. See
[keyring setup](configuration.md#store-an-api-key-in-the-os-keyring).

Common fixes:

- **`✗ endpoint claude needs claude`**: install Claude Code and log in, or point
  `own_chats` at another endpoint.
- **`✗ endpoint claude-api needs ANTHROPIC_API_KEY`** (when your own chats use it): set the
  environment variable, then start Attaché again.
- **`✗ endpoint local needs an [endpoints] block`**: `[routing]` or a chat names an endpoint
  that `attache.toml` doesn't define. Add the block, or fix the name.
- **`✗ [web] host = '0.0.0.0': use 127.0.0.1, localhost or ::1 for now`**: phase 1 serves
  this machine only.

## Next

- Choose your models in [Configuration](configuration.md).
- Teach it how you work: [Skills](skills.md).
- Ask for boards and trackers, or write your own: [Pages](pages.md).
