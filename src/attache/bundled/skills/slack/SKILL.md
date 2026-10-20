---
name: slack
description: Connects Attaché to Slack over Socket Mode, with no public URL, so people can
  message it directly or in channels and the owner can approve requests with buttons. Use when
  setting up, checking or troubleshooting Slack, or when someone asks how to reach Attaché
  from Slack or how to add a Slack channel or person.
metadata:
  attache-channel: "channel.py"
---

# Slack

`channel.py` is a sidecar: Attaché starts it with `uv run` and swaps JSON lines with it (see
`docs/channels.md`). It connects out to Slack over Socket Mode, so no port is opened and no
public URL is needed. Through it Attaché:

- answers direct messages, and messages in channels it has been invited to;
- receives files people share (saved under `files/inbox/slack/`) and sends files back;
- replies in a thread in channels, and inline in direct messages;
- shows approval requests as buttons. A typed reply naming an option also works.

The tokens live in the operating system's keychain (service `attache-slack`), never in
`attache.toml` or in chat. If someone pastes a token into a chat, tell them to revoke it in
the app's settings and run `setup` again.

## Setup

1. Go to <https://api.slack.com/apps>, choose **Create New App → From a manifest**, pick the
   workspace and paste the contents of `attache-slack.yml` from this folder.
2. **Install to Workspace** and allow it. The **Bot User OAuth Token** (`xoxb-…`) is under
   **OAuth & Permissions**.
3. Under **Basic Information → App-Level Tokens**, choose **Generate Token and Scopes**, add
   the `connections:write` scope and copy the token (`xapp-…`).
4. In a terminal, store both tokens. It asks for them without showing what you type:

   ```
   uv run ~/.attache/skills/slack/channel.py setup
   ```

   In PowerShell, write `$HOME` instead of `~`. Use your own home folder if Attaché lives
   somewhere else.
5. Test them. With your member id, `check` also prints your DM's chat key for approvals:

   ```
   uv run ~/.attache/skills/slack/channel.py check U0123ABCD
   ```

The agent may run `check` for the owner, but `setup` needs the owner at a terminal.

## attache.toml

```toml
[channels.slack]
enabled = true

[owner]
approvals = "slack:D0123ABCD"  # optional: your DM with the bot, so approvals come as buttons

[owner.ids]
slack = "U0123ABCD"   # your member id: your profile → ⋯ → Copy member ID

[people.sam]
name = "Sam"
slack = "U0456EFGH"   # Sam's member id, from Sam's profile

[chats."slack:C0789IJKL"]   # channel name → About → Channel ID, at the bottom
name = "Team"
engage = "mention"
members = ["sam"]
```

`[owner]` and `[owner.ids]` are already in the file: add the lines to them. Restart
`attache run` after editing.

## Using it

- Invite the bot to each channel with `/invite @attache` (or pick **Attaché** from the list).
  It sees only channels it is in, and answers only in chats listed under `[chats]`.
- Direct messages: open **Attaché** under **Apps** in the sidebar. Only the owner and people
  in `[people]` get answers.
- Members of a channel reach only the docs shared with that chat, and never the owner's files,
  shell or memory. Owner messages inside a channel get member tools too.
- With `engage = "mention"`, Attaché answers an `@attache` mention, a reply in a thread it
  started, or a message containing its name.

## Limits

- On the free plan Slack shows only 90 days of history. Attaché keeps its own history, so its
  memory is not limited by that.
- The bot cannot read channels it is not in, or other people's direct messages.
- Slack has no typing indicator for bots, so none is shown while Attaché thinks.

## Troubleshooting

- `check` says `invalid_auth` or `not_authed`: the token was revoked or mistyped. Run `setup`
  again (press Enter to keep a token that still works).
- Nothing arrives: check that **Socket Mode** is on and **Event Subscriptions** lists the bot
  events from the manifest. After changing scopes, reinstall the app.
- Files don't download: the app needs the `files:read` scope; reinstall after adding it.
- `not_in_channel` in Attaché's log: invite the bot to that channel.
- "cannot read the keychain" on Linux: install and unlock a Secret Service keyring (for
  example GNOME Keyring).
