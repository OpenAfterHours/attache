"""Fire due tasks. Owner: foundation. Budget ~45 lines.

Tasks live in the store. `kind="message"` tasks are delivered as text; `kind="turn"` tasks run
as an agent turn (e.g. a morning triage). The router does the firing via the `fire` callback.
Tasks that came due while Attaché was stopped fire once on start. Cron tasks then move to their
next future time, with no backfill.
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


class Scheduler:
    def __init__(self, store: Store, fire: Fire | None = None) -> None:
        self.store = store
        self.fire = fire  # set later by app.run once the router exists
        self._wake = asyncio.Event()

    def add(
        self,
        chat_key: str,
        text: str,
        *,
        at: float | None = None,
        cron: str | None = None,
        kind: str = "message",
        by: str = "",
    ) -> dict[str, Any]:
        """Exactly one of `at` (epoch seconds) or `cron` (5 fields, local time). Wakes the loop."""
        if (at is None) == (cron is None):
            raise ValueError("give exactly one of `at` or `cron`")
        due = at if at is not None else next_cron(str(cron), time.time())
        task = self.store.task_add(chat_key, text, due, kind=kind, cron=cron, by=by)
        self.wake()
        return task

    def wake(self) -> None:
        self._wake.set()

    async def run(self) -> None:
        """Forever: fire due tasks, then sleep until the next one (60 s at most) or wake()."""
        while True:
            fire = self.fire  # until it is set, due tasks wait instead of being lost
            for task in self.store.task_due(time.time()) if fire else []:
                try:
                    await fire(task)
                except Exception:
                    logger.exception("task %s failed", task["id"])
                if task["cron"]:  # from now, not from `due`, so a slow turn cannot fire twice
                    self.store.task_reschedule(task["id"], next_cron(task["cron"], time.time()))
                else:
                    self.store.task_finish(task["id"])
            due = self.store.task_next() if fire else None
            delay = 60.0 if due is None else min(60.0, max(0.0, due - time.time()))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), delay)
            self._wake.clear()


def next_cron(expr: str, after: float) -> float:
    """Next time strictly after `after` (epoch seconds) matching a 5-field cron expression."""
    return next(CronSim(expr, datetime.fromtimestamp(after).astimezone())).timestamp()
