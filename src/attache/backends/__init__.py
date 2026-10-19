"""Pick a backend for an endpoint. Owner: models. Budget ~20 lines."""

from __future__ import annotations

from attache.types import Backend, Endpoint


def make(endpoint: Endpoint) -> Backend:
    """anthropic | openai-chat | openai-responses -> tool_loop over that adapter;
    claude-cli -> ClaudeCLI. Unknown api names raise ValueError listing the valid ones.
    SDK clients are created lazily so a missing API key fails at the first turn, with a
    message naming the endpoint and its key_env."""
    raise NotImplementedError
