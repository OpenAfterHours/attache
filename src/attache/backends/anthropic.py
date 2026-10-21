"""Anthropic Messages API adapter, for the Claude API and any Anthropic-compatible base_url."""

from __future__ import annotations

from typing import Any

from attache.agent import TOKENS, Step, api_key, tool_spec
from attache.types import Endpoint, ToolCall, Turn


class AnthropicAdapter:
    def __init__(self, endpoint: Endpoint, client: Any = None) -> None:
        """client: an AsyncAnthropic-like object; tests pass a fake. None builds one lazily from
        endpoint.base_url and the env var named by endpoint.key_env."""
        self.endpoint = endpoint
        self.client = client

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
        tools = [tool_spec(t, key="input_schema") for t in turn.tools]
        resp = await self._client().messages.create(
            model=self.endpoint.model,
            max_tokens=self.endpoint.options.get("max_tokens", 4096),
            system=turn.system,
            messages=state,
            **({"tools": tools} if tools else {}),
        )
        # Send the blocks back as they came, so thinking blocks keep their signatures.
        blocks = [b.model_dump(exclude_none=True) for b in resp.content]
        state.append({"role": "assistant", "content": blocks})
        text = "".join(b.text for b in resp.content if b.type == "text")
        calls = [ToolCall(b.id, b.name, b.input) for b in resp.content if b.type == "tool_use"]
        usage = {key: getattr(resp.usage, key, None) for key in TOKENS}  # None: not cached
        return Step(text, calls, usage)

    def add_results(self, state: list[dict[str, Any]], results: list[tuple[ToolCall, str]]) -> None:
        blocks = [
            {"type": "tool_result", "tool_use_id": call.id, "content": output}
            for call, output in results
        ]
        state.append({"role": "user", "content": blocks})
