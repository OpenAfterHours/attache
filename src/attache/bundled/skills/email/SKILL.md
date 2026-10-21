---
name: email
description: Email channel - lets the people listed in attache.toml write to Attaché by email
  (a message that mentions @attache) and get the answer by email, in the same thread. It
  watches one inbox of the mail skill. Use when the owner wants to set up, check or fix the
  email channel, add someone's email address, or asks why an email got no answer.
metadata:
  attache-channel: "channel.py"
---

# Email

`channel.py` is a sidecar: Attaché starts it with `uv run` and swaps JSON lines with it (see
`docs/channels.md`). It checks one mail account's inbox every minute, through the mail skill's
own code, so the account, its sign-in and `doctor` are the mail skill's. Through it Attaché:

- answers an email from someone in `[people]` whose new text, above any quoted history,
  mentions `@attache` (or `@Attaché`; case and accents don't matter);
- replies in the thread of the message it answers, to that sender only, under a line saying
  it was sent by Attaché and that an answer should start with `@attache`;
- never answers mail that was there before the channel's first start.

## Setup

1. Set up the account in the mail skill first: `attache skill mail setup`, edit
   `skills/mail/accounts.toml`, then `attache skill mail login <account>` if it needs one,
   and `attache skill mail doctor` until it says `ok`.
2. Give the account a `user = "you@example.com"` line in `accounts.toml` if it hasn't one
   (Outlook accounts often don't): mail from that address is then never treated as a request.
3. Add the channel and the people to `attache.toml`, then restart `attache run`.

A home made with 0.0.2 or earlier has an older mail skill, which the channel refuses ("older
than this channel"): `docs/mail.md`, "Updating the mail skill", says how to replace it while
keeping `accounts.toml` and every sign-in.

## attache.toml

```toml
[channels.email]
enabled = true
account = "gmail"          # a section of skills/mail/accounts.toml
# every = "60"             # seconds between checks
# mention = "@attache"     # what a message must contain
# signature = "..."        # the line under each reply
# authserv_id = "mx.google.com"   # Gmail: only Google's own results count

[people.dan]
name = "Dan"
email = "dan@example.com"  # the address Dan's mail comes From
```

- Values other than `enabled` are strings, so `every = "60"`, not `every = 60`.
- An email address in `[owner.ids]` is refused, and so is an email chat as `approvals`: a
  From line is easier to fake than a chat login, and owner rights include files, the shell
  and memory. The owner writes to Attaché in their own chats.
- `authserv_id` only for Gmail or another IMAP account whose server names itself in its
  `Authentication-Results`; Microsoft 365 doesn't, so leave it unset there.

## Who gets an answer

A message is passed to Attaché only when all of these hold:

- **Its new part mentions Attaché.** A mention in the subject or in quoted history doesn't
  count, so "thanks" in a thread is not a new request.
- **It isn't Attaché's own**: a new part holding the reply signature is skipped, which stops
  two assistants answering each other.
- **It is from a person**, not the account itself, an auto-reply, a mailing list or bulk mail.
- **The sender is proven.** The receiving server's own `Authentication-Results` header (the
  topmost one) must say `dmarc=pass` for the From address's domain. Gmail and Microsoft 365
  add one to every message. Use the channel only with a provider that does, and only for mail
  delivered straight to it: not Gmail fetching another mailbox, imported mail, or mail through
  a filtering gateway. On Microsoft 365 (Graph, or Outlook classic on an Exchange account),
  mail Exchange takes as internal counts too; that proves the organisation, not the person.
- **Attaché knows the address**: it is someone's `email` in `[people]`. Strangers are ignored.

A person's role in `[people]` applies as in a direct message: members get member tools and
the skills shared with members, never the owner's mail, files or shell; guests are ignored.
What an email says is a request, never an instruction to break those rules.

## Replies

- In the thread of the message the reply is about, or of that person's latest message for a
  reminder or a finished background job.
- Only to the From address, never to its Reply-To, and never Cc or reply-all. With no message
  from an address, nothing is sent: no cold email. If the message was moved since (Outlook
  ids change), the reply goes to that same address as a new email, "Re:" its subject.
- Text only: files the reply mentions are not attached.
- Sent straight away, as the answer to the person's own request. The owner's approval rule
  for sending mail is for the owner's mail, through the mail skill, not for these replies.
- Each send is logged in `logs/mail.jsonl` with ids only.

## Troubleshooting

- Attaché's log says "checking <account> failed": run `attache skill mail doctor` and fix what
  it says (a password, a Microsoft sign-in, Outlook classic not running).
- "Email: ... Fix it": `[channels.email]` has no `account`, `accounts.toml` doesn't have that
  account, or the mail skill is missing or older than the channel (see Setup).
- An email got no answer: only an unproven sender leaves a line in Attaché's log ("skipped
  message ...: sender not authenticated": the domain doesn't pass DMARC, or the mail was
  forwarded). Mail without `@attache` in its new text is skipped silently, as is any reply
  that quotes Attaché's signature above its own text. Check too that the address in
  `[people]` is the From address.
- A reply failed with "isn't to the sender alone": the draft is left in Drafts unsent. Delete
  it there.
