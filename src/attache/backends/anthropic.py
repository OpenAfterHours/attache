"""Anthropic Messages API adapter. Owner: models. Budget ~65 lines.

Works with the Claude API and any Anthropic-compatible base_url. Uses anthropic.AsyncAnthropic.
"""

from __future__ import annotations

from typing import Any

from attache.agent import Step, api_key
from attache.types import Endpoint, ToolCall, Turn


class AnthropicAdapter:
    def __init__(self, endpoint: Endpoint, client: Any = None) -> None:
        """client: an AsyncAnthropic-like object; tests pass a fake. None builds one lazily from
        endpoint.base_url and the env var named by endpoint.key_env."""
        self.endpoint, self.client = endpoint, client

    def _client(self) -> Any:
        if self.client is None:
            key = api_key(self.endpoint)
            import anthropic

            self.client = anthropic.AsyncAnthropic(api_key=key, base_url=self.endpoint.base_url)
        return self.client

    def start(self, turn: Turn) -> list[dict[str, Any]]:
        # The API wants strict user/assistant alternation that opens with a user message.
        msgs: list[dict[str, Any]] = []
        for m in [*turn.history, {"role": "user", "content": turn.text}]:
            if msgs and msgs[-1]["role"] == m["role"]:
                msgs[-1]["content"] += "\n\n" + m["content"]
            elif msgs or m["role"] == "user":
                msgs.append({"role": m["role"], "content": m["content"]})
        return msgs

    async def step(self, state: list[dict[str, Any]], turn: Turn) -> Step:
        tools = [
            {"name": t.name, "description": t.description, "input_schema": t.schema}
            for t in turn.tools
        ]
        resp = await self._client().messages.create(
            model=self.endpoint.model,
            max_tokens=self.endpoint.options.get("max_tokens", 4096),
            system=turn.system,
            messages=state,
            **({"tools": tools} if tools else {}),
        )
        blocks: list[dict[str, Any]] = []
        for b in resp.content:
            if b.type == "text":
                blocks.append({"type": "text", "text": b.text})
            elif b.type == "tool_use":
                blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
        state.append({"role": "assistant", "content": blocks})
        u = resp.usage
        return Step(
            text="".join(b["text"] for b in blocks if b["type"] == "text"),
            calls=[ToolCall(b["id"], b["name"], b["input"]) for b in blocks if "input" in b],
            usage={"input_tokens": u.input_tokens, "output_tokens": u.output_tokens},
        )

    def add_results(self, state: list[dict[str, Any]], results: list[tuple[ToolCall, str]]) -> None:
        blocks = [{"type": "tool_result", "tool_use_id": c.id, "content": o} for c, o in results]
        state.append({"role": "user", "content": blocks})
