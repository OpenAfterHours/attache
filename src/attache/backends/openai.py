"""OpenAI adapters. Owner: models. Budget ~130 lines.

ChatAdapter speaks Chat Completions, the format most custom endpoints share (Ollama, vLLM,
LM Studio, llama.cpp, OpenRouter, Groq, Azure v1). Parse tool-call arguments tolerantly: bad
JSON becomes an "error: ..." result rather than a crash.

ResponsesAdapter speaks the Responses API, which OpenAI's reasoning models need for tool calls.
Both use openai.AsyncOpenAI with endpoint.base_url.
"""

from __future__ import annotations

from typing import Any

from attache.agent import Step
from attache.types import Endpoint, ToolCall, Turn


class ChatAdapter:
    def __init__(self, endpoint: Endpoint, client: Any = None) -> None:
        raise NotImplementedError

    def start(self, turn: Turn) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def step(self, state: list[dict[str, Any]], turn: Turn) -> Step:
        raise NotImplementedError

    def add_results(self, state: list[dict[str, Any]], results: list[tuple[ToolCall, str]]) -> None:
        raise NotImplementedError


class ResponsesAdapter:
    def __init__(self, endpoint: Endpoint, client: Any = None) -> None:
        raise NotImplementedError

    def start(self, turn: Turn) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def step(self, state: list[dict[str, Any]], turn: Turn) -> Step:
        raise NotImplementedError

    def add_results(self, state: list[dict[str, Any]], results: list[tuple[ToolCall, str]]) -> None:
        raise NotImplementedError
