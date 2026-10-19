"""The shared tool loop for API backends. Owner: models. Budget ~55 lines.

An Adapter speaks one wire protocol and keeps its own native message list (`state`). The loop
asks it for one model step at a time, runs any tool calls, and feeds the results back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from attache.types import Reply, Tool, ToolCall, Turn

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


def tool_spec(tool: Tool) -> dict[str, Any]:
    """Neutral description adapters translate: {"name", "description", "parameters"}."""
    raise NotImplementedError


async def run_tool(call: ToolCall, turn: Turn) -> str:
    """Run one call against turn.tools. Unknown tools and exceptions become "error: ..." text,
    never exceptions, so the model can recover. Output is capped at MAX_TOOL_OUTPUT."""
    raise NotImplementedError


async def tool_loop(adapter: Adapter, turn: Turn, max_steps: int = MAX_STEPS) -> Reply:
    """Step until the model stops calling tools or max_steps is hit (then say so in the reply).
    Usage from every step is summed into Reply.usage."""
    raise NotImplementedError
