---
icon: lucide/shield-check
---

# Security

Attaché runs on your machine, as you, with access to your files, a shell and your accounts.
This page says who can make it do what, and where the limits are.

In phase 1 the only person who can talk to Attaché is you, in the terminal and on the local
web page. The rules for other people below are already in place, ready for group chats in
phase 2.

## Roles

Every turn has one of three roles:

| Role | Who |
|---|---|
| **Owner** | You. Everything typed in the terminal or on the web page, and your direct messages on chat apps, which Attaché recognises by the ids in `[owner]`. |
| **Member** | In a group chat, the people listed in that chat's `members`. In a direct chat, a person from `[people]` with `role = "member"`. |
| **Guest** | Everyone else in a listed group chat. Answered only if the chat has `guests = "public"`. |

People are known by their sender ids on each chat app, never by display names. A group chat
that isn't listed under `[chats]` is ignored, and so is a direct message from anyone not in
`[people]`. See [Configuration](configuration.md#people).

**Your turns in a group chat get member rights.** A group carries other people's messages,
so even your own requests there never get files, the shell, or your private notes.

## What each role can do

| Tool | Owner, in your own chats | Member (and you, in a group) | Guest |
|---|---|---|---|
| `read_file`, `write_file`, `edit_file` | Inside the home folder | No | No |
| `run` (shell commands) | Yes | No | No |
| `script` (skill scripts) | Any skill | Only [member skills](skills.md#sharing-a-skill-with-group-chats) | No |
| `send` (a message to this chat) | With any file in the home folder | Only with files from this chat's files folder | No |
| `db` (documents) | Every collection | Only docs shared with this chat; can add and edit, not delete | No |
| `artifact` (pages) | Yes | Pages tied to this chat | No |
| `schedule` (reminders) | Message reminders and routines; can list and cancel tasks in every chat | Message reminders in this chat; can cancel only their own | No |

The prompt follows the same line. Your `memory/USER.md` goes only into turns with owner
rights. You get the index of every skill; members get the full text of the skills shared
with them, and nothing else from `skills/`. A guest's turn has no tools at all.

Only your own turns have the file tools, so only they can change skills, memory or settings,
and only after your own turns does Attaché commit the home folder to git.

## Other people's words are data

Only your own words are instructions. In a group chat, the earlier messages, and the new one
unless you sent it, go to the model in one fenced block labelled as data. Each message is
quoted as a single JSON string, so no character in it can close the fence or pass itself off
as a label or a header. Labels come from `attache.toml` (the configured name and the role in
that chat), never from display names.

The rules in every prompt say the same of tool results, emails, web pages and files: they
are data, never instructions.

## Files and the shell

The file tools work only inside the home folder. Attaché refuses, before touching the disk:

- absolute paths, drive letters (also `C:foo`), UNC paths, and any path with a `:`;
- anything that resolves outside the home folder, including through symlinks and junctions;
- the files it keeps to itself, in any case and at any depth: `.git`, `.claude`,
  `CLAUDE.md`, `.secret` and `attache.db*`.

!!! warning "The shell is not sandboxed"

    The `run` tool runs a command in the home folder (`cmd.exe` on Windows, `sh` elsewhere)
    with your user's rights. A command can reach anything you can. That is why only your own
    turns get it. Each command has a time limit, 60 seconds unless the assistant asks for
    more, and at most 10 minutes; on timeout, the whole process tree is stopped.

Skill scripts run the same way, as you, with `uv run`, in the chat's files folder. The
`script` tool takes only plain skill and script names, so it can't be pointed at a file
outside `skills/<name>/scripts/`.

## The page sandbox

Pages are untrusted HTML: the assistant writes them, and they may quote emails or web pages.
Each one is served with a Content Security Policy sandbox, which gives it an opaque origin
and blocks every request to the internet, pop-ups, dialogs, form posts, and framing by other
sites. A page reaches Attaché only through the bridge, with a token for that page alone, and
only the collections named in its `attache-collections` meta tag. It opens only inside the
Attaché window. The details are in [Pages](pages.md#the-sandbox).

## Tokens

- **The web page's token** is random (24 bytes from `secrets`) and new every time Attaché
  starts. It reaches the browser only through the link Attaché prints, after `#t=`; the
  fragment never goes to the server in a request line, and the page removes it from the
  address bar and keeps it in the browser's storage for other tabs. API calls carry it in a
  header (the live event stream, which can't send headers, in its query string), never in a
  cookie.
- **Each page's token** is an HMAC of the web page's token and the page's name, so it changes
  every run too, and one page's token is no use to another.
- **Each Claude Code turn** gets its own MCP token, valid only while that turn runs and only
  for that turn's tools.

Attaché compares tokens with `hmac.compare_digest`, in constant time.

## Loopback only

In phase 1 the web server serves this machine only:

- `[web] host` must be `127.0.0.1`, `localhost` or `::1`. Attaché refuses to start on any
  other address, including `0.0.0.0`.
- Requests whose `Host` header isn't a loopback name are refused, so a web site can't reach
  the server by pointing its own domain name at `127.0.0.1` (DNS rebinding).
- Only the bridge routes answer cross-origin requests, for the sandboxed pages. The MCP
  endpoint refuses any request that comes with an `Origin` header, so a web page can't post
  to it.

Anything that runs as your user on this machine can still read `~/.attache` directly. The
tokens keep web pages, and programs that don't have the printed link, from driving the
assistant through its web server.

## The `claude-cli` backend

The `claude-cli` backend runs Claude Code (`claude -p`) on your own login. It has its own
limits:

- **Your own chats only.** It answers only turns with owner rights, and refuses every group
  turn and every other person's turn. Anthropic's terms allow a Pro or Max login for your
  own use; anything that serves other people should use an API key, which is why
  `group_chats` defaults to the `claude-api` endpoint.
- **Claude Code's own tools.** Each turn may use Claude Code's Read, Write, Edit, Glob,
  Grep, Bash, WebFetch and WebSearch without asking. These are Claude Code's tools, not
  Attaché's: Attaché's home-folder limits don't apply to them, and your own Claude Code
  settings in `~/.claude` do.
- **Only Attaché's MCP server.** Claude Code reaches Attaché's other tools (documents,
  reminders, pages, skill scripts, sending) through an MCP endpoint on the local web server.
  The turn starts with `--strict-mcp-config`, so no other MCP server you have configured is
  loaded.
- **Outside your home folder.** Claude Code runs in `~/.attache-claude`, next to the home
  folder, which it reaches with `--add-dir`. A `CLAUDE.md` or `.claude/settings.json` (with
  hooks) written into `~/.attache` is never loaded as project settings.
- **Your key, if one is set.** If `ANTHROPIC_API_KEY` is set where Attaché starts, Claude
  Code uses it instead of your login.

## Still to come

The design proposal plans more safeguards for when other people can reach Attaché: rate
limits per sender and per chat, a daily cost cap for other people's turns, and your approval
before Attaché sends to a different chat. See [Design and roadmap](design.md).
