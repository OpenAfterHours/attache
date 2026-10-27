"""OpenAI adapters, both on openai.AsyncOpenAI with endpoint.base_url.

ChatAdapter speaks Chat Completions, the format most custom endpoints share (Ollama, vLLM,
LM Studio, llama.cpp, OpenRouter, Groq, Azure v1). ResponsesAdapter speaks the Responses API,
which OpenAI's reasoning models need for tool calls.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from attache import credentials
from attache.agent import Step, tool_spec
from attache.types import Endpoint, ToolCall, Turn


async def _client(adapter: ChatAdapter | ResponsesAdapter) -> Any:
    async with adapter._client_lock:
        if adapter.client is None:
            options = await asyncio.to_thread(credentials.client_options, adapter.endpoint)
            options["api_key"] = options["api_key"] or "not-needed"  # unauthenticated servers
            from openai import AsyncOpenAI, DefaultAsyncHttpxClient

            if adapter.endpoint.key_ref:
                options["http_client"] = DefaultAsyncHttpxClient(follow_redirects=False)
            adapter.client = AsyncOpenAI(**options)
    return adapter.client


def _args(raw: Any) -> dict[str, Any]:
    """Parsed tool-call arguments; malformed ones become {"__error__": ...} for run_tool."""
    if isinstance(raw, dict):  # some local servers send an object, not a JSON string
        return raw
    with contextlib.suppress(ValueError):
        if isinstance(args := json.loads(raw or "{}"), dict):
            return args
    return {"__error__": f"arguments were not valid JSON: {(raw or '')[:200]}"}


class ChatAdapter:
    def __init__(self, endpoint: Endpoint, client: Any = None) -> None:
        self.endpoint = endpoint
        self.client = client
        self._client_lock = asyncio.Lock()

    def start(self, turn: Turn) -> list[dict[str, Any]]:
        system = {"role": "system", "content": turn.system}
        return [system, *map(dict, turn.history), {"role": "user", "content": turn.text}]

    async def step(self, state: list[dict[str, Any]], turn: Turn) -> Step:
        kwargs: dict[str, Any] = {"model": self.endpoint.model, "messages": state}
        if turn.tools:
            kwargs["tools"] = [{"type": "function", "function": tool_spec(t)} for t in turn.tools]
        client = await _client(self)
        resp = await client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        tool_calls = msg.tool_calls or []
        message: dict[str, Any] = {"role": "assistant", "content": msg.content}
        if tool_calls:
            message["tool_calls"] = [tc.model_dump(exclude_none=True) for tc in tool_calls]
        state.append(message)
        calls = [
            ToolCall(tc.id, tc.function.name, _args(tc.function.arguments)) for tc in tool_calls
        ]
        u = resp.usage  # some local servers omit it
        usage = {"input_tokens": u.prompt_tokens, "output_tokens": u.completion_tokens} if u else {}
        return Step(msg.content or "", calls, usage)

    def add_results(self, state: list[dict[str, Any]], results: list[tuple[ToolCall, str]]) -> None:
        for call, output in results:
            state.append({"role": "tool", "tool_call_id": call.id, "content": output})


class ResponsesAdapter:
    def __init__(self, endpoint: Endpoint, client: Any = None) -> None:
        self.endpoint = endpoint
        self.client = client
        self._client_lock = asyncio.Lock()

    def start(self, turn: Turn) -> list[dict[str, Any]]:
        return [*map(dict, turn.history), {"role": "user", "content": turn.text}]

    async def step(self, state: list[dict[str, Any]], turn: Turn) -> Step:
        client = await _client(self)
        resp = await client.responses.create(
            model=self.endpoint.model,
            instructions=turn.system,
            input=state,
            # strict mode would reject schemas with optional fields, so turn it off
            tools=[{"type": "function", **tool_spec(t), "strict": False} for t in turn.tools],
            store=False,  # stateless: reasoning comes back encrypted and is replayed from state
            include=["reasoning.encrypted_content"],
        )
        calls = []
        for item in resp.output:
            state.append(item.model_dump(exclude_none=True))
            if item.type == "function_call":
                calls.append(ToolCall(item.call_id, item.name, _args(item.arguments)))
        u = resp.usage
        usage = {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens} if u else {}
        return Step(resp.output_text or "", calls, usage)

    def add_results(self, state: list[dict[str, Any]], results: list[tuple[ToolCall, str]]) -> None:
        for call, output in results:
            state.append({"type": "function_call_output", "call_id": call.id, "output": output})
