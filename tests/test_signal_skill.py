"""The Signal channel skill: its translation functions, the sidecar run against a fake
signal-cli (tests/fixtures/fake_signal_cli.py), and doctor. No Java, no signal-cli, no network.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from attache import skills

SKILL = skills.BUNDLED / "signal"
FAKE = Path(__file__).parent / "fixtures" / "fake_signal_cli.py"
NUMBER = "+447700900123"
BOT = "11111111-1111-4111-8111-111111111111"
OWNER = "22222222-2222-4222-8222-222222222222"
SAM = "33333333-3333-4333-8333-333333333333"
GROUP = "Z3JvdXBpZA=="

_spec = importlib.util.spec_from_file_location("signal_channel", SKILL / "channel.py")
channel = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = channel  # dataclasses look their module up while the file runs
_spec.loader.exec_module(channel)


def dm(text: str | None = "hi", **data: Any) -> dict[str, Any]:
    """A signal-cli envelope for a direct message from the owner."""
    message = {"timestamp": 1729000000001, "message": text, **data}
    return {
        "sourceUuid": OWNER,
        "sourceName": "Alex",
        "timestamp": 1729000000001,
        "sourceNumber": None,
        "dataMessage": message,
    }


def test_skill_md_names_the_sidecar() -> None:
    meta, body = skills.parse((SKILL / "SKILL.md").read_text(encoding="utf-8"))
    assert meta["name"] == "signal"
    assert meta["metadata"] == {"attache-channel": "channel.py"}
    assert "when" in meta["description"].lower()
    assert "## Setup" in body and "signalcaptchas.org" in body and "--voice" in body


def test_a_direct_message() -> None:
    assert channel.inbound(dm("Pack my bag ☀️"), BOT) == {
        "type": "message",
        "chat_id": OWNER,
        "message_id": f"1729000000001:{OWNER}",
        "sender_id": OWNER,
        "sender_name": "Alex",
        "text": "Pack my bag ☀️",
        "is_group": False,
        "mentioned": False,
        "files": [],
    }


def test_a_group_mention_names_the_bot_counting_utf16() -> None:
    text = "🙂 ￼ ferry, ask ￼?"  # the emoji is two UTF-16 units
    mentions = [
        {"uuid": SAM, "number": "+447700900999", "start": 16, "length": 1},
        {"uuid": BOT, "number": None, "start": 3, "length": 1},
    ]
    envelope = dm(text, mentions=mentions, groupInfo={"groupId": GROUP, "type": "DELIVER"})
    message = channel.inbound(envelope, BOT)
    assert message["text"] == "🙂 @Attaché ferry, ask @+447700900999?"
    assert message["mentioned"] and message["is_group"] and message["chat_id"] == GROUP


def test_someone_elses_mention_is_not_the_bot() -> None:
    envelope = dm("￼ hi", mentions=[{"uuid": SAM, "start": 0, "length": 1}])
    message = channel.inbound(envelope, BOT)
    assert message["text"] == f"@{SAM} hi" and not message["mentioned"]


def test_a_reply_to_the_bot_counts_as_a_mention() -> None:
    envelope = dm("yes please", quote={"id": 5, "authorUuid": BOT, "text": "Shall I?"})
    assert channel.inbound(envelope, BOT)["mentioned"]
    assert not channel.inbound(dm("yes", quote={"id": 5, "authorUuid": SAM}), BOT)["mentioned"]


@pytest.mark.parametrize(
    "envelope",
    [
        {"sourceUuid": OWNER, "timestamp": 1, "receiptMessage": {"isDelivery": True}},
        {"sourceUuid": BOT, "timestamp": 1, "syncMessage": {"sentMessage": {"message": "hi"}}},
        {"sourceUuid": OWNER, "timestamp": 1, "typingMessage": {"action": "STARTED"}},
        {**dm("from myself"), "sourceUuid": BOT},
        {**dm("no sender"), "sourceUuid": None},
        dm(None, reaction={"emoji": "👍", "targetSentTimestamp": 1, "isRemove": False}),
        dm(None, groupInfo={"groupId": GROUP, "type": "UPDATE"}),
        dm(""),
    ],
    ids=["receipt", "sync", "typing", "own", "no-sender", "reaction", "group-update", "empty"],
)
def test_envelopes_that_are_not_messages_are_skipped(envelope: dict[str, Any]) -> None:
    assert channel.inbound(envelope, BOT) is None


def test_send_params() -> None:
    assert channel.send_params({"chat_id": OWNER, "text": "Done.", "files": []}) == {
        "recipient": [OWNER],
        "message": "Done.",
    }
    reply = {"chat_id": GROUP, "text": "09:40", "files": ["/tmp/a.pdf"], "reply_to": f"17:{SAM}"}
    assert channel.send_params(reply) == {
        "groupId": GROUP,
        "message": "09:40",
        "attachments": ["/tmp/a.pdf"],
        "quoteTimestamp": 17,
        "quoteAuthor": SAM,
    }
    assert channel.send_params({"chat_id": NUMBER, "text": "x", "reply_to": None}) == {
        "recipient": [NUMBER],
        "message": "x",
    }


def test_ask_text() -> None:
    assert channel.ask_text("Send the list?", ["Approve", "Decline"]) == (
        "Send the list?\n\nReact 👍 for Approve, 👎 for Decline, or reply with the option."
    )
    three = channel.ask_text("Which?", ["Red", "Green", "Blue"])
    assert "React 👍 for Red, 👎 for Green" in three and "1 Red, 2 Green, 3 Blue" in three


def reaction(emoji: str, target: int = 42, remove: bool = False) -> dict[str, Any]:
    react = {"emoji": emoji, "targetAuthorUuid": BOT, "targetSentTimestamp": target}
    return dm(None, reaction={**react, "isRemove": remove})


def test_answers_from_reactions() -> None:
    asks = {42: ("a19", ["Approve", "Decline"]), 43: ("a20", ["OK"])}
    answer = channel.answer_from_reaction(reaction("👍"), asks)
    assert answer == {"type": "answer", "ask_id": "a19", "sender_id": OWNER, "choice": "Approve"}
    assert channel.answer_from_reaction(reaction("👎🏽"), asks)["choice"] == "Decline"  # skin tone
    assert channel.answer_from_reaction(reaction("👍", remove=True), asks) is None
    assert channel.answer_from_reaction(reaction("❤️"), asks) is None
    assert channel.answer_from_reaction(reaction("👍", target=7), asks) is None
    assert channel.answer_from_reaction(reaction("👎", target=43), asks) is None  # no 2nd option
    assert channel.answer_from_reaction(dm("plain text"), asks) is None


def test_attachments_are_copied_under_safe_names(tmp_path: Path) -> None:
    store, inbox = tmp_path / "attachments", tmp_path / "inbox"
    store.mkdir()
    (store / "abc123.pdf").write_bytes(b"%PDF")
    items = [{"id": "abc123.pdf", "filename": "../../ev:il?.pdf"}, {"id": "missing.jpg"}]
    saved = channel.save_attachments(dm("see", attachments=items), store, inbox)
    assert len(saved) == 1
    path = Path(saved[0])
    assert path.is_absolute() and path.parent == inbox.resolve() and path.read_bytes() == b"%PDF"
    assert path.name == "1729000000001-0-ev_il_.pdf"


# The sidecar against the fake signal-cli.


class Sidecar:
    """channel.py running as Attaché runs it, with its stdout read on a thread."""

    def __init__(self, tmp_path: Path, **env: str) -> None:
        store = tmp_path / "xdg" / "signal-cli" / "attachments"
        store.mkdir(parents=True)
        (store / "abc123.txt").write_text("passport, charger", encoding="utf-8")
        self.inbox, self.requests = tmp_path / "inbox", tmp_path / "requests.jsonl"
        full_env = {
            **{k: v for k, v in os.environ.items() if k != "ATTACHE_UUID"},
            "ATTACHE_NUMBER": NUMBER,
            "ATTACHE_SIGNAL_CLI": json.dumps([sys.executable, str(FAKE)]),
            "ATTACHE_FILES": str(self.inbox),
            "XDG_DATA_HOME": str(tmp_path / "xdg"),
            "FAKE_SIGNAL_LOG": str(self.requests),
            **env,
        }
        pipe = subprocess.PIPE
        argv = [sys.executable, str(SKILL / "channel.py")]
        self.proc = subprocess.Popen(argv, stdin=pipe, stdout=pipe, stderr=pipe, env=full_env)
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in self.proc.stdout:
            self.events.put(json.loads(line.decode("utf-8")))

    def next(self, kind: str) -> dict[str, Any]:
        """The next event of this type, skipping logs."""
        while (event := self.events.get(timeout=20))["type"] != kind:
            assert event["type"] == "log", event
        return event

    def write(self, command: dict[str, Any]) -> None:
        self.proc.stdin.write(json.dumps(command).encode() + b"\n")
        self.proc.stdin.flush()

    def recorded(self) -> list[dict[str, Any]]:
        lines = self.requests.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines]


def test_the_sidecar_speaks_the_protocol(tmp_path: Path) -> None:
    side = Sidecar(tmp_path)
    assert side.next("ready") == {"type": "ready", "self_id": BOT, "name": "Attaché"}

    direct = side.next("message")
    assert direct["chat_id"] == OWNER and direct["text"] == "Pack my bag ☀️"
    assert not direct["is_group"]
    [copied] = direct["files"]
    assert Path(copied).parent == side.inbox.resolve()
    assert Path(copied).read_text(encoding="utf-8") == "passport, charger"

    group = side.next("message")
    assert group["chat_id"] == GROUP and group["is_group"] and group["mentioned"]
    assert group["text"] == "🙂 @Attaché what time is the ferry?"

    ask = {"type": "ask", "chat_id": OWNER, "ask_id": "a19", "text": "Share the list?"}
    side.write({**ask, "options": ["Approve", "Decline"]})
    answer = side.next("answer")
    assert answer == {"type": "answer", "ask_id": "a19", "sender_id": OWNER, "choice": "Approve"}

    side.write({"type": "typing", "chat_id": GROUP, "on": True})
    reply = {"type": "send", "chat_id": GROUP, "text": "09:40 from Portsmouth.", "files": []}
    side.write({**reply, "reply_to": f"1729000000002:{SAM}"})
    side.write({"type": "stop"})
    side.proc.stdin.close()
    assert side.proc.wait(timeout=20) == 0, side.proc.stderr.read().decode()

    argvs = [entry["argv"] for entry in side.recorded() if "argv" in entry]
    assert argvs == [["-o", "json", "listAccounts"], ["-a", NUMBER, "jsonRpc"]]
    requests = [entry for entry in side.recorded() if "method" in entry]
    assert [r["method"] for r in requests] == ["version", "send", "send"]
    asked, replied = requests[1]["params"], requests[2]["params"]
    assert asked["recipient"] == [OWNER] and asked["message"].startswith("Share the list?\n\n")
    assert "React 👍 for Approve, 👎 for Decline" in asked["message"]
    assert replied == {
        "groupId": GROUP,
        "message": "09:40 from Portsmouth.",
        "quoteTimestamp": 1729000000002,
        "quoteAuthor": SAM,
    }


def test_the_bot_id_can_be_set(tmp_path: Path) -> None:
    side = Sidecar(tmp_path, ATTACHE_UUID="44444444-4444-4444-8444-444444444444")
    assert side.next("ready")["self_id"] == "44444444-4444-4444-8444-444444444444"
    assert not side.next("message")["mentioned"]
    side.proc.stdin.close()  # the end of stdin also stops it
    assert side.proc.wait(timeout=20) == 0
    assert not any(entry.get("argv", [""])[-1] == "listAccounts" for entry in side.recorded())


def test_when_signal_cli_dies_the_sidecar_exits_non_zero(tmp_path: Path) -> None:
    side = Sidecar(tmp_path, FAKE_SIGNAL_DIE="1")
    side.next("ready")
    assert side.proc.wait(timeout=20) == 3  # its stdin is still open: Attaché did not stop it
    events = []
    with contextlib.suppress(queue.Empty):
        while True:
            events.append(side.events.get(timeout=2))
    assert any(e["type"] == "log" and "exited with code 3" in e["text"] for e in events)
    assert b"Fatal Python error" not in side.proc.stderr.read()


def test_a_missing_number_exits_2(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if k != "ATTACHE_NUMBER"}
    done = subprocess.run(
        [sys.executable, str(SKILL / "channel.py")], capture_output=True, env=env, timeout=30
    )
    assert done.returncode == 2
    event = json.loads(done.stdout.decode("utf-8").splitlines()[0])
    assert event["type"] == "log" and event["level"] == "error" and "number" in event["text"]


# doctor.


def test_doctor_says_what_is_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ATTACHE_SIGNAL_CLI", raising=False)
    monkeypatch.setattr(channel.shutil, "which", lambda *args, **kwargs: None)
    assert channel.doctor([NUMBER]) == 1
    out = capsys.readouterr().out
    assert "FAIL  java: not found on PATH" in out and "winget install" in out
    assert "FAIL  signal-cli: not found" in out


def test_doctor_checks_version_age_and_account(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    real_which = shutil.which

    def no_java(name: str, *args: Any, **kwargs: Any) -> str | None:
        return None if name == "java" else real_which(name, *args, **kwargs)

    monkeypatch.setattr(channel.shutil, "which", no_java)
    monkeypatch.setenv("ATTACHE_SIGNAL_CLI", json.dumps([sys.executable, str(FAKE)]))
    monkeypatch.setattr(channel, "release_date", lambda version: date.today() - timedelta(120))
    assert channel.doctor([NUMBER]) == 1  # java is missing
    out = capsys.readouterr().out
    assert "WARN  signal-cli 0.14.9" in out and "120 days old" in out
    assert f"ok    account {NUMBER} is registered; the bot's Signal id is {BOT}" in out

    monkeypatch.setattr(channel, "release_date", lambda version: None)  # offline
    assert channel.doctor(["+44 7700 900999"]) == 1
    out = capsys.readouterr().out
    assert "release date unknown" in out and "+447700900999 is not registered" in out
