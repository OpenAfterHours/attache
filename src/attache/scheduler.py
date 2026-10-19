"""Fire due tasks. Owner: foundation. Budget ~45 lines.

Tasks live in the store. `kind="message"` tasks are delivered as text; `kind="turn"` tasks run
as an agent turn (e.g. a morning triage). The router does the firing via the `fire` callback.
Tasks that came due while Attaché was stopped fire once on start. Cron tasks then move to their
next future time, with no backfill.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from attache.store import Store

Fire = Callable[[dict[str, Any]], Awaitable[None]]


class Scheduler:
    def __init__(self, store: Store, fire: Fire | None = None) -> None:
        self.store = store
        self.fire = fire  # set later by app.run once the router exists
        raise NotImplementedError

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
        raise NotImplementedError

    def wake(self) -> None:
        raise NotImplementedError

    async def run(self) -> None:
        """Forever: fire due tasks, then sleep until the next one (60 s at most) or wake()."""
        raise NotImplementedError


def next_cron(expr: str, after: float) -> float:
    """Next time strictly after `after` (epoch seconds) matching a 5-field cron expression."""
    raise NotImplementedError
