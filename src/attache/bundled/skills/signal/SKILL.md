---
name: signal
description: Signal channel - connects Attaché to Signal through signal-cli, on a separate number
  of its own. Use when the owner wants to set up, check or fix Signal, add the bot to a Signal
  group, or find someone's Signal id for attache.toml.
metadata:
  attache-channel: "channel.py"
---

# Signal

`channel.py` runs [signal-cli](https://github.com/AsamK/signal-cli) in JSON-RPC mode and
passes messages between it and Attaché. The bot needs a phone number of its own: registering a
number with signal-cli logs that number out of any phone using it.

## Setup

1. **Get a separate number.** A cheap SIM works, or a landline (it verifies by voice call).
2. **Install Java 25** (signal-cli needs JRE 25 or newer).
   Windows: `winget install EclipseAdoptium.Temurin.25.JRE`. macOS: `brew install openjdk`.
   Linux: `sudo apt install openjdk-25-jre-headless`, or Temurin 25 from adoptium.net.
3. **Install signal-cli.** Download `signal-cli-<version>.tar.gz` from
   https://github.com/AsamK/signal-cli/releases/latest and unpack it, for example
   `tar -xzf signal-cli-0.14.9.tar.gz -C C:\Tools` (tar is built into Windows). Add its `bin`
   folder to PATH; on Windows the program is `bin\signal-cli.bat`. On Linux x86-64 the
   `-Linux-native` build needs no Java.
4. **Register the number.** Open https://signalcaptchas.org/registration/generate.html on the
   same computer, solve the captcha, right-click **Open Signal** and copy the link. Then:
   ```
   signal-cli -a +447700900123 register --captcha "signalcaptcha://..."
   signal-cli -a +447700900123 verify 123456
   signal-cli -a +447700900123 updateProfile --given-name Attaché
   ```
   For a landline, the first `register` fails (that is expected); wait about 60 seconds, then
   run `register --voice` and answer the call. Captchas expire fast: get a fresh one if it is
   refused. Registering too often gets rate limited until you wait.
5. **Check it:** `uv run ~/.attache/skills/signal/channel.py doctor +447700900123`. It checks
   Java, the signal-cli version and the account, and prints the bot's own Signal id.

`uv run ~/.attache/skills/signal/channel.py setup` prints these steps and runs doctor. It never
solves the captcha for you. If the number is missing, or signal-cli cannot start the account,
the channel says why in Attaché's output and waits; fix it, then restart Attaché.

## Configuration

In `attache.toml`:

```toml
[channels.signal]
enabled = true
number = "+447700900123"          # the bot's number
# signal_cli = 'C:\Tools\signal-cli-0.14.9\bin\signal-cli.bat'   # if it is not on PATH
# uuid = "..."                    # the bot's id, if doctor cannot find it

[owner.ids]
signal = "a1b2c3d4-..."           # YOUR Signal id (ACI), not the bot's

[people.sam]
name = "Sam"
signal = "e5f6a7b8-..."

[chats."signal:Z3JvdXBpZA=="]     # a group: its id from listGroups, exactly as printed
name = "Family"
members = ["sam"]
```

People are matched by Signal id (ACI), a uuid, never by name or number (Signal hides numbers).
To find yours, message the bot from your phone, stop Attaché, then run:

```
signal-cli -a +447700900123 receive
signal-cli -a +447700900123 listContacts --all-recipients
```

Copy the `ACI:` from the line with your name. Do the same for anyone else, once they have
messaged the bot. The bot's own id is printed by doctor, and by Attaché as `self_id` in its
`channel signal ready` line.

## Groups

Save the bot's number as a contact on your phone and add it to a group like anyone else. If it
shows as invited rather than joined, stop Attaché and accept with
`signal-cli -a +447700900123 updateGroup -g GROUP_ID`; `listGroups` shows the ids. In a group,
mention the bot (type @ and pick it) or reply to one of its messages to get an answer.
Attachments reach Attaché only from direct messages and messages that mention the bot, and only
up to 25 MB each.

## Approvals

Signal has no buttons. A question that needs your approval ends with "React 👍 for Approve,
👎 for Decline, or reply with the option". React to that message, or type the option or its
number. Only the person who was asked can answer.

## Limits and upkeep

- **Keep signal-cli updated.** Releases come about monthly, and releases older than about three
  months may stop working as Signal's servers change. Doctor warns after 90 days. "signal-cli
  version is too old for the Signal-Server" in the log means update now.
- **One signal-cli per account.** Stop Attaché before running signal-cli commands by hand, or
  they wait for the account to be free.
- **The keys live in signal-cli's data folder** (`~/.local/share/signal-cli`, also on
  Windows). Back it up. Registering again makes a new identity, and contacts see a new safety
  number.
- **Attachments** are copied into `<home>/files/inbox/signal/`. signal-cli keeps its own copy
  in its data folder; clear `attachments` there now and then.

## Terms

Signal's terms forbid bulk, automated or unsolicited messaging. The bot only replies to people
who message it and to groups it was added to, plus your own reminders and approvals. Never use
it to message people first or to broadcast.
