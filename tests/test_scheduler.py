from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from cronsim import CronSimError

from attache import scheduler
from attache.scheduler import Scheduler, next_cron
from attache.store import Store

CHAT = "local:me"


@pytest.fixture
def store(home: Path) -> Iterator[Store]:
    s = Store(home / "attache.db")
    yield s
    s.close()


async def until(condition: Callable[[], bool], timeout: float = 2.0) -> None:
    end = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < end, "timed out"
        await asyncio.sleep(0.01)


async def stop(runner: asyncio.Task[None]) -> None:
    runner.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await runner


def test_next_cron_is_local_and_strictly_after() -> None:
    at_730 = datetime(2026, 6, 15, 7, 30).timestamp()  # naive datetime: local time
    assert datetime.fromtimestamp(next_cron("30 7 * * *", at_730)) == datetime(2026, 6, 16, 7, 30)
    before = next_cron("30 7 * * *", at_730 - 0.5)
    assert datetime.fromtimestamp(before) == datetime(2026, 6, 15, 7, 30)
    assert next_cron("* * * * *", at_730) == at_730 + 60
    with pytest.raises(CronSimError):
        next_cron("not a cron", at_730)


def test_daily_cron_keeps_its_hour_across_clock_changes() -> None:
    # 40 days from 20 October cover the autumn change in Europe (25 Oct) and the US (1 Nov),
    # so this fails in any zone with DST if next_cron used a fixed UTC offset.
    t = datetime(2026, 10, 20).timestamp()
    for _ in range(40):
        t = next_cron("0 7 * * *", t)
        assert datetime.fromtimestamp(t).hour == 7


def test_next_cron_always_moves_forward() -> None:
    # Through the hour that repeats when the clocks go back, every step must still be later.
    t = datetime(2026, 10, 24).timestamp()
    for _ in range(4 * 24 * 10):
        later = next_cron("*/15 * * * *", t)
        assert t < later <= t + 75 * 60  # 75 min at most: skipping the repeated hour
        t = later


def test_add(store: Store) -> None:
    sched = Scheduler(store)
    with pytest.raises(ValueError):
        sched.add(CHAT, "neither")
    with pytest.raises(ValueError):
        sched.add(CHAT, "both", at=1.0, cron="* * * * *")
    assert store.task_list() == []
    task = sched.add(CHAT, "tea", at=123.0, by="owner")
    assert (task["due"], task["by"], task["kind"], task["cron"]) == (
        123.0,
        "owner",
        "message",
        None,
    )
    assert sched._wake.is_set()
    now = time.time()
    daily = sched.add(CHAT, "triage", cron="0 7 * * *", kind="turn")
    due = datetime.fromtimestamp(daily["due"])
    assert now < daily["due"] < now + 2 * 86400 and (due.hour, due.minute) == (7, 0)
    assert daily["kind"] == "turn" and daily["cron"] == "0 7 * * *"


def test_run_fires_due_tasks(store: Store, caplog: pytest.LogCaptureFixture) -> None:
    fired: list[str] = []

    async def fire(task: dict[str, Any]) -> None:
        fired.append(task["text"])
        if task["text"] == "broken":
            raise RuntimeError("boom")

    now = time.time()
    once = store.task_add(CHAT, "once", now - 5)
    broken = store.task_add(CHAT, "broken", now - 4)
    daily = store.task_add(CHAT, "daily", now - 86400, cron="0 7 * * *")  # missed while stopped
    future = store.task_add(CHAT, "future", now + 3600)

    async def main() -> None:
        runner = asyncio.create_task(Scheduler(store, fire).run())
        await until(lambda: len(fired) == 3)
        await asyncio.sleep(0.05)  # give the loop a chance to fire anything twice
        await stop(runner)

    asyncio.run(main())
    assert fired == ["daily", "once", "broken"]  # earliest first, once each
    assert "boom" in caplog.text
    pending = {t["id"]: t for t in store.task_list()}
    assert set(pending) == {daily["id"], future["id"]}
    assert once["id"] not in pending and broken["id"] not in pending
    assert pending[daily["id"]]["due"] > now  # moved to the next 07:00, no backfill
    assert pending[future["id"]]["due"] == future["due"]


def test_run_sleeps_until_next_task(store: Store) -> None:
    fired: list[float] = []

    async def fire(task: dict[str, Any]) -> None:
        fired.append(time.time())

    due = time.time() + 0.3
    store.task_add(CHAT, "soon", due)  # straight into the store: no wake()

    async def main() -> None:
        runner = asyncio.create_task(Scheduler(store, fire).run())
        await until(lambda: bool(fired))
        await stop(runner)

    asyncio.run(main())
    assert fired[0] >= due - 0.05


def test_wake_fires_a_new_task_promptly(store: Store) -> None:
    async def main() -> None:
        fired = asyncio.Event()

        async def fire(task: dict[str, Any]) -> None:
            fired.set()

        sched = Scheduler(store, fire)
        runner = asyncio.create_task(sched.run())
        await asyncio.sleep(0.05)  # the loop now waits up to 60 s
        sched.add(CHAT, "now", at=time.time())
        await asyncio.wait_for(fired.wait(), 2)
        await stop(runner)

    asyncio.run(main())
    assert store.task_list() == []


def test_run_survives_store_errors(
    store: Store, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(scheduler, "RETRY_SECONDS", 0.01)
    real_task_due = store.task_due
    failures = [sqlite3.OperationalError("database is locked")] * 2

    def flaky_task_due(now: float) -> list[dict[str, Any]]:
        if failures:
            raise failures.pop()
        return real_task_due(now)

    monkeypatch.setattr(store, "task_due", flaky_task_due)
    store.task_add(CHAT, "still fires", time.time() - 1)
    fired: list[str] = []

    async def fire(task: dict[str, Any]) -> None:
        fired.append(task["text"])

    async def main() -> None:
        runner = asyncio.create_task(Scheduler(store, fire).run())
        await until(lambda: bool(fired))
        await stop(runner)

    asyncio.run(main())
    assert fired == ["still fires"]
    assert caplog.text.count("database is locked") == 2


def test_run_needs_fire(store: Store) -> None:
    with pytest.raises(AssertionError, match="fire"):
        asyncio.run(Scheduler(store).run())
