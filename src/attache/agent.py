"""The shared tool loop for API backends.

An Adapter speaks one wire protocol and keeps its own native message list (`state`). The loop
asks it for one model step at a time, runs any tool calls, and feeds the results back.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Protocol

from attache.types import Endpoint, Reply, Tool, ToolCall, Turn

MAX_STEPS = 20
MAX_TOOL_OUTPUT = 20_000  # characters; longer results are cut with a note saying so


@dataclass
class Step:
    text: str
    calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)


class Adapter(Protocol):
    def start(self, turn: Turn) -> Any:
        """Build the native message list from turn.history and turn.text."""
        ...

    async def step(self, state: Any, turn: Turn) -> Step:
        """One model call with turn.system and turn.tools. Appends the model's message to state."""
        ...

    def add_results(self, state: Any, results: list[tuple[ToolCall, str]]) -> None:
        """Append tool results in the protocol's native form."""
        ...


def api_key(endpoint: Endpoint) -> str | None:
    """The value of $key_env, or None when no key_env is set; RuntimeError when it is empty."""
    env = endpoint.key_env
    if env and not os.environ.get(env):
        raise RuntimeError(f"endpoint '{endpoint.name}' needs the {env} environment variable")
    return os.environ[env] if env else None


def tool_spec(tool: Tool, key: str = "parameters") -> dict[str, Any]:
    """{"name", "description", <key>: schema}; Anthropic and MCP call the schema key differently."""
    return {"name": tool.name, "description": tool.description, key: tool.schema}


async def run_tool(call: ToolCall, turn: Turn) -> str:
    """Run one call against turn.tools. Unknown tools and exceptions become "error: ..." text,
    never exceptions, so the model can recover. Output is capped at MAX_TOOL_OUTPUT."""
    if "__error__" in call.args:  # set by adapters when the model sent malformed arguments
        return f"error: {call.args['__error__']}"
    tool = next((t for t in turn.tools if t.name == call.name), None)
    if tool is None:
        return f"error: unknown tool {call.name!r}"
    try:
        out = await tool.fn(call.args, turn.ctx)
    except Exception as e:
        return f"error: {type(e).__name__}: {e}"
    if len(out) > MAX_TOOL_OUTPUT:
        out = f"{out[:MAX_TOOL_OUTPUT]}\n[cut: showing {MAX_TOOL_OUTPUT} of {len(out)} characters]"
    return out


async def tool_loop(adapter: Adapter, turn: Turn, max_steps: int = MAX_STEPS) -> Reply:
    """Step until the model stops calling tools or max_steps is hit (then say so in the reply).
    Usage from every step is summed into Reply.usage."""
    state = adapter.start(turn)
    usage: dict[str, Any] = {}
    text = ""
    for _ in range(max_steps):
        step = await adapter.step(state, turn)
        for key, value in step.usage.items():
            usage[key] = usage.get(key, 0) + (value or 0)
        text = step.text or text
        if not step.calls:
            return Reply(step.text, usage=usage)
        results = [(call, await run_tool(call, turn)) for call in step.calls]
        adapter.add_results(state, results)
    return Reply(f"{text}\n\n(I stopped after {max_steps} steps.)".lstrip(), usage=usage)
