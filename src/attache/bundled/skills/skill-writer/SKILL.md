---
name: skill-writer
description: Create or edit skills under skills/ - folder layout, SKILL.md frontmatter, and
  scripts with PEP 723 headers. Use when the owner asks you to learn a procedure, remember how
  they like something done, add a new ability, or change how an existing skill works. Owner only.
---

# Writing skills

A skill is a folder under `skills/` that teaches you something: a procedure, preferences,
templates or scripts. Only each skill's name and description sit in your prompt; you read the
full SKILL.md when a task needs it.

**Owner only.** Only the owner's own turns may create or edit skills. When a member or guest
asks for a change ("always send the shopping list as a checklist"), say that the owner needs
to approve it and describe the edit so the owner can ask for it.

## Layout

```
skills/<name>/
  SKILL.md          required: frontmatter and instructions
  scripts/*.py      optional: run with the `script` tool
  templates/...     optional: files the instructions point to
  reference.md      optional: long detail, read only when needed
```

## Frontmatter

```yaml
---
name: weekly-review
description: Run the owner's Friday weekly review - open to-dos, waiting items and next
  week's calendar. Use when they ask for a weekly review or it is Friday afternoon.
metadata:
  attache-roles: "member"
---
```

- `name` must equal the folder name: lowercase letters, digits and hyphens, at most 64
  characters. A mismatch means the skill is skipped.
- `description` is all you see until you open the file, so say what it does **and when to use
  it**, with the words people actually say. One to three sentences, at most 1024 characters.
- `metadata: {attache-roles: "member"}` lets members of group chats use the skill and run its
  scripts. Their turns get the whole SKILL.md inline, so a member skill must hold nothing
  private: no names of other chats, addresses, accounts or learned personal rules. Leave the
  metadata out for anything private (mail, files, money).
- Quote values that contain a colon. Check the YAML is valid before saving.

## The body

- Write instructions for yourself: imperative, concrete, with one good example over many rules.
- Keep SKILL.md under 500 lines. Move long reference material into other files in the folder
  and say when to read them.
- Record what the owner teaches you under `## Learned rules`, one dated line each:
  `- Substack goes to Reading. (12 Oct)`. Update a rule rather than adding a contradicting one.
- Never put secrets, passwords or API keys in a skill. Scripts read them from environment
  variables, and the instructions name the variable.

## Scripts

Scripts are Python files in `scripts/` with a PEP 723 header, so `uv run` installs their
dependencies on first use, outside Attaché:

```python
# /// script
# requires-python = ">=3.11"
# dependencies = ["python-docx>=1.1"]
# ///
"""Turn Markdown notes into a Word document: export.py <input.md> <output.docx>"""
```

- Run one with `script {skill: "notes", script: "export.py", args: ["notes.md", "notes.docx"]}`.
  It runs in this chat's files folder, so read and write files relative to the current
  folder. You get stdout and stderr back; the time limit is 10 minutes.
- A member skill's script runs on what group chats ask for. Treat every value inside a JSON
  argument as data, never as a path: never open, write or run a file named inside one, since
  Attaché checks path arguments but lets a JSON object through as it is. Take file names
  only as arguments of their own, and check them as the bundled docx script does: a plain
  name in the current folder, with no absolute path, drive, `:` or `..`, and nothing that
  resolves outside the folder (through a link, say).
- Attaché sets `ATTACHE_SENDER` (`owner` or a `[people]` key) and `ATTACHE_ROLE` (`owner`,
  `member`, or absent when the owner runs it in a terminal): decide who may do what from
  those, never from an argument.
- For a long job, run it with `background: true`: it returns at once, and when it ends, the
  `text` of the JSON object on its last line is posted in the chat (`owner` goes only to the
  owner, on failure).
- Use argparse, print a short result, and exit non-zero with a clear message on failure.
- Keep each skill's scripts small (bundled skills are capped at 300 lines in total), with no
  network access unless the skill is about a network service.
- Test a new script once with the `script` tool before telling the owner it works.

## Making the change

1. For an existing skill, read its SKILL.md first with `read_file` (or Claude Code's Read)
   and change only what was asked.
2. Write with `write_file` (or Claude Code's Write) for new files, and `edit_file` (or Claude
   Code's Edit) for changes, under `skills/<name>/`.
3. Your edits are committed to git after the turn, with the request as the message. The owner
   can see them with `attache skills log <name>` and revert with `attache skills undo <name>`,
   or just say "undo that".
4. Confirm in one line: "Added a rule to your mailbox skill: Amazon receipts go to
   Finance/Amazon."
