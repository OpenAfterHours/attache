"""The shared tool loop for API backends, and the owner's progress events.

An Adapter speaks one wire protocol and keeps its own native message list (`state`). The loop
asks it for one model step at a time, runs any tool calls, and feeds the results back.

A long job may take many steps, so the loop stops on signs of a loop, not on a small count.
The same call giving the same result is a step that learned nothing: the model is told so the
second time (REPEATED), and the MAX_REPEATS-th time the turn is wound up. So is a turn that
reaches its step limit (step_limit) or most of its time (WIND_DOWN). Winding up tells the model
to stop and gives it one more step, whose calls are not run, to say what it did and what is
left; the reply ends by saying so, and "continue" picks the work up from there.

Every call and result stays in the turn's context until the turn ends, so a long turn's context
grows. Once a step's tokens (prompt and reply) reach the endpoint's context_tokens, the model
is told to write notes on where it is, and the turn goes on in a fresh context: the request,
those notes, and CARRY_ON. That works on every API, and no history is edited (the Claude API
rejects some edits to earlier turns once thinking blocks follow them).

progress() sends ChannelManager.activity events ("started", "step", "thinking", "finished")
for turns with owner rights only, so a group or a member never sees steps or thinking. A step's
label() names the tool and a few arguments, never contents.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from attache.types import Endpoint, Reply, Tool, ToolCall, Turn, TurnContext

MAX_STEPS = 20  # a turn without owner rights: other people's turns are on your bill
OWNER_STEPS = 100  # a turn with owner rights, unless its endpoint sets max_steps
MAX_REPEATS = 3  # the same call giving the same result this often in one turn is a loop
TIMEOUT = 900.0  # seconds a turn may take, unless its endpoint sets timeout (the router's)
WIND_DOWN = 0.8  # the share of that time after which the loop winds the turn up
CONTEXT_TOKENS = 150_000  # a context this big starts afresh, unless the endpoint says otherwise
REPEATED = "\n[Attaché: this call gave this same result before. Try another way, or stop.]"
WRAP_UP = (
    "\n[Attaché: stop now, because {why}. Call no more tools: reply with what you did, what you "
    "found and what is left, so the work can go on in a later turn.]"
)
NOTES = (
    "\n[Attaché: your context is nearly full, so after your next reply it starts afresh with "
    "only the request and that reply. Call no more tools: write notes on what you have done, "
    "found and still have to do, with every detail you need to carry on (names, paths, "
    "numbers, and what earlier notes said that still matters).]"
)
CARRY_ON = "[Attaché: your context was cleared to make room. Carry on from your notes above.]"
MAX_TOOL_OUTPUT = 20_000  # characters; longer results are cut with a note saying so
# The only arguments a step's label shows (40 characters each): never text, html, data, body,
# summary or question. Ours, then Claude Code's own tools' (claude_cli).
LABELLED = ("skill", "op", "collection", "path", "name", "command", "file_path", "pattern", "url")
# A label's word: up to the first space, "?" or "#", so a command shows only its program and a
# URL no query or fragment (where tokens go); 40 characters at most.
WORD = re.compile(r"[^\s?#]{0,40}")
# A turn's tokens (the router's "finished"): the cached ones count too, often most of them.
TOKENS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


@dataclass
class Step:
    text: str
    calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)


class Adapter(Protocol):
    def start(self, turn: Turn) -> Any:
        """Build the native message list from turn.history and turn.text."""

    async def step(self, state: Any, turn: Turn) -> Step:
        """One model call with turn.system and turn.tools. Appends the model's message to state."""

    def add_results(self, state: Any, results: list[tuple[ToolCall, str]]) -> None:
        """Append tool results in the protocol's native form."""


def api_key(endpoint: Endpoint) -> str | None:
    """The value of $key_env, or None when no key_env is set; RuntimeError when it is empty."""
    env = endpoint.key_env
    if env and not os.environ.get(env):
        raise RuntimeError(f"endpoint '{endpoint.name}' needs the {env} environment variable")
    return os.environ[env] if env else None


def tool_spec(tool: Tool, key: str = "parameters") -> dict[str, Any]:
    """{"name", "description", <key>: schema}; Anthropic and MCP call the schema key differently."""
    return {"name": tool.name, "description": tool.description, key: tool.schema}


async def progress(ctx: TurnContext, kind: str, **data: Any) -> None:
    """An activity event for the turn's chat, when the turn has owner rights (tools.access)."""
    if ctx.role == "owner" and not ctx.is_group:
        await ctx.services.channels.activity(ctx.chat_key, kind, **data)


def label(name: Any, args: Any) -> str:
    """A step's label: the tool's name and the first word (WORD) of each LABELLED argument, plus
    script's first arg (as in "script mail inbox") unless it is an inline spec {...}; at most
    80 characters. args may be anything a model or a stream sent: only a dict has words."""
    args = args if isinstance(args, dict) else {}
    values = [args.get(key) for key in LABELLED] + list(args.get("args") or [])[:1]
    words = [WORD.match(v)[0] for v in values if isinstance(v, str) and not v.startswith("{")]
    return " ".join([str(name), *words])[:80]


async def run_tool(call: ToolCall, turn: Turn) -> str:
    """Run one call against turn.tools. Unknown tools and exceptions become "error: ..." text,
    never exceptions, so the model can recover. Output is capped at MAX_TOOL_OUTPUT. Each call
    is a "step" event (progress), with how long it took."""
    if "__error__" in call.args:  # set by adapters when the model sent malformed arguments
        return f"error: {call.args['__error__']}"
    tool = next((t for t in turn.tools if t.name == call.name), None)
    if tool is None:
        return f"error: unknown tool {call.name!r}"
    t0 = time.monotonic()
    try:
        out = await tool.fn(call.args, turn.ctx)
    except Exception as e:
        out = f"error: {type(e).__name__}: {e}"
    seconds = time.monotonic() - t0
    await progress(turn.ctx, "step", label=label(call.name, call.args), seconds=seconds)
    if len(out) > MAX_TOOL_OUTPUT:
        out = f"{out[:MAX_TOOL_OUTPUT]}\n[cut: showing {MAX_TOOL_OUTPUT} of {len(out)} characters]"
    return out


def timeout(endpoint: Endpoint) -> float:
    """Seconds a turn on this endpoint may take: its timeout option, else TIMEOUT."""
    return float(endpoint.options.get("timeout", TIMEOUT))


def step_limit(turn: Turn) -> int:
    """How many steps the turn may take: its endpoint's max_steps (default OWNER_STEPS) with
    owner rights, and never more than MAX_STEPS without them."""
    steps = int(turn.endpoint.options.get("max_steps", OWNER_STEPS))
    owner = turn.ctx.role == "owner" and not turn.ctx.is_group
    return steps if owner else min(steps, MAX_STEPS)


def fresh(turn: Turn, notes: str) -> Turn:
    """The turn to start afresh from the model's notes: its request, the notes as the model's
    reply, then CARRY_ON as the new message."""
    request = [{"role": "user", "content": turn.text}, {"role": "assistant", "content": notes}]
    return replace(turn, history=[*turn.history, *request], text=CARRY_ON)


async def tool_loop(adapter: Adapter, turn: Turn, max_steps: int | None = None) -> Reply:
    """Step until the model stops calling tools, or wind the turn up (see the module docstring)
    after max_steps (default step_limit), a loop, or WIND_DOWN of the endpoint's timeout. A
    context that reaches context_tokens starts afresh from the model's notes (fresh). Usage
    from every step, the wrap-up's and the notes' included, is summed into Reply.usage."""
    limit = max_steps or step_limit(turn)
    room = int(turn.endpoint.options.get("context_tokens", CONTEXT_TOKENS))
    wind_down = time.monotonic() + WIND_DOWN * timeout(turn.endpoint)
    state = adapter.start(turn)
    usage: dict[str, Any] = {}
    seen: Counter[int] = Counter()  # hashes of (call, result): outputs can be long
    steps, text, why, full = 0, "", "", False
    while True:
        step = await adapter.step(state, turn)
        steps += 1
        for key, value in step.usage.items():
            usage[key] = usage.get(key, 0) + (value or 0)
        text = step.text or text
        if full:  # that step wrote the notes: go on from them, and only them
            state, full = adapter.start(fresh(turn, step.text or text or "(no notes)")), False
            seen.clear()  # calls made again in the fresh context are not a loop
            continue
        if why or not step.calls:  # done, or that was the wrap-up: its calls are not run
            break
        results = []
        for call in step.calls:
            out = await run_tool(call, turn)
            mark = hash((call.name, json.dumps(call.args, sort_keys=True, default=str), out))
            seen[mark] += 1
            results.append((call, out + REPEATED if seen[mark] > 1 else out))
        if max(seen.values()) >= MAX_REPEATS:
            why = "the same call keeps giving the same result"
        elif steps >= limit:
            why = f"this turn has used its {limit} steps"
        elif time.monotonic() > wind_down:
            why = "this turn is near its time limit"
        full = not why and sum(step.usage.get(key) or 0 for key in TOKENS) >= room
        if why or full:
            call, out = results[-1]
            results[-1] = (call, out + (WRAP_UP.format(why=why) if why else NOTES))
        adapter.add_results(state, results)
    if not why:
        return Reply(step.text, usage=usage)
    note = f'(Stopped because {why}. Say "continue" and I\'ll pick up from here.)'
    return Reply(f"{text}\n\n{note}".lstrip(), usage=usage)
