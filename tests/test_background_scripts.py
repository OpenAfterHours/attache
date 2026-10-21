"""The script environment (ATTACHE_* variables) and background scripts (script, background
true). Scripts are small Python files run by this interpreter in place of `uv run`."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from attache import app, channels, config, tools
from attache.scheduler import Scheduler
from attache.store import Store
from attache.types import Outbound, Person, TurnContext

DM, GROUP = "email:sam@example.com", "slack:CFAM"
SKILL = "---\nname: jobs\ndescription: Test jobs.\nmetadata:\n  attache-roles: member\n---\nBody.\n"
ENV = """import json, os
keys = ["ATTACHE_HOME", "ATTACHE_SENDER", "ATTACHE_ROLE", "ATTACHE_CHAT"]
print(json.dumps({key: os.environ.get(key) for key in keys}))
"""
# job.py CODE [LINE...]: prints a log line, then each LINE, and exits with CODE. job.py sleep:
# starts a child that shares its output, leaves a file named after its pid, and sleeps.
JOB = """import os, subprocess, sys, time
print("log: password=hunter2", flush=True)  # what a requester must never see
if sys.argv[1] == "sleep":
    subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    open(f"sleeping-{os.getpid()}", "w").close()
    time.sleep(60)
print(*sys.argv[2:], sep="\\n")
sys.exit(int(sys.argv[1]))
"""
FAILED = json.dumps({"text": "The VA run failed.", "owner": "va exited with code 3 after 1 min"})
SORRY = "Sorry, that didn't finish. I've let Alex know."


class Recorder:
    """A channel that keeps what is sent to it."""

    def __init__(self, name: str, sent: list[Outbound]) -> None:
        self.name, self.sent = name, sent

    async def start(self, on_message: Any) -> None:
        pass

    async def send(self, msg: Outbound) -> None:
        self.sent.append(msg)

    async def ask(self, chat_id: str, ask_id: str, text: str, options: list[str]) -> bool:
        return False

    async def stop(self) -> None:
        pass


@pytest.fixture
def services(home: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[app.Services]:
    people = '[owner]\nname = "Alex"\n[people.sam]\nname = "Sam"\n'
    (home / "attache.toml").write_text(people, encoding="utf-8")
    scripts = home / "skills" / "jobs" / "scripts"
    scripts.mkdir(parents=True)
    (scripts.parent / "SKILL.md").write_text(SKILL, encoding="utf-8")
    (scripts / "env.py").write_text(ENV, encoding="utf-8")
    (scripts / "job.py").write_text(JOB, encoding="utf-8")
    real_exec = tools._exec

    async def without_uv(cmd: list[str], cwd: Path, timeout: float, env: Any = None) -> str:
        assert cmd[:2] == ["uv", "run"]
        return await real_exec([sys.executable, *cmd[2:]], cwd, timeout, env)

    monkeypatch.setattr(tools.shutil, "which", lambda name: "uv")
    monkeypatch.setattr(tools, "_exec", without_uv)
    hub, log = channels.ChannelManager(), []
    for name in ("local", "email", "slack"):
        hub.add(Recorder(name, log))  # one log for every chat
    db = Store(home / "attache.db")
    yield app.Services(config.load(home), db, Scheduler(db), hub, {}, "http://test")
    db.close()
    assert not tools.RUNNING  # every run's slot is given back, however it ended


def ctx(services: app.Services, who: str = "sam", chat: str = DM) -> TurnContext:
    person = Person("owner", "Alex", "owner") if who == "owner" else Person("sam", "Sam", "member")
    return TurnContext(chat, person, chat.startswith("slack:"), services)


def sent(services: app.Services) -> list[tuple[str, str]]:
    return [(m.chat_key, m.text) for m in services.channels.channels["local"][0].sent]


def kept(services: app.Services, chat: str) -> list[tuple[str, str]]:
    return [(m["role"], m["text"]) for m in services.store.history(chat)]


async def script(context: TurnContext, name: str, *args: str, background: bool = False) -> str:
    tool = next(t for t in tools.build(context) if t.name == "script")
    call = {"skill": "jobs", "script": name, "args": list(args), "background": background}
    return await tool.fn(call, context)


async def to_the_end(context: TurnContext, *args: str) -> None:
    """Start job.py in the background and wait for it to end."""
    started = json.loads(await script(context, "job.py", *args, background=True))
    assert started == {"started": True, "note": tools.STARTED}
    await asyncio.gather(*tools.RUNNING)


def test_a_script_learns_who_asks_and_where(
    services: app.Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in ("ATTACHE_ROLE", "ATTACHE_SENDER"):  # inherited values never get through
        monkeypatch.setenv(key, "owner")
    monkeypatch.setenv("ATTACHE_CHAT", "local:me")

    def env(context: TurnContext) -> dict[str, str]:
        return json.loads(json.loads(asyncio.run(script(context, "env.py")))["output"])

    home = str(services.home.resolve())
    owner = {"ATTACHE_HOME": home, "ATTACHE_SENDER": "owner", "ATTACHE_ROLE": "owner"}
    assert env(ctx(services, "owner", "local:me")) == {**owner, "ATTACHE_CHAT": "local:me"}
    member = {"ATTACHE_HOME": home, "ATTACHE_SENDER": "sam", "ATTACHE_ROLE": "member"}
    assert env(ctx(services)) == {**member, "ATTACHE_CHAT": DM}
    assert env(ctx(services, chat=GROUP)) == {**member, "ATTACHE_CHAT": GROUP}
    in_a_group = env(ctx(services, "owner", GROUP))  # the owner in a group: member rights
    assert (in_a_group["ATTACHE_SENDER"], in_a_group["ATTACHE_ROLE"]) == ("owner", "member")
    schema = next(t for t in tools.build(ctx(services)) if t.name == "script").schema
    assert schema["properties"]["background"] == {"type": "boolean"}
    assert schema["required"] == ["skill", "script"]


def test_a_background_script_posts_its_text_when_it_ends(services: app.Services) -> None:
    async def main() -> None:
        member = ctx(services)
        started = await script(member, "job.py", "0", background=True)
        (job,) = tools.RUNNING
        assert json.loads(started)["started"] and not job.done()  # returned before it ran
        assert sent(services) == []
        await job
        await to_the_end(member, "0", '{"text": "The VA run finished."}', "", "")
        await to_the_end(ctx(services, "owner", "local:me"), "0", '{"owner": "unused"}')

    asyncio.run(main())
    assert sent(services) == [
        (DM, "Done."),  # no JSON on its last line
        (DM, "The VA run finished."),  # the last non-empty line's text
        ("local:me", "Done."),  # a success tells nobody else
    ]
    assert kept(services, DM) == [("assistant", "Done."), ("assistant", "The VA run finished.")]
    assert kept(services, "local:me") == [("assistant", "Done.")]


def test_a_failure_shows_only_its_text_and_tells_the_owner(services: app.Services) -> None:
    async def main() -> None:
        member = ctx(services)
        await to_the_end(member, "3", FAILED)
        await to_the_end(member, "1")  # a crash: no JSON, so the fixed sentence
        await to_the_end(member, "2", '["not", "an object"]', '{"text": 5}')

    asyncio.run(main())
    head = f"jobs/job.py for Sam in {DM} failed"
    assert sent(services) == [
        (DM, "The VA run failed."),
        ("local:me", f"{head} (exit 3): va exited with code 3 after 1 min"),
        (DM, SORRY),
        ("local:me", f"{head} (exit 1)"),
        (DM, SORRY),  # a text that is not a string counts as none
        ("local:me", f"{head} (exit 2)"),
    ]
    assert not any("password" in text for _, text in sent(services))  # never the output
    assert [text for _, text in kept(services, DM)] == ["The VA run failed.", SORRY, SORRY]
    assert len(kept(services, "local:me")) == 3


def test_a_background_script_that_times_out(
    services: app.Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tools, "BACKGROUND_SECONDS", 1)
    asyncio.run(to_the_end(ctx(services, chat=GROUP), "sleep"))
    note = f"jobs/job.py for Sam in {GROUP} failed (timed out)"
    assert sent(services) == [(GROUP, SORRY), ("local:me", note)]


def test_in_the_approvals_chat_a_failure_is_one_message(services: app.Services) -> None:
    asyncio.run(to_the_end(ctx(services, "owner", "local:me"), "3", FAILED))
    note = "jobs/job.py for Alex in local:me failed (exit 3): va exited with code 3 after 1 min"
    assert sent(services) == [("local:me", note)]
    assert kept(services, "local:me") == [("assistant", note)]


def test_four_at_once_and_stopping_kills_them(
    services: app.Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    killed, real_kill = [], channels.kill_tree

    async def kill_tree(proc: asyncio.subprocess.Process) -> None:
        running = proc.returncode is None
        await real_kill(proc)
        # The pipe ends only once every process holding it, the child too, has died.
        await asyncio.wait_for(proc.stdout.read(), 10)
        killed.append(running)

    monkeypatch.setattr(channels, "kill_tree", kill_tree)
    folder = tools.chat_files(ctx(services))

    async def main() -> None:
        for _ in range(tools.MAX_BACKGROUND):
            await script(ctx(services), "job.py", "sleep", background=True)
        with pytest.raises(RuntimeError, match="4 scripts are running in the background"):
            await script(ctx(services, "owner", "local:me"), "job.py", "0", background=True)
        for _ in range(400):  # until all four are running
            if len(list(folder.glob("sleeping-*"))) == 4:
                break
            await asyncio.sleep(0.05)
        await services.channels.stop()  # as Attaché does when it stops

    start = time.time()
    asyncio.run(main())
    assert killed == [True] * 4 and time.time() - start < 30  # killed, not left for a minute
    assert sent(services) == []  # a cancelled run posts nothing
