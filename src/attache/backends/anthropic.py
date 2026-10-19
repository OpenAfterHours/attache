"""Anthropic Messages API adapter. Owner: models. Budget ~65 lines.

Works with the Claude API and any Anthropic-compatible base_url. Uses anthropic.AsyncAnthropic.
"""

from __future__ import annotations

from typing import Any

from attache.agent import Step
from attache.types import Endpoint, ToolCall, Turn


class AnthropicAdapter:
    def __init__(self, endpoint: Endpoint, client: Any = None) -> None:
        """client: an AsyncAnthropic-like object; tests pass a fake. None builds one lazily from
        endpoint.base_url and the env var named by endpoint.key_env."""
        raise NotImplementedError

    def start(self, turn: Turn) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def step(self, state: list[dict[str, Any]], turn: Turn) -> Step:
        raise NotImplementedError

    def add_results(self, state: list[dict[str, Any]], results: list[tuple[ToolCall, str]]) -> None:
        raise NotImplementedError
