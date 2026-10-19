"""Pick a backend for an endpoint. Owner: models. Budget ~20 lines."""

from __future__ import annotations

from dataclasses import dataclass

from attache.agent import Adapter, tool_loop
from attache.backends.anthropic import AnthropicAdapter
from attache.backends.claude_cli import ClaudeCLI
from attache.backends.openai import ChatAdapter, ResponsesAdapter
from attache.types import Backend, Endpoint, Reply, Turn

ADAPTERS = {
    "anthropic": AnthropicAdapter,
    "openai-chat": ChatAdapter,
    "openai-responses": ResponsesAdapter,
}


@dataclass
class APIBackend:
    adapter: Adapter

    async def run(self, turn: Turn) -> Reply:
        return await tool_loop(self.adapter, turn)


def make(endpoint: Endpoint) -> Backend:
    """anthropic | openai-chat | openai-responses -> tool_loop over that adapter;
    claude-cli -> ClaudeCLI. Unknown api names raise ValueError listing the valid ones.
    SDK clients are created lazily so a missing API key fails at the first turn, with a
    message naming the endpoint and its key_env."""
    if endpoint.api == "claude-cli":
        return ClaudeCLI(endpoint)
    if endpoint.api not in ADAPTERS:
        valid = ", ".join([*ADAPTERS, "claude-cli"])
        raise ValueError(
            f"endpoint '{endpoint.name}' has unknown api '{endpoint.api}'; use {valid}"
        )
    return APIBackend(ADAPTERS[endpoint.api](endpoint))
