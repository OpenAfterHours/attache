# Attaché

A personal assistant you install with one command. It runs on your own machine, keeps your
to-dos and reminders, makes interactive pages on demand, and learns how you work through a
folder of skills it can edit itself. The core is about 1,500 lines of Python; everything else
is a skill.

> **Status:** phase 1 of the [roadmap](docs/proposal.html): the local core, terminal and web
> chat. Slack and Signal arrive in phase 2, email (Outlook, Gmail, Titan) in phase 3.

## Try it

```
uvx attache              # first run asks your name, creates ~/.attache, then opens a chat
uv tool install attache  # keep it
attache chat             # terminal chat; the web page is at http://127.0.0.1:8765
```

Attaché talks to models through four adapters, chosen in `~/.attache/attache.toml`:

| `api`              | Use it for                                                        |
|--------------------|-------------------------------------------------------------------|
| `claude-cli`       | Claude Code (`claude -p`) on your own login. Your own chats only. |
| `anthropic`        | The Claude API, or any Anthropic-compatible `base_url`            |
| `openai-chat`      | Ollama, LM Studio, vLLM, llama.cpp, OpenRouter, Groq, Azure       |
| `openai-responses` | OpenAI reasoning models with tools                                |

```toml
[routing]
own_chats   = "claude"      # your Claude login
group_chats = "claude-api"  # an API key, per Anthropic's terms

[endpoints.claude]
api   = "claude-cli"
model = "claude-sonnet-5-5"

[endpoints.claude-api]
api     = "anthropic"
model   = "claude-sonnet-5-5"
key_env = "ANTHROPIC_API_KEY"
```

## How it is put together

- **Skills** live in `~/.attache/skills/<name>/SKILL.md` ([Agent Skills](https://agentskills.io)
  format). Only names and descriptions go into the prompt. The assistant edits skills when you
  change how you work, and `~/.attache` is a git repo, so `attache skills log` and
  `attache skills undo <name>` show and reverse every change.
- **Artifacts** are HTML pages the assistant writes into `~/.attache/artifacts/`. The local web
  server adds a small `attache` bridge so a page can read and write real data with live updates.
- **Roles.** You are the owner. Group members (from phase 2) only reach what is shared with
  their chat, and never files, shell or email.

The full design, with the research behind it, is in [docs/proposal.html](docs/proposal.html).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md): bug fixes and simplifications go in the core, features
go in skills.

## License

MIT
