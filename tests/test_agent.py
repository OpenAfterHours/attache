from __future__ import annotations

import asyncio
import itertools
import time
from types import SimpleNamespace
from typing import Any

from attache import agent
from attache.agent import MAX_TOOL_OUTPUT, Step, label, run_tool, step_limit, tool_loop, tool_spec
from attache.types import Endpoint, Person, Tool, ToolCall, Turn, TurnContext


class Hub:
    """Records ChannelManager.activity events."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    async def activity(self, chat_key: str, kind: str, **data: Any) -> None:
        self.events.append((chat_key, kind, data))


def make_turn(tools: list[Tool], role: str = "owner", chat: str = "local:me") -> Turn:
    services = SimpleNamespace(channels=Hub())
    ctx = TurnContext(chat, Person("owner", "Phil", role), chat.startswith("slack:C"), services)
    return Turn(ctx, Endpoint("test", "anthropic", "m"), "system", [], "hi", tools)


def test_label_names_the_tool_and_a_few_arguments_never_contents() -> None:
    command = 'curl -H "Authorization: Bearer s3cret" https://x.example/a'
    assert label("run", {"command": command, "timeout": 5}) == "run curl"  # the program only
    assert label("run", {"command": "x" * 60}) == "run " + "x" * 40
    url = "https://example.com/files/report.pdf?token=s3cret#page=2"
    assert label("WebFetch", {"url": url}) == "WebFetch https://example.com/files/report.pdf"
    spec = {"skill": "docx", "script": "make.py", "args": ['{"title": "My diary"}', "out.docx"]}
    assert label("script", spec) == "script docx"  # an inline spec is contents
    assert label("Glob", ["*.py"]) == "Glob" and label(None, None) == "None"  # from a stream
    assert label("db", {"op": "put", "collection": "todos", "data": {"x": 1}}) == "db put todos"
    assert label("script", {"skill": "mail", "script": "mail.py", "args": ["inbox", "--n=5"]}) == (
        "script mail inbox"
    )
    secret = {"path": "notes/a.md", "content": "my diary", "text": "t", "html": "<p>h</p>"}
    assert label("write_file", secret) == "write_file notes/a.md"
    for kept_out in ("body", "summary", "question", "data", "html", "text", "content"):
        assert label("x", {kept_out: "contents"}) == "x"
    assert label("Read", {"file_path": "src/app.py"}) == "Read src/app.py"  # Claude Code's own
    assert len(label("artifact", {"name": "n" * 40, "path": "p" * 40, "op": "o" * 40})) == 80


def test_run_tool_reports_each_call_as_a_step_for_the_owner_only() -> None:
    def boom(args: dict[str, Any], ctx: TurnContext) -> str:
        raise ValueError("bad input")

    tools = [make_tool("db", lambda a, c: "[]"), make_tool("boom", boom)]
    turn = make_turn(tools)
    asyncio.run(run_tool(ToolCall("1", "db", {"op": "list", "collection": "todos"}), turn))
    asyncio.run(run_tool(ToolCall("2", "boom", {}), turn))  # a failed call is a step too
    events = turn.ctx.services.channels.events
    assert [(key, kind, data["label"]) for key, kind, data in events] == [
        ("local:me", "step", "db list todos"),
        ("local:me", "step", "boom"),
    ]
    assert all(isinstance(data["seconds"], float) for _, _, data in events)
    for role, chat in [("member", "slack:DSAM"), ("owner", "slack:CFAM"), ("page", "local:me")]:
        other = make_turn(tools, role, chat)  # no owner rights: a member, a group, or a page
        asyncio.run(run_tool(ToolCall("3", "db", {"op": "list"}), other))
        assert other.ctx.services.channels.events == []


def make_tool(name: str, fn: Any) -> Tool:
    async def wrapped(args: dict[str, Any], ctx: TurnContext) -> Any:
        return fn(args, ctx)

    return Tool(name, f"{name} tool", {"type": "object"}, wrapped)


class ScriptedAdapter:
    """Replays canned steps and records the tool results it is given."""

    def __init__(self, steps: list[Step]) -> None:
        self.steps, self.results, self.starts = steps, [], []

    def start(self, turn: Turn) -> list[Any]:
        self.starts.append(turn)
        return []

    async def step(self, state: list[Any], turn: Turn) -> Step:
        return self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]

    def add_results(self, state: list[Any], results: list[tuple[ToolCall, str]]) -> None:
        self.results.append(results)


def test_tool_spec_is_neutral() -> None:
    tool = make_tool("add", lambda a, c: "")
    assert tool_spec(tool) == {
        "name": "add",
        "description": "add tool",
        "parameters": {"type": "object"},
    }
    assert tool_spec(tool, key="input_schema")["input_schema"] == {"type": "object"}


def test_run_tool_passes_args_and_context() -> None:
    turn = make_turn([make_tool("who", lambda a, c: f"{a['x']} {c.sender.name}")])
    assert asyncio.run(run_tool(ToolCall("1", "who", {"x": "hello"}), turn)) == "hello Phil"


def test_run_tool_errors_become_text() -> None:
    def boom(args: dict[str, Any], ctx: TurnContext) -> str:
        raise ValueError("bad input")

    turn = make_turn([make_tool("boom", boom)])
    assert asyncio.run(run_tool(ToolCall("1", "boom", {}), turn)) == "error: ValueError: bad input"
    out = asyncio.run(run_tool(ToolCall("2", "missing", {}), turn))
    assert out.startswith("error: unknown tool") and "missing" in out


def test_run_tool_caps_output() -> None:
    turn = make_turn([make_tool("big", lambda a, c: "x" * (MAX_TOOL_OUTPUT + 500))])
    out = asyncio.run(run_tool(ToolCall("1", "big", {}), turn))
    assert out.startswith("x" * MAX_TOOL_OUTPUT)
    assert len(out) < MAX_TOOL_OUTPUT + 100 and str(MAX_TOOL_OUTPUT + 500) in out


def test_run_tool_reports_malformed_args_without_running() -> None:
    ran = []
    turn = make_turn([make_tool("t", lambda a, c: ran.append(a) or "ran")])
    out = asyncio.run(run_tool(ToolCall("1", "t", {"__error__": "arguments were bad"}), turn))
    assert out == "error: arguments were bad" and ran == []


def test_tool_loop_runs_tools_then_answers() -> None:
    turn = make_turn([make_tool("add", lambda a, c: str(a["a"] + a["b"]))])
    call = ToolCall("c1", "add", {"a": 2, "b": 3})
    adapter = ScriptedAdapter(
        [
            Step("thinking", [call], {"input_tokens": 10, "output_tokens": 2}),
            Step("It is 5.", [], {"input_tokens": 15, "output_tokens": 4, "cached": None}),
        ]
    )
    reply = asyncio.run(tool_loop(adapter, turn))
    assert reply.text == "It is 5."
    assert adapter.results == [[(call, "5")]]
    assert reply.usage == {"input_tokens": 25, "output_tokens": 6, "cached": 0}


def test_tool_loop_winds_up_after_max_steps_without_running_more_tools() -> None:
    count = itertools.count(1)
    turn = make_turn([make_tool("next", lambda a, c: f"page {next(count)}")])
    working = Step("still working", [ToolCall("c", "next", {})], {"n": 1})
    summary = Step("Read pages 1-3; 4 on are left.", [ToolCall("d", "next", {})], {"n": 1})
    adapter = ScriptedAdapter([working, working, working, summary])
    reply = asyncio.run(tool_loop(adapter, turn, max_steps=3))
    assert reply.text == (
        "Read pages 1-3; 4 on are left.\n\n(Stopped because this turn has used its 3 steps. "
        'Say "continue" and I\'ll pick up from here.)'
    )
    outputs = [out for results in adapter.results for _, out in results]
    assert outputs[:2] == ["page 1", "page 2"] and outputs[2].startswith("page 3\n[Attaché: stop")
    assert next(count) == 4  # the wrap-up's call was not run
    assert reply.usage == {"n": 4}  # three steps and the wrap-up


def test_tool_loop_flags_a_repeated_result_and_stops_a_loop() -> None:
    turn = make_turn([make_tool("status", lambda a, c: "running")])
    polling = Step("", [ToolCall("c", "status", {"job": "va", "n": 1})])
    adapter = ScriptedAdapter([polling, polling, polling, Step("", [])])
    reply = asyncio.run(tool_loop(adapter, turn, max_steps=50))
    outputs = [out for results in adapter.results for _, out in results]
    assert outputs[0] == "running" and outputs[1] == "running" + agent.REPEATED
    assert "same call keeps giving the same result" in outputs[2]
    assert reply.text.startswith("(Stopped because the same call keeps giving the same result.")


def test_tool_loop_lets_a_repeated_call_with_a_new_result_go_on() -> None:
    count = itertools.count(1)
    turn = make_turn([make_tool("test", lambda a, c: f"{next(count)} failed")])
    run = Step("", [ToolCall("c", "test", {})])
    adapter = ScriptedAdapter([run, run, run, run, Step("All pass now.", [])])
    reply = asyncio.run(tool_loop(adapter, turn))
    assert reply.text == "All pass now." and len(adapter.results) == 4
    assert not any(agent.REPEATED in out for results in adapter.results for _, out in results)


def test_tool_loop_winds_up_near_the_endpoints_timeout() -> None:
    turn = make_turn([make_tool("slow", lambda a, c: time.sleep(0.1) or "done")])
    turn.endpoint.options["timeout"] = 0.05
    slow = Step("", [ToolCall("c", "slow", {})])
    adapter = ScriptedAdapter([slow, Step("Half done.", [])])
    reply = asyncio.run(tool_loop(adapter, turn))
    assert reply.text.startswith("Half done.\n\n(Stopped because this turn is near its time limit")
    assert len(adapter.results) == 1


def test_a_full_context_starts_afresh_from_the_models_notes() -> None:
    count = itertools.count(1)
    turn = make_turn([make_tool("read", lambda a, c: f"part {next(count)}")])
    turn.history = [{"role": "user", "content": "earlier"}]
    turn.endpoint.options["context_tokens"] = 1000
    read = ToolCall("c", "read", {})
    adapter = ScriptedAdapter(
        [
            Step("", [read], {"input_tokens": 900, "output_tokens": 50}),
            Step("", [read], {"input_tokens": 400, "cache_read_input_tokens": 600}),  # 1000: full
            Step("Notes: parts 1-2 read.", [read], {"input_tokens": 1100}),  # its call isn't run
            Step("", [read], {"input_tokens": 200}),
            Step("Done.", [], {"input_tokens": 300}),
        ]
    )
    reply = asyncio.run(tool_loop(adapter, turn))
    assert reply.text == "Done." and reply.usage["input_tokens"] == 2900
    outputs = [out for results in adapter.results for _, out in results]
    assert outputs == ["part 1", "part 2" + agent.NOTES, "part 3"]
    first, again = adapter.starts
    assert first is turn and again.text == agent.CARRY_ON
    assert again.history == [
        {"role": "user", "content": "earlier"},
        {"role": "user", "content": "hi"},  # the request, as it came
        {"role": "assistant", "content": "Notes: parts 1-2 read."},
    ]
    assert again.system == turn.system and again.tools == turn.tools


def test_step_limit_is_generous_for_the_owner_only() -> None:
    owner, member = make_turn([]), make_turn([], "member", "slack:DSAM")
    in_group = make_turn([], "owner", "slack:CFAM")  # an owner turn in a group: member rights
    assert step_limit(owner) == agent.OWNER_STEPS
    assert step_limit(member) == step_limit(in_group) == agent.MAX_STEPS
    for turn in (owner, member, in_group):
        turn.endpoint.options["max_steps"] = 500
    assert (step_limit(owner), step_limit(member), step_limit(in_group)) == (500, 20, 20)
    member.endpoint.options["max_steps"] = 5
    assert step_limit(member) == 5
