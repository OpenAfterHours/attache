from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from attache.agent import MAX_TOOL_OUTPUT, Step, label, run_tool, tool_loop, tool_spec
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
        self.steps, self.results = steps, []

    def start(self, turn: Turn) -> list[Any]:
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


def test_tool_loop_stops_after_max_steps() -> None:
    turn = make_turn([make_tool("again", lambda a, c: "more")])
    adapter = ScriptedAdapter([Step("still working", [ToolCall("c", "again", {})], {"n": 1})])
    reply = asyncio.run(tool_loop(adapter, turn, max_steps=3))
    assert reply.text == "still working\n\n(I stopped after 3 steps.)"
    assert len(adapter.results) == 3 and reply.usage == {"n": 3}
