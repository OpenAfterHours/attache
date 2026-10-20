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
from collections.abc import AsyncIterator
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


def inbound(e: dict[str, Any], seen: dict[Any, Any] | None = None) -> dict[str, Any] | None:
    return channel.inbound(e, SELF, NAMES, {} if seen is None else seen)


# Inbound messages


def test_a_direct_message() -> None:
    dm = event(channel="D0SAM", channel_type="im", text="what time is the ferry?")
    assert inbound(dm) == {
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
    msg = inbound(event(text="<@U0BOT> is 3 &lt; 4 &amp; 5?"))
    assert msg and msg["is_group"] and msg["mentioned"]
    assert msg["text"] == "is 3 < 4 & 5?"
    labelled = inbound(event(text="hi <@U0BOT|attache>"))
    assert labelled and labelled["mentioned"] and labelled["text"] == "hi"


def test_other_mentions_and_plain_channel_messages() -> None:
    msg = inbound(event(text="ask <@U0SAM> later"))
    assert msg and not msg["mentioned"] and msg["text"] == "ask <@U0SAM> later"


def test_a_reply_in_the_bots_thread_counts_as_a_mention() -> None:
    msg = inbound(event(ts="1729.50", thread_ts="1729.40", parent_user_id=SELF, text="Sunday?"))
    assert msg and msg["mentioned"]


def test_an_app_mention_has_no_channel_type_but_is_a_group() -> None:
    mention = event(type="app_mention", text="<@U0BOT> hi")
    del mention["channel_type"]
    msg = inbound(mention)
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
    assert [inbound(e) for e in ignored] == [None] * len(ignored)
    assert inbound(event(subtype="thread_broadcast"))


def test_a_mention_delivered_twice_is_one_message() -> None:
    seen: dict[Any, Any] = {}
    assert inbound(event(type="app_mention", text="<@U0BOT> hi"), seen)
    assert inbound(event(text="<@U0BOT> hi"), seen) is None
    assert inbound(event(ts="1729.43"), seen)  # a new message
    assert inbound(event(channel="C0OTHER"), seen)  # the same ts in another chat


def test_seen_messages_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(channel, "KEEP", 3)
    seen: dict[Any, Any] = {}
    for i in range(5):
        inbound(event(ts=f"1.{i}"), seen)
    assert list(seen) == [("C0TEAM", "1.2"), ("C0TEAM", "1.3"), ("C0TEAM", "1.4")]


def test_a_file_share() -> None:
    file = {"id": "F0PDF", "name": "menu.pdf", "url_private_download": "https://files.slack.com/x"}
    msg = inbound(event(subtype="file_share", text="", files=[file]))
    assert msg and msg["text"] == "" and msg["files"] == []  # run() downloads, then fills it


def test_unknown_senders_fall_back_to_their_id() -> None:
    msg = inbound(event(user="U0NEW"))
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


# Downloads


class FakeResponse:
    def __init__(self, status: int = 200, kind: str = "application/pdf", chunks: Any = ()) -> None:
        self.status, self.content_type, self.chunks = status, kind, chunks
        self.content = types.SimpleNamespace(iter_chunked=lambda size: self.stream())

    async def stream(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self.response, self.requests = response, []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.requests.append((url, kwargs))
        return self.response

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


PDF = {"id": "F1", "name": "menu.pdf", "mimetype": "application/pdf", "size": 8}
URL = "https://files.slack.com/files-pri/T0-F1/download/menu.pdf"


def fetch(
    tmp_path: Path, response: FakeResponse, limit: int = 25 * 2**20, **file: Any
) -> tuple[Any, FakeSession]:
    session = FakeSession(response)
    found = asyncio.run(
        channel.fetch(session, {**PDF, "url_private_download": URL, **file}, tmp_path, limit)
    )
    return found, session


def test_a_file_downloads_without_following_redirects(tmp_path: Path) -> None:
    found, session = fetch(tmp_path, FakeResponse(chunks=[b"%PDF", b"-1.7"]))
    assert found == str(tmp_path / "F1-menu.pdf") and Path(found).read_bytes() == b"%PDF-1.7"
    assert session.requests == [(URL, {"allow_redirects": False})]


@pytest.mark.parametrize(
    "response",
    [FakeResponse(302), FakeResponse(404), FakeResponse(kind="text/html", chunks=[b"<html>"])],
    ids=["redirect", "missing", "sign-in page"],
)
def test_refused_downloads_leave_nothing(
    tmp_path: Path, response: FakeResponse, capsys: pytest.CaptureFixture[str]
) -> None:
    found, _ = fetch(tmp_path, response)
    assert found is None and not list(tmp_path.iterdir())
    assert "files:read" in json.loads(capsys.readouterr().out)["text"]


def test_an_html_file_is_still_a_file(tmp_path: Path) -> None:
    page = FakeResponse(kind="text/html", chunks=[b"<p>hi</p>"])
    found, _ = fetch(tmp_path, page, name="page.html", mimetype="text/html")
    assert found and Path(found).read_bytes() == b"<p>hi</p>"


def test_big_files_stop_at_the_limit(tmp_path: Path) -> None:
    found, session = fetch(tmp_path, FakeResponse(), limit=10, size=11)  # Slack says too big
    assert found is None and session.requests == []
    found, _ = fetch(tmp_path, FakeResponse(chunks=[b"x" * 8, b"x" * 8]), limit=10, size=None)
    assert found is None and not list(tmp_path.iterdir())  # the part already written is gone


def test_files_without_a_slack_link_are_skipped(tmp_path: Path) -> None:
    found, session = fetch(tmp_path, FakeResponse(), url_private_download="https://evil.example/")
    assert found is None and session.requests == []


def download(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, files: list[dict[str, Any]], chunks: Any
) -> tuple[list[str], FakeSession]:
    """channel.download() with a stand-in aiohttp, whose session serves `chunks` for any file."""
    session = FakeSession(FakeResponse(chunks=chunks))
    monkeypatch.setitem(
        sys.modules, "aiohttp", types.SimpleNamespace(ClientSession=lambda **kw: session)
    )
    monkeypatch.setenv("ATTACHE_FILES", str(tmp_path / "inbox"))
    files = [{**PDF, "id": f"F{i}", "url_private_download": URL, **f} for i, f in enumerate(files)]
    return asyncio.run(channel.download(files, "xoxb-test")), session


def test_a_message_brings_ten_files_at_most(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths, session = download(monkeypatch, tmp_path, [{}] * 12, [b"%PDF-1.7"])
    assert len(paths) == len(session.requests) == 10
    assert all(Path(p).is_absolute() and Path(p).parent == tmp_path / "inbox" for p in paths)
    assert "first 10 of 12 files" in json.loads(capsys.readouterr().out)["text"]


def test_a_message_brings_fifty_mb_at_most(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(channel, "MAX_FILE", 10)
    monkeypatch.setattr(channel, "MAX_TOTAL", 20)
    # Each file is 8 bytes. Two fit, leaving 4 bytes: the third declares its 8 bytes and is
    # never fetched; the fourth declares nothing and is stopped while streaming.
    files = [{}, {"size": None}, {}, {"size": None}]
    paths, session = download(monkeypatch, tmp_path, files, [b"x" * 8])
    assert [Path(p).name for p in paths] == ["F0-menu.pdf", "F1-menu.pdf"]
    assert len(session.requests) == 3  # the third declared its size, so it was never fetched
    assert sorted(p.name for p in (tmp_path / "inbox").iterdir()) == ["F0-menu.pdf", "F1-menu.pdf"]


# Threads


def test_replies_thread_in_channels_and_stay_inline_in_direct_messages() -> None:
    seen: dict[Any, Any] = {}
    for e in [
        event(ts="1.1"),  # top level in a channel: start a thread under it
        event(ts="1.3", thread_ts="1.2"),  # inside a thread: answer in that thread
        event(ts="2.1", channel="D0SAM", channel_type="im"),  # top level in a DM: inline
        event(ts="2.3", channel="D0SAM", channel_type="im", thread_ts="2.2"),
    ]:
        inbound(e, seen)
    assert channel.reply_thread("1.1", "C0TEAM", seen) == "1.1"
    assert channel.reply_thread("1.3", "C0TEAM", seen) == "1.2"
    assert channel.reply_thread("2.1", "D0SAM", seen) is None
    assert channel.reply_thread("2.3", "D0SAM", seen) == "2.2"
    assert channel.reply_thread(None, "C0TEAM", seen) is None
    assert channel.reply_thread("9.9", "C0TEAM", seen) == "9.9"  # from before a restart
    assert channel.reply_thread("9.9", "D0SAM", seen) is None


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


def test_a_long_question_is_split_into_sections() -> None:
    blocks = channel.ask_blocks("x" * 7000, ["OK"], "a1")
    assert [len(b["text"]["text"]) for b in blocks[:-1]] == [3000, 3000, 1000]


def press(user: str, choice: str, ask_id: str = "a19") -> dict[str, Any]:
    """A block_actions payload, as Slack sends it for a button on an ask."""
    blocks = channel.ask_blocks("Send it?", ["Approve", "Decline"], ask_id)
    return {
        "type": "block_actions",
        "user": {"id": user},
        "channel": {"id": "D0ALEXDM"},
        "message": {"ts": "1729.60", "text": "Send it?", "blocks": blocks},
        "actions": [{"value": json.dumps({"ask_id": ask_id, "choice": choice})}],
    }


def test_answer_from_action() -> None:
    assert channel.answer_from_action(press("U0ALEX", "Decline")) == {
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


def verdict(clicks: dict[Any, Any], user: str, accepted: bool) -> list[tuple[str, Any]]:
    client = FakeClient()
    command = {"type": "answered", "ask_id": "a19", "sender_id": user, "accepted": accepted}
    asyncio.run(channel.answered(client, command, clicks))
    return client.calls


def test_an_accepted_answer_shows_the_choice_of_whoever_made_it() -> None:
    clicks = {
        ("a19", "U0SAM"): press("U0SAM", "Approve"),
        ("a19", "U0ALEX"): press("U0ALEX", "Decline"),
    }
    [(method, kwargs)] = verdict(clicks, "U0ALEX", accepted=True)
    assert method == "chat_update"
    assert kwargs["channel"] == "D0ALEXDM" and kwargs["ts"] == "1729.60"
    assert kwargs["text"] == "Send it?"
    assert kwargs["blocks"][-1]["elements"][0]["text"] == "✓ Decline by <@U0ALEX>"
    assert list(clicks) == [("a19", "U0SAM")]


def test_a_refused_answer_is_told_only_to_who_pressed() -> None:
    clicks = {("a19", "U0SAM"): press("U0SAM", "Approve")}
    assert verdict(clicks, "U0SAM", accepted=False) == [
        ("chat_postEphemeral", {"channel": "D0ALEXDM", "user": "U0SAM", "text": channel.NOT_TAKEN})
    ]
    assert verdict(clicks, "U0SAM", accepted=True) == []  # nothing pending any more


# Commands


def test_parse_command() -> None:
    send = {"type": "send", "chat_id": "C0", "text": "hi", "files": [], "reply_to": None}
    assert channel.parse_command(json.dumps(send) + "\r\n") == send
    assert channel.parse_command('{"type": "stop"}') == {"type": "stop"}
    answered = '{"type": "answered", "ask_id": "a1", "sender_id": "U0", "accepted": true}'
    assert channel.parse_command(answered)
    unknown = ['{"type": "typing", "chat_id": "C0", "on": true}', '{"type": "dance"}']
    for line in ["", "not json", "[1, 2]", '"send"', '{"type": ["send"]}', *unknown]:
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


def test_broadcast_mentions_never_ping() -> None:
    text = "Sam says <!channel> and <!here|here>, ping <!subteam^S01|devs> and <@U0SAM>"
    expected = "Sam says @\u200dchannel and @\u200dhere, ping @subteam^S01 and <@U0SAM>"
    assert channel.quiet(text) == expected
    assert channel.quiet("@everyone look, @here!") == "@\u200deveryone look, @\u200dhere!"
    assert channel.quiet("mail me@here.com or @channels") == "mail me@here.com or @channels"
    zwj = "\N{ZERO WIDTH JOINER}"
    assert (
        channel.quiet("@Here, @CHANNEL, @EveryOne") == f"@{zwj}Here, @{zwj}CHANNEL, @{zwj}EveryOne"
    )
    assert channel.quiet("<!Here>") == f"@{zwj}Here"


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
    command = {"type": "send", "chat_id": "C0", "text": "**Yes** @here", "reply_to": "1.1"}
    asyncio.run(channel.send(client, command, {("C0", "1.1"): "1.0"}))
    assert client.calls == [
        (
            "chat_postMessage",
            {"channel": "C0", "thread_ts": "1.0", "markdown_text": "**Yes** @\u200dhere"},
        )
    ]


def test_send_uploads_files_with_the_text_as_comment(tmp_path: Path) -> None:
    report = tmp_path / "report.pdf"
    client = FakeClient()
    command = {"type": "send", "chat_id": "D0", "text": "Here", "files": [str(report)]}
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


# A sidecar that can't run says why and waits for stop, so Attaché doesn't restart it.


def stdin(monkeypatch: pytest.MonkeyPatch, text: str) -> io.TextIOWrapper:
    stream = io.TextIOWrapper(io.BytesIO(text.encode("utf-8")), encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", stream)
    return stream


def keyring(monkeypatch: pytest.MonkeyPatch, get_password: Any) -> None:
    monkeypatch.setitem(sys.modules, "keyring", types.SimpleNamespace(get_password=get_password))


def test_missing_tokens_are_explained_then_the_sidecar_waits_for_stop(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    keyring(monkeypatch, lambda service, name: None)
    rest = stdin(monkeypatch, 'noise\n{"type": "send", "chat_id": "C0"}\n{"type": "stop"}\nafter\n')
    assert channel.main([]) == 2
    assert rest.read() == "after\n"  # it read up to stop, and no further
    out = capsys.readouterr()
    assert "channel.py setup" in out.err and out.out == ""
    stdin(monkeypatch, "")
    assert channel.main([]) == 2  # the end of stdin counts as stop
    assert channel.main(["check"]) == 2  # check never waits
    assert "channel.py setup" in capsys.readouterr().err


def test_an_unreadable_keychain_is_explained(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def locked(service: str, name: str) -> str:
        raise RuntimeError("no backend")

    keyring(monkeypatch, locked)
    stdin(monkeypatch, '{"type": "stop"}\n')
    assert channel.main([]) == 2
    assert "cannot read the keychain: no backend" in capsys.readouterr().err


class FakeSlackApiError(Exception):
    def __init__(self, error: str) -> None:
        super().__init__(error)
        self.response = {"error": error}


@pytest.mark.parametrize(("error", "code"), [("invalid_auth", 2), ("ratelimited", 1)])
def test_refused_tokens_wait_but_other_errors_exit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], error: str, code: int
) -> None:
    keyring(monkeypatch, lambda service, name: f"{name}-value")
    errors = types.SimpleNamespace(SlackApiError=FakeSlackApiError)
    monkeypatch.setitem(sys.modules, "slack_sdk.errors", errors)

    async def run(bot_token: str, app_token: str) -> int:
        raise FakeSlackApiError(error)

    monkeypatch.setattr(channel, "run", run)
    rest = stdin(monkeypatch, '{"type": "stop"}\n')
    assert channel.main([]) == code
    out = capsys.readouterr()
    if code == 2:
        assert "Slack refused a token (invalid_auth)" in out.err and rest.read() == ""
    else:  # Attaché restarts it later
        assert json.loads(out.out)["level"] == "error" and rest.read() == '{"type": "stop"}\n'


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
    assert 'role = "guest"' in body and "25 MB" in body


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
    assert set(manifest["oauth_config"]["scopes"]["bot"]) == {
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
    }
    settings = manifest["settings"]
    assert set(settings["event_subscriptions"]["bot_events"]) == {
        "app_mention",
        "message.im",
        "message.channels",
        "message.groups",
        "message.mpim",
    }
    assert settings["interactivity"]["is_enabled"] is True
    assert settings["socket_mode_enabled"] is True
