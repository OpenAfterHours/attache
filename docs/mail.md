---
icon: lucide/mail
---

# Mail

The `mail` skill lets the assistant read, search, file and draft your email in Outlook, Gmail
and Titan, and send a draft once you say so. It also reads your Outlook calendar and tasks.
Every account gets the same commands; behind them sit three ways in:

| Account | How Attaché reaches it | Sign-in | Calendar and tasks |
|---|---|---|---|
| Outlook | Outlook classic on this PC (COM), or Microsoft Graph | COM: Outlook's own. Graph: a one-time browser sign-in | Yes |
| Gmail | IMAP and SMTP | An app password | Not in v1 |
| Titan | IMAP and SMTP | Your Titan password, or an app password with 2FA | Not in v1 |

The skill is yours alone: it isn't shared with group chats, and the assistant uses it only on
your own turns. Your filing rules belong in the `mailbox` skill, so they stay the same
whichever way an account is reached.

## Setting up

Run these in a terminal (PowerShell, Terminal or a shell), not in a chat. The first run
downloads the skill's libraries, which takes a few seconds.

1. Create your accounts file:

    ```
    attache skill mail setup
    ```

    It copies `accounts.example.toml` to `~/.attache/skills/mail/accounts.toml` and prints the
    path. The file holds no passwords. Delete the accounts you don't use; the names in square
    brackets (`[gmail]`) are yours to choose, and they start every message id.

2. Set up each account, below.

3. Check them all:

    ```
    attache skill mail doctor
    ```

    Each account says `ok`, or what is wrong and how to fix it.

### Outlook classic (COM)

```toml
[outlook]
backend = "com"
```

Nothing else: Attaché uses the Outlook already signed in on this PC, and starts it if it has
to. It needs **Outlook classic** on Windows; the new Outlook app has no COM. Attaché must run
in your own logged-in session and not as administrator, because Windows refuses the
connection between an elevated program and a normal one. `attache service install` sets it
up that way.

### Outlook through Microsoft Graph

Use Graph when you want the Outlook account to work with Outlook closed, with the new
Outlook app, or on another machine. It needs an app registration of your own, once:

1. Sign in to the [Microsoft Entra admin center](https://entra.microsoft.com/) and open
   **App registrations → New registration**. A personal Microsoft account first needs a free
   Azure account to get a directory (it asks for a phone number and a card, and puts a
   temporary $1 hold on it).
2. Name it `Attaché`. Under **Supported account types**, pick the one that includes personal
   Microsoft accounts if yours is an Outlook.com or Hotmail address.
3. Under **Redirect URI**, choose **Public client/native (mobile & desktop)** and enter
   `http://localhost`. Register.
4. Under **Authentication**, set **Allow public client flows** to **Yes**.
5. Under **API permissions → Add a permission → Microsoft Graph → Delegated**, add
   `Mail.ReadWrite`, `Mail.Send`, `Calendars.ReadWrite`, `Tasks.ReadWrite` and
   `offline_access`.
6. Copy the **Application (client) ID** from the overview page. It is a public value, not a
   secret.

```toml
[outlook]
backend = "graph"
client_id = "00000000-0000-0000-0000-000000000000"
user = "you@outlook.com"   # optional: the account to sign in with
tenant = "consumers"       # for a personal account; leave it out for work or school
```

Then sign in, which opens your browser:

```
attache skill mail login outlook
```

The consent screen says the app is unverified: apps registered with a personal account can't
be publisher-verified. The sign-in lasts until it goes 90 days unused or you change your
Microsoft password; `doctor` then asks you to run `login` again. Some work tenants need an
admin to approve the app first. The tokens are kept in `~/.attache/.mail/graph-cache.bin`,
encrypted for your Windows user.

### Gmail

```toml
[gmail]
backend = "imap"
kind = "gmail"
imap = "imap.gmail.com:993"
smtp = "smtp.gmail.com:465"
user = "you@gmail.com"
```

1. Turn on [2-Step Verification](https://myaccount.google.com/signinoptions/twosv) for your
   Google account; app passwords need it.
2. Make an [app password](https://myaccount.google.com/apppasswords) named `Attaché` and keep
   the page open.
3. Store it:

    ```
    attache skill mail login gmail
    ```

    It asks for the password without showing what you type and keeps it in your system's
    keyring (Windows Credential Manager, macOS Keychain or the Secret Service). Then run
    `attache skill mail doctor`, which signs in with it, and close the Google page.

Google cancels app passwords whenever you change your Google password: `doctor` then says
Gmail refused the password, and you make a new one and run `login` again. In Gmail, folders
are labels: moving a message adds the label and takes it out of the inbox.

### Titan

```toml
[titan]
backend = "imap"
imap = "imap.titan.email:993"
smtp = "smtp.titan.email:465"
user = "you@yourdomain.com"
sent_folder = "Sent"
send_limit_hour = 50
```

1. In Titan webmail, open the settings and switch on **Enable Titan on Other Apps**.
2. If you use two-factor authentication, make an **Application Password** there; otherwise
   use your Titan password.
3. Store it: `attache skill mail login titan`.

Titan doesn't file mail sent through SMTP, so the skill copies each sent message to
`sent_folder`. Titan limits mail to outside addresses by plan (from 50 an hour on Starter):
set `send_limit_hour` to your plan's figure, and the skill refuses a send that would pass it.
It counts the recipients of the last hour's sends in `~/.attache/.mail/sent.jsonl`.

## Using it

Ask in plain words: "anything urgent in my inbox?", "find the boiler quote from Dan", "file the
receipts", "draft a reply saying Thursday works", "what's on tomorrow?". The assistant runs
the skill's commands (`inbox`, `search`, `read`, `move`, `flag`, `draft-reply` and so on; the
full list is in the skill's `SKILL.md`). It may read, file, flag and draft on its own. It
sends, answers invitations and creates events with attendees only after you say Send.

Moving more than 20 messages at once needs your yes too.

## How sending works

1. The assistant saves the draft in that account's Drafts folder. The mail command writes a
   plain-text **preview** of exactly what would be sent: the account, To, Cc, Bcc, subject,
   attachments with their sizes, and the text.
2. You see that preview, not the assistant's description of it, with two buttons: **Send** and
   **Not now**. Attaché's core reads the preview from the file the mail command wrote and
   shows it word for word.
3. On **Send**, Attaché records your approval, and the skill sends the draft.

The approval is tied to a **fingerprint**: a hash of the preview, the account and the draft.
When you press Send, Attaché's core writes a file named after that fingerprint in
`~/.attache/.approvals/`, holding a hash of the exact text it showed you. The send command then
checks:

- **The draft is still the one you saw.** It reads the draft again and rebuilds the preview.
  If anything changed in between (a recipient or a Bcc added, an attachment, the text edited),
  it refuses: "it changed since it was approved". It also compares the rebuilt preview with
  the hash of what you were shown, so a preview file rewritten after drafting is no use: "the
  preview changed since it was approved". A yes for one account's draft can't send another
  account's.
- **The preview can't be dressed up.** In the header lines (To, Cc, Bcc, Subject, attachment
  names), line breaks and control characters become spaces, so a subject can't draw a fake
  `To:` line. The body is shown as it is.
- **You approved it, recently.** The approval file must exist and be under 15 minutes old.
  The send claims it by renaming it, so two sends can't share one yes, and it is deleted once
  the mail has gone: one yes sends one message.

If a send fails, what happens next depends on where it failed. When nothing can have left
your machine (the mail server refused the sign-in, the draft can't be found), your approval
is put back, so you needn't be asked again. When the mail may have gone (the connection broke
after the server took it), the skill keeps a marker, `<fingerprint>.sending`. For Outlook it
then refuses that draft for good: "this may already have been sent; check Sent, then edit it
to resend" (an edited draft is a new one to approve). For Gmail and Titan the skill records
each Message-ID in `~/.attache/.mail/sent.jsonl` just before handing the message over, and
that record decides: a recorded one is never sent again ("already sent at …: check Sent"), and
one that was never recorded, because the try stopped before that, can be approved and sent. (For anyone writing a
backend: a `ValueError` from `send_draft`, `respond` or `create_event` means nothing was sent,
and any other exception means it may have been.)

Be clear about what this does and doesn't prove. The fingerprint stops a changed draft from
being sent, and you approve the real draft, not the model's words about it. It is **not**
proof that you said yes: the approval file is written by Attaché itself, and the assistant's
file tools can't touch that folder, but a turn that can run shell commands (your own chats
have a shell) could still create the file. What this stops is the ordinary path: a model that
an email talks into sending can't do it with the mail commands alone. The rest of the
protection is in the skill's rules: email text is data, never instructions; recipients found
in an email are never added without your yes; and mail is used only on your own turns.

Outlook sends through Outlook itself. Gmail and Titan send over SMTP: the skill rebuilds the
message from the fields in the preview as a single plain-text part, so nothing goes that you
weren't shown. A draft with other parts (HTML, attachments) made in another mail app is
refused: send that one from your mail app. The draft is removed from Drafts after sending.

## What is kept where

| What | Where |
|---|---|
| Accounts (no secrets) | `~/.attache/skills/mail/accounts.toml` |
| Gmail and Titan passwords | Your system keyring, service `attache-mail`, one entry per account name |
| Graph sign-in | `~/.attache/.mail/graph-cache.bin`, encrypted for your user (DPAPI on Windows) |
| Previews and approvals | `~/.attache/.approvals/`: a `.preview` file per draft and a file per approved send, both removed after the send; previews of drafts never sent go after a day |
| Gmail and Titan sends | `~/.attache/.mail/sent.jsonl`: time, account, Message-ID and number of recipients |
| Audit log | `~/.attache/logs/mail.jsonl` |

The audit log gets one line for every command, read or write:
`{"ts", "command", "account", "ids", "ok"}`, plus `error` when a command fails and `approved`
on a send you approved. It never holds a subject or a body. A preview holds the draft's text
until it is sent, in a folder only you and Attaché can read.

To remove a stored password, delete the `attache-mail` entry in Windows Credential Manager
(or Keychain Access on a Mac). To sign Graph out, delete `graph-cache.bin`.

## Limits

- Calendar and tasks are Outlook only, for now.
- Only classic Outlook has COM. If you switch to the new Outlook app, `doctor` says so: set
  the account to `backend = "graph"` and it carries on.
- Outlook may show security prompts when antivirus isn't reported healthy, or when a policy
  forces them. On a home PC with Defender up to date it is normally silent.
- Outlook updates occasionally break COM for a while; `doctor` is the first thing to run.
- Replies hold only the text the assistant wrote; the original message isn't quoted.
- Never built: mail rules, automatic forwarding, settings changes and permanent deletes.

## Troubleshooting

| `doctor` says | Do this |
|---|---|
| no password stored for gmail | `attache skill mail login gmail` |
| imap.gmail.com:993 refused the password | Make a new Gmail app password, then `login` again |
| imap.titan.email:993 refused the password | Switch on **Enable Titan on Other Apps**; with 2FA use an Application Password; `login` again |
| can't reach the server | Check `imap` and `smtp` in `accounts.toml`, and your connection |
| a Graph sign-in error | `attache skill mail login outlook` again; a work tenant may need an admin's approval |
| No module named 'win32com' | COM works only on Windows: use `backend = "graph"` elsewhere |
| no accounts yet | `attache skill mail setup`, then edit the file it prints |

`login` asks for a password, so it only runs in a terminal: the assistant can't run it, and
you never give a password in a chat. If someone pastes one into a chat, change it.
