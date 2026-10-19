from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace
from typing import Any

import pytest

from attache import backends
from attache.backends.anthropic import AnthropicAdapter
from attache.backends.claude_cli import ClaudeCLI
from attache.backends.openai import ChatAdapter, ResponsesAdapter
from attache.backends.openai import _client as openai_client
from attache.types import Endpoint, Person, Tool, Turn, TurnContext

MISSING = "ATTACHE_TEST_MISSING_KEY"


async def _add(args: dict[str, Any], ctx: TurnContext) -> str:
    return str(args["a"] + args["b"])


ADD = Tool("add", "Add two numbers", {"type": "object", "properties": {}}, _add)


def make_turn(endpoint: Endpoint, history: list[dict[str, str]] | None = None) -> Turn:
    ctx = TurnContext("web:local", Person("owner", "Phil", "owner"), False, SimpleNamespace())
    return Turn(ctx, endpoint, "be brief", history or [], "what is 2+3?", [ADD])


class Recorder:
    """An async create() that returns canned responses and keeps a copy of each request."""

    def __init__(self, *responses: Any) -> None:
        self.responses, self.requests = list(responses), []

    async def __call__(self, **kwargs: Any) -> Any:
        self.requests.append(copy.deepcopy(kwargs))
        return self.responses.pop(0)


def run(adapter: Any, turn: Turn) -> Any:
    return asyncio.run(backends.APIBackend(adapter).run(turn))


# make() and keys


def test_make_picks_backend_by_api() -> None:
    def ep(api: str) -> Endpoint:
        return Endpoint("e", api, "m", key_env=MISSING)

    assert isinstance(backends.make(ep("anthropic")).adapter, AnthropicAdapter)
    assert isinstance(backends.make(ep("openai-chat")).adapter, ChatAdapter)
    assert isinstance(backends.make(ep("openai-responses")).adapter, ResponsesAdapter)
    assert isinstance(backends.make(ep("claude-cli")), ClaudeCLI)
    with pytest.raises(ValueError, match="openai-responses"):
        backends.make(ep("gemini"))


@pytest.mark.parametrize("api", ["anthropic", "openai-chat", "openai-responses"])
def test_missing_key_fails_at_first_turn(api: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MISSING, raising=False)
    endpoint = Endpoint("work", api, "m", key_env=MISSING)
    backend = backends.make(endpoint)  # no key needed yet
    with pytest.raises(RuntimeError, match=f"endpoint 'work' needs the {MISSING} environment"):
        asyncio.run(backend.run(make_turn(endpoint)))


def test_clients_are_built_lazily(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MISSING, "sk-test")
    local = ChatAdapter(Endpoint("local", "openai-chat", "m", "http://localhost:11434/v1"))
    assert local.client is None
    client = openai_client(local)
    assert client.api_key == "not-needed" and str(client.base_url).startswith("http://localhost")
    keyed = AnthropicAdapter(Endpoint("c", "anthropic", "m", key_env=MISSING))
    assert keyed._client().api_key == "sk-test"


# Anthropic


def test_anthropic_tool_round_trip() -> None:
    create = Recorder(
        SimpleNamespace(
            content=[
                SimpleNamespace(type="text", text="Let me add."),
                SimpleNamespace(type="tool_use", id="tu_1", name="add", input={"a": 2, "b": 3}),
            ],
            usage=SimpleNamespace(input_tokens=20, output_tokens=7),
        ),
        SimpleNamespace(
            content=[SimpleNamespace(type="text", text="5")],
            usage=SimpleNamespace(input_tokens=30, output_tokens=1),
        ),
    )
    endpoint = Endpoint("c", "anthropic", "claude-x", options={"max_tokens": 99})
    adapter = AnthropicAdapter(
        endpoint, client=SimpleNamespace(messages=SimpleNamespace(create=create))
    )
    reply = run(adapter, make_turn(endpoint))
    assert reply.text == "5"
    assert reply.usage == {"input_tokens": 50, "output_tokens": 8}
    first, second = create.requests
    assert first["model"] == "claude-x" and first["max_tokens"] == 99
    assert first["system"] == "be brief"
    assert first["tools"] == [
        {"name": "add", "description": "Add two numbers", "input_schema": ADD.schema}
    ]
    assert second["messages"][1:] == [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Let me add."},
                {"type": "tool_use", "id": "tu_1", "name": "add", "input": {"a": 2, "b": 3}},
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "tu_1", "content": "5"}],
        },
    ]


def test_anthropic_history_alternates_and_starts_with_user() -> None:
    history = [
        {"role": "assistant", "content": "Hello, I'm Attaché."},
        {"role": "user", "content": "one"},
        {"role": "user", "content": "two"},
        {"role": "assistant", "content": "a"},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "three"},
    ]
    turn = make_turn(Endpoint("c", "anthropic", "m"), history)
    assert AnthropicAdapter(turn.endpoint, client=SimpleNamespace()).start(turn) == [
        {"role": "user", "content": "one\n\ntwo"},
        {"role": "assistant", "content": "a\n\nb"},
        {"role": "user", "content": "three\n\nwhat is 2+3?"},
    ]
    assert history[1]["content"] == "one"  # the caller's history is not modified


def test_anthropic_without_tools_sends_none() -> None:
    create = Recorder(
        SimpleNamespace(
            content=[SimpleNamespace(type="text", text="hi")],
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )
    )
    endpoint = Endpoint("c", "anthropic", "m")
    turn = make_turn(endpoint)
    turn.tools = []
    assert (
        run(
            AnthropicAdapter(
                endpoint, client=SimpleNamespace(messages=SimpleNamespace(create=create))
            ),
            turn,
        ).text
        == "hi"
    )
    assert "tools" not in create.requests[0] and create.requests[0]["max_tokens"] == 4096


# OpenAI Chat Completions


def chat_response(content: str | None, *calls: tuple[str, str, str]) -> Any:
    tool_calls = [
        SimpleNamespace(id=i, function=SimpleNamespace(name=n, arguments=a)) for i, n, a in calls
    ] or None
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message)],
        usage=SimpleNamespace(prompt_tokens=5, completion_tokens=3),
    )


def chat_adapter(*responses: Any) -> tuple[ChatAdapter, Recorder]:
    create = Recorder(*responses)
    endpoint = Endpoint("local", "openai-chat", "qwen", "http://localhost:11434/v1")
    return ChatAdapter(
        endpoint,
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
    ), create


def test_chat_tool_round_trip() -> None:
    adapter, create = chat_adapter(
        chat_response(None, ("call_1", "add", '{"a": 2, "b": 3}')), chat_response("5")
    )
    history = [{"role": "assistant", "content": "earlier"}]
    reply = run(adapter, make_turn(adapter.endpoint, history))
    assert reply.text == "5" and reply.usage == {"input_tokens": 10, "output_tokens": 6}
    first, second = create.requests
    assert first["model"] == "qwen"
    assert first["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "assistant", "content": "earlier"},
        {"role": "user", "content": "what is 2+3?"},
    ]
    assert first["tools"] == [
        {
            "type": "function",
            "function": {"name": "add", "description": "Add two numbers", "parameters": ADD.schema},
        }
    ]
    assert second["messages"][3:] == [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "add", "arguments": '{"a": 2, "b": 3}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "5"},
    ]


def test_chat_bad_json_arguments_become_an_error_result() -> None:
    adapter, create = chat_adapter(
        chat_response(None, ("call_1", "add", "{not json")), chat_response("sorry")
    )
    assert run(adapter, make_turn(adapter.endpoint)).text == "sorry"
    tool_message = create.requests[1]["messages"][-1]
    assert tool_message["role"] == "tool"
    assert tool_message["content"] == "error: arguments were not valid JSON: {not json"


def test_chat_message_without_calls_has_no_tool_calls_key() -> None:
    adapter, _ = chat_adapter(chat_response("plain answer"))
    turn = make_turn(adapter.endpoint)
    state = adapter.start(turn)
    step = asyncio.run(adapter.step(state, turn))
    assert step.text == "plain answer" and step.calls == []
    assert state[-1] == {"role": "assistant", "content": "plain answer"}


# OpenAI Responses


class Item(SimpleNamespace):
    """Stands in for an SDK output item, which is a pydantic model."""

    def model_dump(self, exclude_none: bool = False) -> dict[str, Any]:
        return {k: v for k, v in vars(self).items() if v is not None or not exclude_none}


def test_responses_function_call_round_trip() -> None:
    reasoning = Item(type="reasoning", id="rs_1", encrypted_content="enc", summary=[])
    call = Item(
        type="function_call",
        id="fc_1",
        call_id="call_9",
        name="add",
        arguments='{"a": 2, "b": 3}',
        status=None,
    )
    message = Item(type="message", role="assistant", content=[{"type": "output_text", "text": "5"}])
    create = Recorder(
        SimpleNamespace(
            output=[reasoning, call],
            output_text="",
            usage=SimpleNamespace(input_tokens=9, output_tokens=4),
        ),
        SimpleNamespace(
            output=[message],
            output_text="5",
            usage=SimpleNamespace(input_tokens=12, output_tokens=1),
        ),
    )
    endpoint = Endpoint("openai", "openai-responses", "gpt-x", key_env="OPENAI_API_KEY")
    adapter = ResponsesAdapter(
        endpoint, client=SimpleNamespace(responses=SimpleNamespace(create=create))
    )
    reply = run(adapter, make_turn(endpoint))
    assert reply.text == "5" and reply.usage == {"input_tokens": 21, "output_tokens": 5}
    first, second = create.requests
    assert first["instructions"] == "be brief" and first["model"] == "gpt-x"
    assert first["store"] is False and first["include"] == ["reasoning.encrypted_content"]
    assert first["input"] == [{"role": "user", "content": "what is 2+3?"}]
    assert first["tools"][0] == {
        "type": "function",
        "name": "add",
        "description": "Add two numbers",
        "parameters": ADD.schema,
        "strict": False,
    }
    assert second["input"][1:] == [
        {"type": "reasoning", "id": "rs_1", "encrypted_content": "enc", "summary": []},
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_9",
            "name": "add",
            "arguments": '{"a": 2, "b": 3}',
        },
        {"type": "function_call_output", "call_id": "call_9", "output": "5"},
    ]
