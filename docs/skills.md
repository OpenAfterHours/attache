---
icon: lucide/book-open
---

# Skills

Attaché keeps its know-how in a folder of skills. A skill teaches it one thing: a procedure,
your preferences, a set of templates, or a script it can run. New abilities arrive as new
skills, not as changes to the core, and the assistant rewrites its skills as your habits
change.

## What a skill is

A skill is a folder under `~/.attache/skills/` with a `SKILL.md` file in the
[Agent Skills](https://agentskills.io/specification) format, the same format Claude Code and
other agents read:

```text
skills/<name>/
  SKILL.md          required: frontmatter, then instructions
  scripts/*.py      optional: scripts the assistant runs with `uv run`
  templates/...     optional: files the instructions point to
  reference.md      optional: long detail, read only when needed
```

`SKILL.md` opens with YAML frontmatter:

```markdown
---
name: weekly-review
description: Run Sam's Friday weekly review - open to-dos, waiting items and next week's
  reminders. Use when Sam asks for a weekly review, or on Friday afternoon.
---

# Weekly review

1. List the open to-dos, oldest first, and the ones waiting on someone else.
2. ...
```

Only each skill's `name` and `description` go into your prompt, with the path to its
`SKILL.md`. The assistant opens the full file when a task needs it, so forty skills cost less
than one long system prompt.

## The bundled skills

`attache init` copies every bundled skill into your home; `attache skills list` shows them.
They are your copies: change them as you like. These four are the core ones:

| Skill | What it does | Members |
|---|---|---|
| `todos` | Keeps to-do lists in the `todos` collection: add, find, tick off, move and share items, and show a list as a board. | Yes |
| `reminders` | Sets, lists and cancels reminders and recurring routines with the `schedule` tool. | Yes |
| `artifact-kit` | Builds [pages](pages.md) with shared design tokens (`tokens.css`) and two templates, `board.html` and `home.html`. | No |
| `skill-writer` | Teaches the assistant to create and edit skills: folder layout, frontmatter, and scripts. | No |

The *Members* column shows which skills are shared with members of group chats (see
[attache-roles](#sharing-a-skill-with-group-chats)).

List the skills in your home with:

```console
$ attache skills list
artifact-kit: Build artifacts - interactive HTML pages over live data, such as boards, ...
reminders: Set, list and cancel reminders and recurring routines with the `schedule` tool. ...
skill-writer: Create or edit skills under skills/ - folder layout, SKILL.md frontmatter, ...
todos: Keep to-do lists in the `todos` collection - add, find, tick off, move and share ...
```

`attache init` copies a bundled skill only when your home has no folder by that name, and
never overwrites a file. To go back to the bundled version of a skill, move its folder out of
`skills/` and run `attache init` again.

## How the assistant edits skills

Tell Attaché how you like something done, and it can write that into a skill:

> **You:** From now on, things to buy go on the Shopping list
>
> **Attaché:** Added a rule to your todos skill: things to buy go on the Shopping list.

The bundled `skill-writer` skill tells it how: rules go under a `## Learned rules` heading,
one dated line each, and an existing rule is updated rather than contradicted. Ask for a new
ability ("learn how I do my weekly review") and it drafts a new skill the same way.

Only your own turns can change a skill. Members and guests have no tool that writes to
`skills/`; when one of them asks for a change, the assistant tells them you need to approve
it.

## History and undo

Your home folder is a git repository. After each of your turns, Attaché commits `skills/`,
`memory/`, `artifacts/` and `attache.toml`, with the chat and the first 60 characters of your
request as the message. Nothing is committed when nothing changed.

`attache skills log` shows the latest 20 commits, newest first. Give a skill name to see only
the commits that touched that skill:

```console
$ attache skills log todos
3e1b7c0 local:me: From now on, things to buy go on the Shopping list
67ca413 attache init
```

`attache skills undo <name>` puts a skill back as it was before its latest commit, and commits
that as `undo: <name>`:

```console
$ attache skills undo todos
Undid the last change to todos (9a2f4d1).
```

- Changes not yet committed are committed first, as `before undo: <name>`, so undo never
  loses work.
- Undo reverts the latest commit that touched the skill, so running it twice puts the change
  back.
- It never deletes a skill. If the skill has no earlier version (it was added by its latest
  commit, such as `attache init`), it prints `Nothing to undo.` and exits with code 1.

You can also say "undo that" in the chat. For anything else, `~/.attache` is an ordinary git
repository: `git log -p skills/todos` shows every change in full.

## Writing a skill

You can write a skill by hand, or ask Attaché to. Either way:

1. Make a folder under `skills/`. Its name is the skill's name: lowercase letters, digits and
   hyphens, starting with a letter or digit, at most 64 characters.
2. Add a `SKILL.md`. Its `name` must equal the folder name, and it needs a `description`.
   Attaché skips a skill without both, and prints a warning.
3. Write the `description` for the assistant: it is all the assistant sees until it opens the
   file, so say what the skill does **and when to use it**, in the words you actually use.
   One to three sentences.
4. Write the body as instructions to the assistant: imperative and concrete, with one good
   example rather than many rules. Keep it under 500 lines, and move long reference material
   into other files in the folder.

New skills are picked up on the next turn; there is nothing to restart. Never put passwords
or API keys in a skill. A script reads them from an environment variable, and the skill says
which one.

### Scripts

A skill can ship Python scripts in `scripts/`. Each one declares its own dependencies in a
[PEP 723](https://peps.python.org/pep-0723/) header, so
[`uv run`](https://docs.astral.sh/uv/guides/scripts/) installs them on first use, outside
Attaché:

```python
# /// script
# requires-python = ">=3.11"
# dependencies = ["python-docx>=1.1"]
# ///
"""Make a Word document from a Markdown file: make.py <input.md> <output.docx>"""
```

The assistant runs a script with its `script` tool, which takes the skill name, the script
file name and a list of arguments:

```json
{"skill": "docx", "script": "make.py", "args": ["notes.md", "notes.docx"]}
```

The script runs with `uv run`, in the chat's own files folder (`~/.attache/files/<chat>/`),
with a time limit of 10 minutes (12 hours in the
[background](#long-jobs-in-the-background)). The assistant gets back the exit code and the
output, and can send the files it made to the chat.

Use `argparse`, print a short result, and exit with a non-zero code and a clear message when
something fails.

#### Who is asking

Attaché sets these environment variables on every run, over any it would otherwise inherit,
so nobody in a chat can choose their values:

| Variable | Value |
|---|---|
| `ATTACHE_HOME` | the home folder |
| `ATTACHE_SENDER` | who asked: `owner`, or their key under `[people]`, such as `dan` |
| `ATTACHE_ROLE` | the rights the request has: `owner` or `member` (you, in a group chat, are `member`) |
| `ATTACHE_CHAT` | the chat it came from, such as `local:me` or `email:dan@example.com` |
| `ATTACHE_BACKGROUND` | `1` in a [background](#long-jobs-in-the-background) run, empty otherwise |

A script that does something not everyone may ask for checks `ATTACHE_ROLE`, and
`ATTACHE_SENDER` against its own list of people. When `ATTACHE_ROLE` is missing, you ran the
script yourself (`attache skill NAME ...` in your terminal), so it may treat the caller as
you. Then `ATTACHE_HOME` may be missing too, so find the home folder from the script's own
path as well: `Path(__file__).resolve().parents[3]`. A script meant only for the background
can refuse other people's foreground runs: `ATTACHE_BACKGROUND` is never inherited.

#### Long jobs in the background

For a job that takes longer than a reply should wait, have the skill tell the assistant to
add `"background": true`:

```json
{"skill": "jobs", "script": "jobs.py", "args": ["start", "va"], "background": true}
```

The tool returns at once, and the assistant tells the person the job has started. The script
runs as it would otherwise, in the same folder and with the same variables, but for up to 12
hours. At most four run at once: you may use all four, everyone else three between them,
one each at a time. Stopping Attaché stops them. When the script ends,
Attaché posts one message in the chat it started in. Choose what it says by printing a JSON
object as the script's last line:

```json
{"text": "The VA run failed.", "owner": "va exited with code 3 after 14 min. Log: logs/jobs/va.log"}
```

| How it ended | The chat gets | You get, in your approvals chat |
|---|---|---|
| exit code 0 | `text`, or `Done.` | nothing |
| another exit code, 12 hours up, or an error | `text`, or `Sorry, that didn't finish. I've let <your name> know.` | `<skill>/<script> for <person> in <chat> failed (exit N)`, or `(timed out)` or `(error: <its type>)`, then `: "<owner>"`, quoted as a JSON string, when the script gave one |

The chat never sees the script's output, only `text`, so put nothing in `text` that the person
who asked shouldn't read. `owner` is for you: what failed and where the log is, never the
log itself. When the job ran in your approvals chat, you get only the message for you.

## Sharing a skill with group chats

By default a skill is yours alone. To let members of group chats use one, add `attache-roles`
to its metadata:

```yaml
---
name: shopping
description: Keep the family shopping list. Use when someone mentions something to buy.
metadata:
  attache-roles: "member"
---
```

`attache-roles` is a list of roles, or a string of role names separated by spaces or commas.
Only `member` has an effect: you always have every skill, and guests have none.

A member skill works differently from yours:

- Members have no tool that opens files, so their turns get the **whole** `SKILL.md` in the
  prompt, not just the description.
- Members may run its scripts with the `script` tool. Scripts of other skills are refused in
  their chats.

So a member skill must hold nothing private: no names of other chats, addresses, accounts, or
rules about your own life. Leave the metadata out of anything private, such as mail, files or
money. Its scripts run on your machine, as you, with arguments shaped by other people's
messages, so share only scripts that are safe with any input.

!!! info "Reserved: `attache-channel`"

    `attache-channel: "<command>"` will mark a skill that runs a chat app as a sidecar
    process. Channels arrive in phase 2, and phase 1 doesn't read this key yet.
