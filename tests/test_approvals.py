from __future__ import annotations

import asyncio

import pytest

from attache.approvals import Approvals, choose


class Channels:
    def __init__(self, fail: bool = False) -> None:
        self.asked: list[tuple[str, str, str, list[str]]] = []
        self.fail = fail

    async def ask(self, chat_key: str, ask_id: str, text: str, options: list[str]) -> None:
        if self.fail:
            raise ConnectionError("the sidecar is down")
        self.asked.append((chat_key, ask_id, text, options))


def test_choose() -> None:
    options = ["Approve", "Decline"]
    assert (
        choose(options, "approve")
        == choose(options, " 1 ")
        == choose(options, "APPROVE!")
        == "Approve"
    )
    assert choose(options, "2") == choose(options, "decline.") == "Decline"
    assert choose(options, "3") is choose(options, "maybe") is choose(options, "0") is None


def test_ask_and_answer() -> None:
    channels = Channels()
    approvals = Approvals(channels)

    async def main() -> None:
        task = asyncio.create_task(
            approvals.ask("slack:D1", "owner", "Ship it?", ["Approve", "Decline"])
        )
        await asyncio.sleep(0)
        ((chat_key, ask_id, text, options),) = channels.asked
        assert (chat_key, text, options) == ("slack:D1", "Ship it?", ["Approve", "Decline"])
        assert len(ask_id) == 8 and list(approvals.pending) == [ask_id]
        assert not approvals.answer(ask_id, "sam", "Approve")  # only the person asked
        assert not approvals.answer("unknown", "owner", "Approve")
        assert not approvals.answer(ask_id, "owner", "Maybe")  # not one of the options
        assert approvals.answer(ask_id, "owner", "approve")
        assert await task == "Approve"
        assert approvals.pending == {}
        assert not approvals.answer(ask_id, "owner", "Approve")  # answered already: gone

    asyncio.run(main())


def test_typed_replies_by_name_or_number() -> None:
    approvals = Approvals(Channels())

    async def main() -> None:
        tea = asyncio.create_task(approvals.ask("local:me", "owner", "Tea?", ["Yes", "No"]))
        colour = asyncio.create_task(approvals.ask("slack:C1", "sam", "Which?", ["Red", "Blue"]))
        await asyncio.sleep(0)
        assert not approvals.match("local:me", "sam", "yes")  # not asked of sam
        assert not approvals.match("slack:C1", "sam", "green")  # not an option
        assert not approvals.match("slack:C2", "sam", "red")  # another chat
        assert approvals.match("slack:C1", "sam", " 2 ")
        assert approvals.match("local:me", "owner", "YES.")
        assert await tea == "Yes" and await colour == "Blue"
        assert not approvals.match("local:me", "owner", "yes")  # nothing waits any more

    asyncio.run(main())


def test_timeout_gives_none() -> None:
    approvals = Approvals(Channels())
    choice = asyncio.run(approvals.ask("local:me", "owner", "Hello?", ["Yes"], timeout=0.05))
    assert choice is None and approvals.pending == {}


def test_a_failing_channel_leaves_nothing_pending() -> None:
    approvals = Approvals(Channels(fail=True))
    with pytest.raises(ConnectionError):
        asyncio.run(approvals.ask("slack:D1", "owner", "Ship it?", ["Yes", "No"]))
    assert approvals.pending == {}
