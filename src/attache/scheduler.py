"""Fire due tasks.

Tasks live in the store. `kind="message"` tasks are delivered as text; `kind="turn"` tasks run
as an agent turn (e.g. a morning triage). The router does the firing via the `fire` callback.
Tasks that came due while Attaché was stopped fire once on start. Cron tasks then move to their
next future time, with no backfill. Firing is at most once (see run).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from cronsim import CronSim

from attache.store import Store

Fire = Callable[[dict[str, Any]], Awaitable[None]]
logger = logging.getLogger(__name__)
RETRY_SECONDS = 5.0  # after an unexpected error, such as "database is locked"


class Scheduler:
    def __init__(self, store: Store, fire: Fire | None = None) -> None:
        self.store = store
        self.fire = fire  # the Router sets this to Router.fire when it is created
        self._wake = asyncio.Event()

    def wake(self) -> None:
        """Call after store.task_add, so a task due sooner than the loop's next check fires."""
        self._wake.set()

    async def run(self) -> None:
        """Forever: fire due tasks, then sleep until the next one (60 s at most) or wake().

        At most once: each task is finished (or, for cron, moved on) before it fires, so a store
        error or a crash mid-fire can lose that one reminder but never repeat it. An error from
        the store (e.g. "database is locked") is logged and retried, never fatal.
        """
        assert self.fire is not None, "set Scheduler.fire before run()"  # or tasks would be lost
        while True:
            try:
                for task in self.store.task_due(time.time()):
                    if task["cron"]:  # the next time after now, not after `due`: no backfill
                        self.store.task_reschedule(task["id"], next_cron(task["cron"], time.time()))
                    else:
                        self.store.task_end(task["id"])
                    try:
                        await self.fire(task)
                    except Exception:
                        logger.exception("task %s failed", task["id"])
                due = self.store.task_next()
                delay = 60.0 if due is None else min(60.0, max(0.0, due - time.time()))
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), delay)
                self._wake.clear()
            except Exception:
                logger.exception("scheduler error; retrying in %s s", RETRY_SECONDS)
                await asyncio.sleep(RETRY_SECONDS)


def next_cron(expr: str, after: float) -> float:
    """Next time strictly after `after` (epoch seconds) matching a 5-field cron expression."""
    # Naive local time, so "0 7 * * *" stays at 07:00 across clock changes. When the clocks go
    # back, the repeated hour's times map to their first pass, so skip any that are not later.
    moments = CronSim(expr, datetime.fromtimestamp(after))
    return next(m.timestamp() for m in moments if m.timestamp() > after)
