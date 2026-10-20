"""Ask one person a question in a chat and wait for their answer. Owner: approvals.

Budget: at most 40 code lines. Slack shows the options as buttons, Signal as a reaction
prompt (the sidecar turns 👍/👎 into an answer event), and every channel accepts a typed
reply naming an option or its number. Only the person asked can answer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from attache.channels import ChannelManager


class Approvals:
    def __init__(self, channels: ChannelManager) -> None:
        raise NotImplementedError

    async def ask(
        self, chat_key: str, person_id: str, text: str, options: list[str], timeout: float = 600
    ) -> str | None:
        """Send the question via channels.ask() and wait for person_id's choice (None on
        timeout). person_id is the config person id ("owner", "sam"), not a platform id; the
        app's on_answer wiring maps a sidecar's platform sender id to it with config.person()."""
        raise NotImplementedError

    def answer(self, ask_id: str, person_id: str, choice: str) -> bool:
        """Resolve a pending ask when person_id is the person asked and choice is an option."""
        raise NotImplementedError

    def match(self, chat_key: str, person_id: str, text: str) -> bool:
        """Typed-reply fallback: when an ask in chat_key waits for person_id and text names one
        of its options (case-insensitive) or its 1-based number, resolve it and return True.
        The router then treats the message as an answer, not a request."""
        raise NotImplementedError
