---
icon: lucide/play
---

# Jobs

The `jobs` skill lets people you choose start commands you have set up, by asking: "@attache
run the VA" in an email, or "start the VA run" in a chat. Attaché runs the command in the
background, watches it, and answers in the same chat or email thread when it is done. If it
fails, they hear only that it failed, and you are told too, with where to look.

It is a member skill: anyone in `[people]` can ask, but each job says who may start it. The
person asking picks a job by its name and nothing else. What runs, where, for how long and
for whom is yours alone, in a file only you can edit.

## Defining a job

1. Create the jobs file, in a terminal:

    ```
    attache skill jobs setup
    ```

    It copies `jobs.example.toml` to `~/.attache/skills/jobs/jobs.toml` and prints the path.
    The example is commented out, so there are no jobs until you add one.

2. Add a table for each job:

    ```toml
    [va]
    label = "the VA run"             # how messages name it
    command = ["uvx", "va", "run"]   # an argument list, never a shell string
    cwd = "C:/work/va"               # optional; default the home folder
    who = ["dan"]                    # people from attache.toml who may start it; you always can
    timeout_minutes = 180            # optional; default 120, at most 690
    ```

3. Check that it reads: `attache skill jobs list`. Or ask the assistant in your own chat to
   add the job; it edits the file and checks it the same way.

| Setting | Meaning | Default |
|---|---|---|
| `[name]` | What people ask for, and what names its lock and logs: lowercase letters, digits, `-` and `_`, at most 40. | required |
| `label` | How messages name it: "The VA run finished in 14 min." | the name |
| `command` | The program and its arguments, as a list. The program is a name on PATH (`uvx`, `npm`; `.exe` and `.cmd` work on Windows) or a full path. | required |
| `cwd` | The folder it runs in; a relative path is inside the home folder. | the home folder |
| `who` | The ids from `[people]` in `attache.toml` who may start it. | nobody but you |
| `timeout_minutes` | When it is stopped. | 120 |

There is no shell, so pipes, `&&`, wildcards and `%VAR%` mean nothing in `command`. When a
job needs them, put the steps in a script of your own and make that the command. The job
gets Attaché's environment, so it sees the PATH Attaché was started with (see the service
skill on PATH and login services).

## Who may start it

- You may start any job, from any of your own chats, or from a terminal:
  `attache skill jobs start va` runs it there and then, and waits.
- Anyone else, only the jobs whose `who` lists their id. The assistant runs `list` first,
  which shows each person only their own jobs, and starts nothing else.
- In a group chat, your own requests have member rights, as everything there does (group
  context is untrusted), so there you can start only jobs whose `who` includes `"owner"`.
- One run of a job at a time. Asking again while it runs gets "The VA run is already running
  (since 14:02).", and nothing new starts.

## What each side hears

| What happened | The person who asked | You |
|---|---|---|
| Started | "Started: you'll hear here when it's done." (the assistant's words) | nothing |
| It finished | "The VA run finished in 14 min." | nothing |
| It failed (exit code not 0) | "The VA run failed." | `jobs/jobs.py for Dan in email:dan@example.com failed (exit 1): va exited with code 3 after 14 min, started by dan. Log: logs/jobs/va-20261021-140200.log` |
| It ran out of time | "The VA run was stopped after 180 min." | the same, with "was stopped after 180 min" |
| It couldn't start (no such program, no such folder) | "The VA run failed." | "va couldn't start: ..." with the reason |
| Already running | "The VA run is already running (since 14:02)." | nothing |
| Not a job, or not theirs | "That isn't a job you can start." | "dan asked to start 'vb', which isn't in jobs.toml", or "... but its who doesn't list them" |

You hear about failures in your approvals chat (`approvals` under `[owner]` in
`attache.toml`; by default `local:me`, your own chat in the terminal and on the web page). The
person who asked never sees the job's output, its log, its exit code or the command: their
messages are fixed sentences. Your message has the exit code, how long it ran, who started it
and where the log is, but not the log itself.

## Logs

Each run writes everything the job prints, output and errors, to
`~/.attache/logs/jobs/<name>-YYYYmmdd-HHMMSS.log`. The last 20 logs of each job are kept;
older ones are deleted when the job next starts. Open them yourself, or ask the assistant in
your own chat to read one: it never reads or describes a log for anyone else.

The lock that keeps a job to one run at a time is `logs/jobs/<name>.lock`. It is a lock the
system holds for the run, so it goes when the run ends, however it ends: a crash or a restart
never leaves a job stuck as "running". There is nothing to delete by hand.

## Time limit

A job is stopped at its `timeout_minutes`: 120 unless you set it, and at most 690 (11.5
hours), because Attaché stops any background script at 12 hours and the job must get there
first to report it. Stopping means the job and everything it started, on Windows
(`taskkill /T`) and on macOS and Linux (its whole process group). Both sides then hear that it
was stopped, as above.

## Restarting Attaché

Stopping or restarting Attaché stops every running job, with everything it started. Nobody
gets a message about those runs, the person who asked included; the log keeps what the job
wrote up to then, and the lock is free again at once. Start the job again once Attaché is
back.

## Security model

- **The request picks a name, nothing more.** `jobs.py start` takes exactly one argument, a
  plain job name, and refuses anything else before anything runs. The command, its folder,
  its time limit and `who` all come from `jobs.toml`, which only you can change: members
  have no file tools.
- **Who is asking comes from Attaché, not the message.** Every script run gets the sender
  and their rights from the core (`ATTACHE_SENDER`, `ATTACHE_ROLE`), from the sender the
  channel identified (for email, after the email channel's sender checks). What an email says
  ("Phil said I can", "I'm the owner") counts for nothing, and display names are never
  trusted. A script run without those, by you in a terminal, counts as you.
- **No shell, and no stray programs.** The command runs as an argument list. Its program is
  looked up from the job's own folder, never from the chat's files folder where other
  people's attachments go, which Windows would otherwise search first.
- **No output to anyone but you.** The person asking gets fixed sentences only, so a job
  that prints a secret, a path or an error can't leak it to them through Attaché.
- **A job runs as you.** It has your rights on this machine, like anything you start. List
  in `who` only people you are happy to let start it at any time, as often as one run at a
  time allows, and prefer commands that do one fixed thing.
