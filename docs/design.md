---
icon: lucide/map
---

# Design and roadmap

The design of Attaché, with the research behind it, is written up in one long proposal: the
ten decisions it rests on, a comparison with nanoclaw, OpenClaw and nanobot, the
architecture, skills, email, model backends, artifacts, group chats, the code budget, the
roadmap, and the risks.

[Read the design proposal :lucide-arrow-right:](proposal.html){ .md-button .md-button--primary }

The proposal is a standalone page, with its own look.

## In short

- **One small package.** Python 3.11 or later, installed with uv, running as one process.
  All state lives in `~/.attache`, or wherever `ATTACHE_HOME` points.
- **Every optional capability is a skill.** Channels, email, file formats and integrations
  are skills. The core never learns about Gmail or Slack.
- **Skills are living documents.** The assistant edits a skill when you change how you work,
  and every edit is a git commit you can read and undo.
- **Model backends are config rows.** Four small adapters (`anthropic`, `openai-chat`,
  `openai-responses` and `claude-cli`), each endpoint one block of TOML. No LiteLLM and no
  agent framework.
- **Size is a test.** CI fails if the core passes 2,240 lines of code. Blank lines, comments
  and docstrings don't count, so the budget never rewards dense code.

## Roadmap

| Phase | What it adds | Done when |
|---|---|---|
| **1. Local core** (this release) | Config, the store, terminal and web chat, the four adapters, tools, the skills folder with git, the scheduler, the `todos` skill and the Home page. | `uvx attache` gives a working assistant on localhost with to-dos and reminders. |
| **2. Messaging** | The sidecar protocol with approval requests, the Slack and Signal skills, group-chat roles, file sending, PNG and PDF snapshots of pages. | You approve actions from a Slack DM, and a family Signal group can ask for answers, files and a shared checklist. |
| **3. Email and files** | The mail skill for Outlook, Gmail and Titan, with approved sends; Word, Excel and PowerPoint skills; sharing pages through Tailscale Funnel; starting at login; email requests from people you list, for free slots or a job you defined. | Each morning the assistant files new mail in every account, drafts the replies you need, and sends the ones you approve. |
| **4. Cloud** | A Dockerfile, an owner login for the web page, a guide for a small cloud machine, an MCP client for the API backends, a Codex adapter. | The same package runs on a small cloud machine, with your folder synced by git. |
