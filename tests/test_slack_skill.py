"""The Slack skill: its sidecar's protocol translation (offline, without slack-bolt), its
SKILL.md and its app manifest."""

from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import re
import sys
import tomllib
import types
from pathlib import Path
from typing import Any

import pytest
import yaml

from attache import skills

FOLDER = skills.BUNDLED / "slack"
SELF = "U0BOT"
NAMES = {"U0SAM": "Sam"}


def load_channel() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("slack_channel", FOLDER / "channel.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


channel = load_channel()


def event(**fields: Any) -> dict[str, Any]:
    base = {"type": "message", "channel": "C0TEAM", "channel_type": "channel", "user": "U0SAM"}
    return {**base, "ts": "1729.42", "text": "hello", **fields}


# Inbound messages


def test_a_direct_message() -> None:
    dm = event(channel="D0SAM", channel_type="im", text="what time is the ferry?")
    assert channel.inbound(dm, SELF, NAMES) == {
        "type": "message",
        "chat_id": "D0SAM",
        "message_id": "1729.42",
        "sender_id": "U0SAM",
        "sender_name": "Sam",
        "text": "what time is the ferry?",
        "is_group": False,
        "mentioned": False,
        "files": [],
    }


def test_a_channel_mention_is_stripped_and_unescaped() -> None:
    msg = channel.inbound(event(text="<@U0BOT> is 3 &lt; 4 &amp; 5?"), SELF, NAMES)
    assert msg and msg["is_group"] and msg["mentioned"]
    assert msg["text"] == "is 3 < 4 & 5?"
    labelled = channel.inbound(event(text="hi <@U0BOT|attache>"), SELF, NAMES)
    assert labelled and labelled["mentioned"] and labelled["text"] == "hi"


def test_other_mentions_and_plain_channel_messages() -> None:
    msg = channel.inbound(event(text="ask <@U0SAM> later"), SELF, NAMES)
    assert msg and not msg["mentioned"] and msg["text"] == "ask <@U0SAM> later"


def test_a_reply_in_the_bots_thread_counts_as_a_mention() -> None:
    reply = event(ts="1729.50", thread_ts="1729.40", parent_user_id=SELF, text="and Sunday?")
    msg = channel.inbound(reply, SELF, NAMES)
    assert msg and msg["mentioned"]


def test_an_app_mention_has_no_channel_type_but_is_a_group() -> None:
    mention = event(type="app_mention", text="<@U0BOT> hi")
    del mention["channel_type"]
    msg = channel.inbound(mention, SELF, NAMES)
    assert msg and msg["is_group"] and msg["mentioned"]


def test_bots_self_and_other_subtypes_are_ignored() -> None:
    ignored = [
        event(bot_id="B0OTHER", user="U0OTHERBOT"),
        event(bot_id="B0OTHER", user=None),
        event(user=SELF),
        event(subtype="message_changed"),
        event(subtype="channel_join"),
        event(subtype="bot_message"),
        event(text="<@U0BOT>"),  # nothing left once the mention goes
    ]
    assert [channel.inbound(e, SELF, NAMES) for e in ignored] == [None] * len(ignored)
    assert channel.inbound(event(subtype="thread_broadcast"), SELF, NAMES)


def test_a_mention_delivered_twice_is_one_message() -> None:
    seen: dict[Any, Any] = {}
    mention = event(type="app_mention", text="<@U0BOT> hi")
    assert channel.inbound(mention, SELF, NAMES, seen)
    assert channel.inbound(event(text="<@U0BOT> hi"), SELF, NAMES, seen) is None
    assert channel.inbound(event(ts="1729.43"), SELF, NAMES, seen)  # a new message
    assert channel.inbound(event(channel="C0OTHER"), SELF, NAMES, seen)  # same ts, other chat


def test_seen_messages_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(channel, "KEEP", 3)
    seen: dict[Any, Any] = {}
    for i in range(5):
        channel.inbound(event(ts=f"1.{i}"), SELF, NAMES, seen)
    assert list(seen) == [("C0TEAM", "1.2"), ("C0TEAM", "1.3"), ("C0TEAM", "1.4")]


def test_a_file_share() -> None:
    file = {"id": "F0PDF", "name": "menu.pdf", "url_private_download": "https://files.slack.com/x"}
    msg = channel.inbound(event(subtype="file_share", text="", files=[file]), SELF, NAMES)
    assert msg and msg["text"] == "" and msg["files"] == []  # run() downloads, then fills it


def test_unknown_senders_fall_back_to_their_id() -> None:
    msg = channel.inbound(event(user="U0NEW"), SELF, NAMES)
    assert msg and msg["sender_name"] == "U0NEW"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("menu.pdf", "F01-menu.pdf"),
        ("../../evil.txt", "F01-.._.._evil.txt"),
        ("C:\\Windows\\x.bat", "F01-C__Windows_x.bat"),
        ("café plan.docx", "F01-café plan.docx"),
        ("trailing. ", "F01-trailing"),
        ("", "F01-file"),
    ],
)
def test_local_file_names_are_safe(name: str, expected: str) -> None:
    assert channel.local_name({"id": "F01", "name": name}) == expected


def test_the_token_only_goes_to_slack() -> None:
    assert channel.is_slack_url("https://files.slack.com/files-pri/T0-F0/download/a.pdf")
    assert not channel.is_slack_url("http://files.slack.com/a.pdf")
    assert not channel.is_slack_url("https://files.slack.com.evil.example/a.pdf")
    assert not channel.is_slack_url("")


# Threads


def test_replies_thread_in_channels_and_stay_inline_in_direct_messages() -> None:
    roots: dict[str, str | None] = {}
    for e in [
        event(ts="1.1"),  # top level in a channel: start a thread under it
        event(ts="1.3", thread_ts="1.2"),  # inside a thread: answer in that thread
        event(ts="2.1", channel="D0SAM", channel_type="im"),  # top level in a DM: inline
        event(ts="2.3", channel="D0SAM", channel_type="im", thread_ts="2.2"),
    ]:
        channel.remember(roots, e["ts"], channel.thread_root(e))
    assert channel.reply_thread("1.1", "C0TEAM", roots) == "1.1"
    assert channel.reply_thread("1.3", "C0TEAM", roots) == "1.2"
    assert channel.reply_thread("2.1", "D0SAM", roots) is None
    assert channel.reply_thread("2.3", "D0SAM", roots) == "2.2"
    assert channel.reply_thread(None, "C0TEAM", roots) is None
    assert channel.reply_thread("9.9", "C0TEAM", roots) == "9.9"  # from before a restart
    assert channel.reply_thread("9.9", "D0SAM", roots) is None


# Approvals


def test_ask_blocks() -> None:
    blocks = channel.ask_blocks("Sam asked: send <the list>?", ["Approve", "Decline"], "a19")
    section, actions = blocks
    assert section["type"] == "section"
    assert section["text"] == {"type": "mrkdwn", "text": "Sam asked: send &lt;the list&gt;?"}
    assert actions["type"] == "actions"
    buttons = actions["elements"]
    assert [b["action_id"] for b in buttons] == ["attache_answer_0", "attache_answer_1"]
    assert all(re.match("^attache_answer_", b["action_id"]) for b in buttons)
    assert [b["text"] for b in buttons] == [
        {"type": "plain_text", "text": "Approve"},
        {"type": "plain_text", "text": "Decline"},
    ]
    assert [json.loads(b["value"]) for b in buttons] == [
        {"ask_id": "a19", "choice": "Approve"},
        {"ask_id": "a19", "choice": "Decline"},
    ]
    assert buttons[0]["style"] == "primary" and "style" not in buttons[1]


def test_a_long_question_is_split_into_sections() -> None:
    blocks = channel.ask_blocks("x" * 7000, ["OK"], "a1")
    assert [len(b["text"]["text"]) for b in blocks[:-1]] == [3000, 3000, 1000]


def test_answer_from_action() -> None:
    body = {
        "type": "block_actions",
        "user": {"id": "U0ALEX", "name": "alex"},
        "channel": {"id": "D0ALEXDM"},
        "message": {"ts": "1729.60", "blocks": []},
        "actions": [
            {
                "action_id": "attache_answer_1",
                "value": json.dumps({"ask_id": "a19", "choice": "Decline"}),
            }
        ],
    }
    assert channel.answer_from_action(body) == {
        "type": "answer",
        "ask_id": "a19",
        "sender_id": "U0ALEX",
        "choice": "Decline",
    }


def test_answered_blocks_replace_the_buttons() -> None:
    asked = channel.ask_blocks("Send it?", ["Approve", "Decline"], "a19")
    done = channel.answered_blocks(asked, "Approve", "U0ALEX")
    assert done[0] == asked[0]
    assert done[1] == {
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": "✓ Approve by <@U0ALEX>"}],
    }
    assert all(b["type"] != "actions" for b in done)


# Commands


def test_parse_command() -> None:
    send = {"type": "send", "chat_id": "C0", "text": "hi", "files": [], "reply_to": None}
    assert channel.parse_command(json.dumps(send) + "\r\n") == send
    assert channel.parse_command('{"type": "stop"}') == {"type": "stop"}
    assert channel.parse_command('{"type": "typing", "chat_id": "C0", "on": true}')
    for line in ["", "not json", "[1, 2]", '"send"', '{"type": "dance"}', '{"type": ["send"]}']:
        assert channel.parse_command(line) is None


def test_stdin_commands_are_queued_and_end_of_input_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = '{"type": "send", "chat_id": "C0", "text": "é"}\nnoise\n{"type": "ask"}\n'
    monkeypatch.setattr(sys, "stdin", io.StringIO(lines))

    async def collect() -> list[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, channel.read_commands, loop, queue)
        await asyncio.sleep(0)  # let the queued callbacks run
        return [queue.get_nowait() for _ in range(queue.qsize())]

    assert [c["type"] for c in asyncio.run(collect())] == ["send", "ask", "stop"]


def test_special_mentions_never_ping() -> None:
    text = "Sam says <!channel> and <!here|here>, ping <!subteam^S01|devs> and <@U0SAM>"
    assert channel.quiet(text) == "Sam says @channel and @here, ping @subteam^S01 and <@U0SAM>"


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, method: str) -> Any:
        async def call(**kwargs: Any) -> dict[str, Any]:
            self.calls.append((method, kwargs))
            return {"ok": True, "user": {"name": "sam", "profile": {"display_name": "Sammy"}}}

        return call


def test_send_threads_text_as_markdown() -> None:
    client = FakeClient()
    command = {"type": "send", "chat_id": "C0", "text": "**Yes** <!here>", "reply_to": "1.1"}
    asyncio.run(channel.send(client, command, {"1.1": "1.0"}))
    assert client.calls == [
        (
            "chat_postMessage",
            {"channel": "C0", "thread_ts": "1.0", "markdown_text": "**Yes** @here"},
        )
    ]


def test_send_uploads_files_with_the_text_as_comment(tmp_path: Path) -> None:
    report = tmp_path / "report.pdf"
    report.write_bytes(b"%PDF")
    client = FakeClient()
    files = [str(report), str(tmp_path / "missing.pdf")]
    command = {"type": "send", "chat_id": "D0", "text": "Here", "files": files, "reply_to": None}
    asyncio.run(channel.send(client, command, {}))
    assert client.calls == [
        (
            "files_upload_v2",
            {
                "channel": "D0",
                "file_uploads": [{"file": str(report)}],
                "initial_comment": "Here",
                "thread_ts": None,
            },
        )
    ]


def test_ask_posts_buttons() -> None:
    client = FakeClient()
    command = {"type": "ask", "chat_id": "D0", "ask_id": "a1", "text": "Go?", "options": ["Yes"]}
    asyncio.run(channel.ask(client, command))
    [(method, kwargs)] = client.calls
    assert method == "chat_postMessage" and kwargs["text"] == "Go?"
    assert kwargs["blocks"] == channel.ask_blocks("Go?", ["Yes"], "a1")


def test_names_are_looked_up_once() -> None:
    client, names = FakeClient(), {}
    assert asyncio.run(channel.name_of(client, names, "U0SAM")) == "Sammy"
    assert asyncio.run(channel.name_of(client, names, "U0SAM")) == "Sammy"
    assert len(client.calls) == 1


def test_missing_tokens_name_the_setup_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    keyring = types.SimpleNamespace(get_password=lambda service, name: None)
    monkeypatch.setitem(sys.modules, "keyring", keyring)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO()))
    assert channel.main([]) == 2
    line = json.loads(capsys.readouterr().out)
    assert line["type"] == "log" and line["level"] == "error"
    assert "channel.py setup" in line["text"]
    assert channel.main(["check"]) == 2
    assert "channel.py setup" in capsys.readouterr().err


# The skill and the manifest


def test_skill_md() -> None:
    meta, body = skills.parse((FOLDER / "SKILL.md").read_text(encoding="utf-8"))
    assert meta["name"] == "slack"
    assert meta["metadata"] == {"attache-channel": "channel.py"}
    assert (FOLDER / meta["metadata"]["attache-channel"]).is_file()
    assert "attache-roles" not in meta["metadata"]  # setup is for the owner
    for step in ("attache-slack.yml", "connections:write", "channel.py setup", "check"):
        assert step in body
    assert "[channels.slack]" in body and "[owner.ids]" in body and '[chats."slack:' in body


def test_pep_723_header() -> None:
    source = (FOLDER / "channel.py").read_text(encoding="utf-8")
    block = re.search(r"(?m)^# /// script\n((?:#.*\n)+?)^# ///$", source)
    assert block
    header = tomllib.loads("\n".join(line[2:] for line in block[1].splitlines()))
    assert header["requires-python"] == ">=3.11"
    assert header["dependencies"] == ["slack-bolt>=1.30", "aiohttp>=3.9", "keyring>=25"]


def test_manifest() -> None:
    manifest = yaml.safe_load((FOLDER / "attache-slack.yml").read_text(encoding="utf-8"))
    assert manifest["display_information"]["name"] == "Attaché"
    features = manifest["features"]
    assert features["bot_user"]["always_online"] is True
    assert re.fullmatch(r"[a-z0-9._-]+", features["bot_user"]["display_name"])
    assert features["app_home"]["messages_tab_enabled"] is True
    assert features["app_home"]["messages_tab_read_only_enabled"] is False
    scopes = set(manifest["oauth_config"]["scopes"]["bot"])
    assert scopes >= {
        "app_mentions:read",
        "chat:write",
        "im:history",
        "im:read",
        "im:write",
        "channels:history",
        "groups:history",
        "mpim:history",
        "files:read",
        "files:write",
        "users:read",
        "reactions:write",
    }
    settings = manifest["settings"]
    events = set(settings["event_subscriptions"]["bot_events"])
    assert events >= {"app_mention", "message.im", "message.channels", "message.groups"}
    assert "message.mpim" in events
    assert settings["interactivity"]["is_enabled"] is True
    assert settings["socket_mode_enabled"] is True
