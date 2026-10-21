---
icon: lucide/shield-check
---

# Security

Attaché runs on your machine, as you, with access to your files, a shell and your accounts.
This page says who can make it do what, and where the limits are.

You talk to Attaché in the terminal, on the local web page and in your direct messages. The
people and group chats you list in `attache.toml` can talk to it too, with fewer rights. With
sharing on, anyone with a share link can open that one page, and nothing else.

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

## Email

The [email channel](channels.md#email) turns mail into direct messages, and a From line is
easy to fake, so it passes a message on only when its sender is proven:

- **Only the receiving server's word counts.** That is the topmost `Authentication-Results`
  header: each server puts its own on top, and anything further down may have been written
  by the sender. It must say `dmarc=pass` for the From address's domain. A result inside a
  comment, a quoted string anywhere in that header, or a second DMARC result (one may be the
  sender's words echoed back) all fail. Gmail and Microsoft 365 add the header to every
  message; with a provider that doesn't, the topmost one could be the sender's own, so the
  channel is not safe there.
- **Exchange's own mark**, `X-MS-Exchange-Organization-AuthAs: Internal`, also counts, but
  only through Graph or Outlook classic on an Exchange mailbox, where Exchange strips it from
  mail arriving from outside. Every copy of it must say so. On Gmail or IMAP it means nothing.
- **Anything else fails closed**: no single From address, mail from the account itself,
  auto-replies, mailing lists and bulk mail are skipped too, and so is mail that doesn't
  mention the assistant.

DMARC proves the domain sent the message, not which person at it: anyone who can send as
that domain (its admin, or someone who has taken over the mailbox) passes. So an email
address is only ever a person's id under `[people]`, with that person's role, and never goes
in `[owner.ids]`: by email nobody gets owner rights. A reply goes only to the From address of
that person's latest proven message, never to its `Reply-To`, never Cc, and never as a new
email, so a message can't send Attaché's answer to someone else. Each reply is logged with
ids only.

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

Skill scripts run the same way, as you, with `uv run`, in the chat's files folder, for at
most 10 minutes. The `script` tool takes only plain skill and script names, so it can't be
pointed at a file outside `skills/<name>/scripts/`. Attaché sets `ATTACHE_SENDER`, `ATTACHE_ROLE`,
`ATTACHE_CHAT` and `ATTACHE_HOME` on every run itself, so a script can check who is asking
and nobody in a chat can choose those values ([Who is asking](skills.md#who-is-asking)).

A run with `background: true` may take up to 12 hours instead, at most four at once, and
stopping Attaché stops its whole process tree. When one fails, the person who asked hears
only that it failed (or the line the script chose for them), never its output, and you get a
notice in your approvals chat with what the script chose to tell you
([Long jobs](skills.md#long-jobs-in-the-background)).

## The page sandbox

Pages are untrusted HTML: the assistant writes them, and they may quote emails or web pages.
Each one is served with a Content Security Policy sandbox, which gives it an opaque origin
and blocks every request to the internet, pop-ups, dialogs, form posts, and framing by other
sites. A page reaches Attaché only through the bridge, with a token for that page alone, and
only the collections named in its `attache-collections` meta tag. It opens only inside the
Attaché window, except from a snapshot link (for the `render` skill) or a share link. The
details are in [Pages](pages.md#the-sandbox).

## Pages that ask

A page in the Attaché window can ask the assistant with `attache.ask`. That turn belongs to
the page, not to you:

- Its prompt reaches the model quoted as data from the page, like another person's message.
- It runs on the `group_chats` endpoint, never `claude-cli`, with no history.
- Its only tool is `db`, on the page's declared collections, through the member rules: the
  docs shared with the page's chat, or with `local:me` for one of your own pages. Owner
  rights never reach a page turn, even on your own page.
- It counts against the guests' daily budget and the per-chat limits, and nothing is stored
  in the chat's history.

Only a page's own token can ask (the web page's token alone can't), and never a share link.

## Share links

When `[web] public_url` is set, a page made in a chat gets a share link,
`<public_url>/s/<token>`, that anyone can open. See [Pages](pages.md#sharing-a-page).

- **Never your access.** Only pages tied to a chat can be shared. A link reaches the
  documents shared with that chat, in the collections the page declared when the link was
  made, under the member rules (add and edit, no delete). It can't use `attache.ask`, and
  no page, shared or not, reaches `_shares` or `_requests` (members' requests to you).
- **Capped writes.** Anyone with the link can write, so each link may make at most 60
  changes an hour (refused tries count too), each at most 64 KB, and adds no document once
  the chat has 500 in that collection; it can still edit the ones there. Over a cap, the
  answer is 429, 413 or 403. The size is checked from the `Content-Length` header before
  any of the body is read, so a write without one, or sent in chunks (whose size is known
  only at the end), gets 413 straight away. Pages in the Attaché window have no such caps.
- **Short-lived.** A link lasts 7 days, and dies at once if the page changes or is deleted.
- **Kept as a hash.** The token is 24 random bytes, and Attaché stores only its SHA-256 (in
  the read-only `_shares` collection), so the database holds no working links.
- **Not passed on.** The page opens top-level with `Referrer-Policy: no-referrer` and
  `frame-ancestors 'none'`, so the link doesn't reach other sites in a `Referer` header and
  no site can frame the page. The bridge sends the token in a header (the live event stream,
  in its query string), and the web server keeps no access log.

## The public port

Tailscale Funnel connects to Attaché from `127.0.0.1`, like everything on this machine, so a
local request and one from the internet look alike. Attaché doesn't tell them apart by
address or `Host` header. Instead, share links get a web server of their own:

- It listens on `127.0.0.1:<public_port>` (by default the web page's port plus one), only
  when `public_url` is set. Funnel forwards to that port; the web page's own port is never
  exposed.
- It serves only share links (`/s/<token>`), the bridge script, the fonts, and the
  document and live-event routes for share links, which take share tokens and nothing else.
  The chat page, other pages, chat, answers, `attache.ask` and the MCP endpoint don't exist
  there.
- It answers requests whose `Host` is the host of `public_url` or a loopback name
  (`127.0.0.1`, `localhost`, `[::1]`), since Funnel may pass either on. Any other name is
  refused. A loopback `Host` gets nothing more: the port has only the share routes.
- On the local web server, share tokens are refused, and `/s/` doesn't exist.

## Tokens

- **The web page's token** is random (24 bytes from `secrets`) and new every time Attaché
  starts. It reaches the browser only through the link Attaché prints, after `#t=`; the
  fragment never goes to the server in a request line, and the page removes it from the
  address bar and keeps it in the browser's storage for other tabs. API calls carry it in a
  header (the live event stream, which can't send headers, in its query string), never in a
  cookie.
- **Each page's token** is an HMAC of the web page's token and the page's name, so it changes
  every run too, and one page's token is no use to another.
- **Each snapshot link** (for the `render` skill) carries a random token for one page, kept
  only in memory and valid for 10 minutes.
- **Each share link** carries a random token that lasts 7 days, stored only as a hash (see
  [Share links](#share-links)).
- **Each Claude Code turn** gets its own MCP token, valid only while that turn runs and only
  for that turn's tools.

Attaché compares the web page's and each page's token with `hmac.compare_digest`, in
constant time. The random tokens (snapshot, share and MCP) are looked up instead; share links
by their hash.

## Loopback only

The web servers serve this machine only (Funnel reaches the public port from this machine
too):

- `[web] host` must be `127.0.0.1`, `localhost` or `::1`. Attaché refuses to start on any
  other address, including `0.0.0.0`. The public port is always on `127.0.0.1`.
- Requests whose `Host` header isn't a loopback name are refused, so a web site can't reach
  the server by pointing its own domain name at `127.0.0.1` (DNS rebinding). The public
  port also takes the host of `public_url`, and serves share routes only.
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

## Limits and approvals

Turns from anyone but you, page turns included, are rate limited per sender, per chat and
per day; see [Channels](channels.md#limits). When a member asks for something only you can
do, the assistant asks you first, and acts only on your yes. See
[Design and roadmap](design.md) for what comes next.
