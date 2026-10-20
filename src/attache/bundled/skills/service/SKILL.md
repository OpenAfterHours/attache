---
name: service
description: Keep Attaché running in the background from login - install, remove or check the
  login service (Task Scheduler on Windows, launchd on macOS, a systemd user unit on Linux).
  Use when the owner asks to start Attaché automatically, run it as a service or at startup,
  stop it starting at login, or whether it is running. Owner only.
---

# Service

The owner runs it in a terminal:

```
attache skill service install     # or uninstall, or status; add --dry-run to see the steps
```

Or they ask you, and you run the same script:
`script {skill: "service", script: "service.py", args: ["install", "--dry-run"]}`.
Always run `--dry-run` first, show the owner the steps, and run it for real only after their
yes. `status` is safe to run any time.

## What install does

| System | What | Check it |
|---|---|---|
| Windows | A Task Scheduler task, `Attache`, started at the owner's own logon, only while they are logged on and without elevated rights, which Outlook automation needs. It also runs on battery, has no time limit, and is started now too. A standard user can install it: no administrator needed. | `schtasks /Query /TN Attache` |
| macOS | A LaunchAgent, `~/Library/LaunchAgents/ai.attache.plist`: started now, at login, and again if it stops. Log: `<home>/logs/service.log`. | `launchctl print gui/$(id -u)/ai.attache` |
| Linux | A systemd user unit, `~/.config/systemd/user/attache.service`, enabled and started, and restarted if it fails. | `journalctl --user -u attache` |

The service runs `attache run --home <home>`, which is the same as typing it: web page,
scheduler and chat apps, without the terminal chat. `uninstall` stops it and removes the
task, agent or unit.

## Before installing

- The service needs the installed copy: `uv tool install attache`. The copy `uvx attache`
  runs lives in uv's cache, which can be cleared at any time, so the script refuses it.
- Stop any `attache run` started by hand first: only one Attaché can use the web port.
- Install again after moving the home folder or changing PATH. After
  `uv tool upgrade attache` there is nothing to do.

## Good to know

- **Windows.** Attaché runs in a console window, opened at logon. Minimising it keeps
  Attaché running; closing it stops Attaché until the next logon (or `schtasks /Run /TN
  Attache`).
- **Environment.** The service sees the PATH from the moment of install (macOS and Linux) or
  the owner's saved environment variables (Windows), but not variables set only in a shell
  profile, such as `ANTHROPIC_API_KEY`. On Windows save them as user environment variables;
  on Linux add `Environment=` lines with `systemctl --user edit attache`; on macOS use
  `launchctl setenv`. Then install again (Windows: log off and on).
- **Linux.** A user unit stops when the owner logs out. `loginctl enable-linger` keeps it
  running; it is the owner's choice, never yours.
