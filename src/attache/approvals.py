"""Ask one person a question in a chat and wait for their answer.

Slack shows the options as buttons, Signal as a reaction prompt (the sidecar turns 👍/👎 into an
answer event), and every channel accepts a typed reply naming an option or its number. Only the
person asked can answer. Pending questions live in memory, keyed by a random ask_id; one that
outlives its timeout, or Attaché, is simply gone.
"""

from __future__ import annotations

import asyncio
import secrets
from typing import NamedTuple

from attache.channels import ChannelManager


class Pending(NamedTuple):
    chat_key: str
    person_id: str
    options: list[str]
    future: asyncio.Future[str]


def choose(options: list[str], text: str) -> str | None:
    """The option `text` names, case-insensitively, or by its 1-based number; else None."""
    typed = text.strip().strip(".!").casefold()
    return next((o for n, o in enumerate(options, 1) if typed in (o.casefold(), str(n))), None)


class Approvals:
    def __init__(self, channels: ChannelManager) -> None:
        self.channels = channels
        self.pending: dict[str, Pending] = {}

    async def ask(
        self, chat_key: str, person_id: str, text: str, options: list[str], timeout: float = 600
    ) -> str | None:
        """Send the question via channels.ask() and wait for person_id's choice (None on
        timeout). person_id is the config person id ("owner", "sam"), not a platform id; the
        app's on_answer wiring maps a sidecar's platform sender id to it with config.person()."""
        ask_id = secrets.token_hex(4)
        self.pending[ask_id] = pending = Pending(chat_key, person_id, options, asyncio.Future())
        try:
            await self.channels.ask(chat_key, ask_id, text, options)
            await asyncio.wait([pending.future], timeout=timeout)  # no answer in time: None
            return pending.future.result() if pending.future.done() else None
        finally:
            del self.pending[ask_id]

    def answer(self, ask_id: str, person_id: str, choice: str) -> bool:
        """Resolve a pending ask when person_id is the person asked and choice is an option."""
        pending = self.pending.get(ask_id)
        mine = pending is not None and pending.person_id == person_id and not pending.future.done()
        option = choose(pending.options, choice) if mine else None
        if option is not None:
            pending.future.set_result(option)
        return option is not None

    def match(self, chat_key: str, person_id: str, text: str) -> bool:
        """Typed-reply fallback: when an ask in chat_key waits for person_id and text names one
        of its options (case-insensitive) or its 1-based number, resolve it and return True.
        The router then treats the message as an answer, not a request."""
        here = [ask_id for ask_id, pending in self.pending.items() if pending.chat_key == chat_key]
        return any(self.answer(ask_id, person_id, text) for ask_id in here)
