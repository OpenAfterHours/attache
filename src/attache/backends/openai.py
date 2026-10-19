"""OpenAI adapters. Owner: models. Budget ~130 lines.

ChatAdapter speaks Chat Completions, the format most custom endpoints share (Ollama, vLLM,
LM Studio, llama.cpp, OpenRouter, Groq, Azure v1). Parse tool-call arguments tolerantly: bad
JSON becomes an "error: ..." result rather than a crash.

ResponsesAdapter speaks the Responses API, which OpenAI's reasoning models need for tool calls.
Both use openai.AsyncOpenAI with endpoint.base_url.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any

from attache.agent import Step, api_key, tool_spec
from attache.types import Endpoint, ToolCall, Turn


def _client(adapter: ChatAdapter | ResponsesAdapter) -> Any:
    if adapter.client is None:
        key = api_key(adapter.endpoint) or "not-needed"  # local servers take any key
        from openai import AsyncOpenAI

        adapter.client = AsyncOpenAI(api_key=key, base_url=adapter.endpoint.base_url)
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
        self.endpoint, self.client = endpoint, client

    def start(self, turn: Turn) -> list[dict[str, Any]]:
        return [
            {"role": "system", "content": turn.system},
            *map(dict, turn.history),
            {"role": "user", "content": turn.text},
        ]

    async def step(self, state: list[dict[str, Any]], turn: Turn) -> Step:
        kwargs: dict[str, Any] = {"model": self.endpoint.model, "messages": state}
        if turn.tools:
            kwargs["tools"] = [{"type": "function", "function": tool_spec(t)} for t in turn.tools]
        resp = await _client(self).chat.completions.create(**kwargs)
        msg, u = resp.choices[0].message, resp.usage
        tcs = msg.tool_calls or []
        message: dict[str, Any] = {"role": "assistant", "content": msg.content}
        if tcs:
            message["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in tcs
            ]
        state.append(message)
        usage = {"input_tokens": u.prompt_tokens, "output_tokens": u.completion_tokens} if u else {}
        calls = [ToolCall(tc.id, tc.function.name, _args(tc.function.arguments)) for tc in tcs]
        return Step(msg.content or "", calls, usage)

    def add_results(self, state: list[dict[str, Any]], results: list[tuple[ToolCall, str]]) -> None:
        state.extend({"role": "tool", "tool_call_id": c.id, "content": out} for c, out in results)


class ResponsesAdapter:
    def __init__(self, endpoint: Endpoint, client: Any = None) -> None:
        self.endpoint, self.client = endpoint, client

    def start(self, turn: Turn) -> list[dict[str, Any]]:
        return [*map(dict, turn.history), {"role": "user", "content": turn.text}]

    async def step(self, state: list[dict[str, Any]], turn: Turn) -> Step:
        resp = await _client(self).responses.create(
            model=self.endpoint.model,
            instructions=turn.system,
            input=state,
            # strict mode would reject schemas with optional fields, so turn it off
            tools=[{"type": "function", **tool_spec(t), "strict": False} for t in turn.tools],
            store=False,  # stateless: reasoning comes back encrypted and is replayed from state
            include=["reasoning.encrypted_content"],
        )
        calls, u = [], resp.usage
        for item in resp.output:
            data = item.model_dump(exclude_none=True) if hasattr(item, "model_dump") else dict(item)
            state.append(data)
            if data.get("type") == "function_call":
                calls.append(ToolCall(data["call_id"], data["name"], _args(data.get("arguments"))))
        usage = {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens} if u else {}
        return Step(resp.output_text or "", calls, usage)

    def add_results(self, state: list[dict[str, Any]], results: list[tuple[ToolCall, str]]) -> None:
        state.extend(
            {"type": "function_call_output", "call_id": c.id, "output": o} for c, o in results
        )
