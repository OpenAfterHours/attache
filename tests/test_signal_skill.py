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
import time
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
P = "￼"  # Signal's placeholder for a mention

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


def in_group(text: str | None, **data: Any) -> dict[str, Any]:
    return dm(text, groupInfo={"groupId": GROUP, "type": "DELIVER"}, **data)


def test_skill_md_names_the_sidecar() -> None:
    meta, body = skills.parse((SKILL / "SKILL.md").read_text(encoding="utf-8"))
    assert meta["name"] == "signal"
    assert meta["metadata"] == {"attache-channel": "channel.py"}
    assert "when" in meta["description"].lower()
    assert "## Setup" in body and "signalcaptchas.org" in body and "--voice" in body
    assert "data_dir" not in body  # not a setting
    assert "listContacts --all-recipients" in body  # how to find a Signal id


def test_a_direct_message() -> None:
    assert channel.inbound(dm("Pack my bag ☀️"), BOT, {}) == {
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
    text = f"🙂 {P} ferry, ask {P}?"  # the emoji is two UTF-16 units
    mentions = [
        {"uuid": SAM, "number": "+447700900999", "start": 16, "length": 1},
        {"uuid": BOT, "number": None, "start": 3, "length": 1},
    ]
    message = channel.inbound(in_group(text, mentions=mentions), BOT, {SAM: "Sam"})
    assert message["text"] == "🙂 @Attaché ferry, ask @Sam?"
    assert message["mentioned"] and message["is_group"] and message["chat_id"] == GROUP


def test_other_mentions_never_show_a_number_or_uuid() -> None:
    mention = {"uuid": SAM, "number": "+447700900999", "name": "+447700900999"}
    envelope = dm(f"{P} hi", mentions=[{**mention, "start": 0, "length": 1}])
    message = channel.inbound(envelope, BOT, {})
    assert message["text"] == "@someone hi" and not message["mentioned"]


def test_a_reply_to_the_bot_counts_as_a_mention() -> None:
    envelope = dm("yes please", quote={"id": 5, "authorUuid": BOT, "text": "Shall I?"})
    assert channel.inbound(envelope, BOT, {})["mentioned"]
    reply = dm("yes", quote={"id": 5, "authorUuid": SAM})
    assert not channel.inbound(reply, BOT, {})["mentioned"]


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
    assert channel.inbound(envelope, BOT, {}) is None


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
    to_number = channel.send_params(
        {"chat_id": NUMBER, "text": "Approve?"}
    )  # approvals = "signal:+44…"
    assert to_number == {"recipient": [NUMBER], "message": "Approve?"}
    plus_group = "+bRmD3k2L0pQ9vX7cY1aZ5tW8uE6sN4hJ0gF2iK1oM="  # base64 can start with "+"
    assert channel.send_params({"chat_id": plus_group, "text": "hi"})["groupId"] == plus_group


def test_ask_text() -> None:
    assert channel.ask_text("Send the list?", ["Approve", "Decline"]) == (
        "Send the list?\n\nReact 👍 for Approve, 👎 for Decline, or reply with the option."
    )


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


def test_attachments_are_copied_under_safe_names_up_to_25_mb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store, inbox = tmp_path / "attachments", tmp_path / "inbox"
    store.mkdir()
    inbox.mkdir()
    (store / "abc123.pdf").write_bytes(b"%PDF")
    (store / "big.mp4").write_bytes(b"12345")
    monkeypatch.setattr(channel, "MAX_FILE", 4)
    items = [{"id": "abc123.pdf", "filename": "../../ev:il?.pdf"}, {"id": "missing.jpg"}]
    saved = channel.save_attachments(
        dm("see", attachments=[*items, {"id": "big.mp4"}]), store, inbox
    )
    assert len(saved) == 1
    path = Path(saved[0])
    assert path.is_absolute() and path.parent == inbox.resolve() and path.read_bytes() == b"%PDF"
    assert path.name == "1729000000001-0-ev_il_.pdf"
    logs = [json.loads(line)["text"] for line in capsys.readouterr().out.splitlines()]
    assert logs == [
        "skipped missing.jpg: missing, over 25 MB, or past 10 files/50 MB",
        "skipped big.mp4: missing, over 25 MB, or past 10 files/50 MB",
    ]


def test_a_message_brings_at_most_10_attachments_and_50_mb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (channel.MAX_FILES, channel.MAX_TOTAL) == (10, 50 * 1024 * 1024)  # as Slack's
    store, inbox = tmp_path / "attachments", tmp_path / "inbox"
    store.mkdir()
    inbox.mkdir()
    for name, size in (("a", 4), ("b", 4), ("c", 2), ("d", 1)):
        (store / name).write_bytes(b"x" * size)
    monkeypatch.setattr(channel, "MAX_TOTAL", 6)
    monkeypatch.setattr(channel, "MAX_FILES", 3)
    items = [{"id": name} for name in "abcd"]
    saved = channel.save_attachments(dm("photos", attachments=items), store, inbox)
    # b would pass 6 bytes in all; c still fits; d is the fourth file
    assert [Path(path).name for path in saved] == ["1729000000001-0-a", "1729000000001-2-c"]
    logs = [json.loads(line)["text"] for line in capsys.readouterr().out.splitlines()]
    assert [text.split(":")[0] for text in logs] == ["skipped b", "skipped d"]


def receive(bridge: Any, envelope: dict[str, Any]) -> None:
    bridge.on_line(
        json.dumps(
            {"jsonrpc": "2.0", "method": "receive", "params": {"envelope": envelope}}
        ).encode()
    )


def test_attachments_come_only_from_direct_messages_and_mentions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store, inbox = tmp_path / "attachments", tmp_path / "inbox"
    store.mkdir()
    inbox.mkdir()
    (store / "a.txt").write_text("x", encoding="utf-8")
    bridge = channel.Bridge(None, BOT, store, inbox)
    files = [{"id": "a.txt", "filename": "a.txt"}]
    receive(bridge, dm("direct", attachments=files))
    receive(bridge, in_group("to the group", attachments=files))
    mention = {"uuid": BOT, "start": 0, "length": 1}
    receive(bridge, in_group(f"{P} look", attachments=files, mentions=[mention]))
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(e["text"], len(e["files"])) for e in events] == [
        ("direct", 1),
        ("to the group", 0),
        ("@Attaché look", 1),
    ]
    assert bridge.names == {OWNER: "Alex"}  # learnt from the messages, for later mentions


# The sidecar against the fake signal-cli, through a launcher as real installs have one.


def launcher(folder: Path) -> str:
    """signal-cli.bat on Windows, or a shell script elsewhere, that runs the fake."""
    if os.name == "nt":
        path = folder / "signal-cli.bat"
        path.write_text(f'@"{sys.executable}" "{FAKE}" %*\n', encoding="utf-8")
    else:
        path = folder / "signal-cli"
        path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n', encoding="utf-8")
        path.chmod(0o755)
    return str(path)


def test_signal_cli_is_an_absolute_path_never_in_the_skill_or_working_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed, work, skill = tmp_path / "installed", tmp_path / "work", tmp_path / "skill"
    for folder in (installed, work, skill):
        folder.mkdir()
    real = str(Path(launcher(installed)).resolve())
    monkeypatch.delenv("ATTACHE_SIGNAL_CLI", raising=False)
    monkeypatch.setenv("PATH", str(installed))
    monkeypatch.chdir(work)
    monkeypatch.setattr(channel, "HERE", skill.resolve())
    assert channel.signal_cli() == real and Path(real).is_absolute()

    planted = str(Path(launcher(work)).resolve())  # where shutil.which looks first on Windows
    assert channel.signal_cli() != planted
    monkeypatch.setenv("NODEFAULTCURRENTDIRECTORYINEXEPATH", "1")  # as main() sets it
    if os.name != "nt" or sys.version_info >= (3, 12):  # 3.11's which ignores it
        assert channel.signal_cli() == real

    monkeypatch.setenv("PATH", os.pathsep.join([str(skill), str(installed)]))
    launcher(skill)
    assert channel.signal_cli() is None  # never the skill folder's, even first on PATH
    monkeypatch.setenv("ATTACHE_SIGNAL_CLI", str(skill / Path(real).name))
    assert channel.signal_cli() is None
    monkeypatch.setenv("ATTACHE_SIGNAL_CLI", real)
    assert channel.signal_cli() == real


class Sidecar:
    """channel.py running as Attaché runs it, with its stdout read on a thread."""

    def __init__(self, tmp_path: Path, **env: str) -> None:
        store = tmp_path / "xdg" / "signal-cli" / "attachments"
        store.mkdir(parents=True)
        (store / "abc123.txt").write_text("passport, charger", encoding="utf-8")
        self.inbox, self.requests = tmp_path / "inbox", tmp_path / "requests.jsonl"
        self.inbox.mkdir()  # as Attaché does
        full_env = {
            **{k: v for k, v in os.environ.items() if k != "ATTACHE_UUID"},
            "ATTACHE_NUMBER": NUMBER,
            "ATTACHE_SIGNAL_CLI": launcher(tmp_path),
            "ATTACHE_FILES": str(self.inbox),
            "XDG_DATA_HOME": str(tmp_path / "xdg"),
            "FAKE_SIGNAL_LOG": str(self.requests),
            **env,
        }
        pipe = subprocess.PIPE
        argv = [sys.executable, str(SKILL / "channel.py")]
        self.proc = subprocess.Popen(  # from the skill folder, as Attaché runs it
            argv, cwd=SKILL, stdin=pipe, stdout=pipe, stderr=pipe, env=full_env
        )
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

    def rest(self) -> list[dict[str, Any]]:
        """The events still to come, once it has exited."""
        events = []
        with contextlib.suppress(queue.Empty):
            while True:
                events.append(self.events.get(timeout=1))
        return events

    def write(self, command: dict[str, Any]) -> None:
        self.proc.stdin.write(json.dumps(command).encode() + b"\n")
        self.proc.stdin.flush()

    def stop(self) -> tuple[int, str]:
        """Say stop as Attaché does; the exit code and everything it wrote on stderr."""
        self.write({"type": "stop"})
        self.proc.stdin.close()
        code = self.proc.wait(timeout=30)
        return code, self.proc.stderr.read().decode("utf-8", errors="replace")

    def recorded(self) -> list[dict[str, Any]]:
        lines = self.requests.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines]


def test_the_sidecar_speaks_the_protocol(tmp_path: Path) -> None:
    side = Sidecar(tmp_path, JAVA_TOOL_OPTIONS="-Xmx64m")
    assert side.next("ready") == {"type": "ready", "self_id": BOT, "name": "Attaché"}

    direct = side.next("message")
    assert direct["chat_id"] == OWNER and direct["text"] == "Pack my bag ☀️"
    assert not direct["is_group"]
    [copied] = direct["files"]
    assert Path(copied).parent == side.inbox.resolve()
    assert Path(copied).read_text(encoding="utf-8") == "passport, charger"

    group = side.next("message")
    assert group["chat_id"] == GROUP and group["is_group"] and group["mentioned"]
    assert group["text"] == "🙂 @Attaché what time is the ferry, @Alex?"  # Alex wrote earlier
    unmentioned = side.next("message")
    assert unmentioned["text"] == "holiday photos" and unmentioned["files"] == []

    ask = {"type": "ask", "chat_id": OWNER, "ask_id": "a19", "text": "Share the list?"}
    side.write({**ask, "options": ["Approve", "Decline"]})
    answer = side.next("answer")
    assert answer == {"type": "answer", "ask_id": "a19", "sender_id": OWNER, "choice": "Approve"}
    side.write({"type": "answered", "ask_id": "a19", "sender_id": OWNER, "accepted": True})

    reply = {"type": "send", "chat_id": GROUP, "text": "09:40 from Portsmouth.", "files": []}
    side.write({**reply, "reply_to": f"1729000000002:{SAM}"})
    code, stderr = side.stop()
    assert code == 0, stderr
    assert side.rest() == []  # `answered` needs nothing, and is not logged as unknown

    runs = [(entry["argv"], entry["java"]) for entry in side.recorded() if "argv" in entry]
    assert runs == [
        (["-o", "json", "listAccounts"], "-Xmx64m"),
        (["-a", NUMBER, "jsonRpc"], "-Xmx64m -Dstdout.encoding=UTF-8"),  # Java's output: UTF-8
    ]
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


def alive(pid: int) -> bool:
    if os.name == "nt":
        found = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True)
        return str(pid).encode() in found.stdout.split()
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_stop_kills_signal_cli_and_its_children(tmp_path: Path) -> None:
    pids = tmp_path / "pids"
    side = Sidecar(tmp_path, FAKE_SIGNAL_HANG=str(pids))  # ignores the end of its input
    side.next("ready")
    code, stderr = side.stop()  # after 4 s, the whole tree
    assert code == 0, stderr
    started = [int(pid) for pid in pids.read_text(encoding="utf-8").split()]
    assert len(started) == 2  # the fake behind the launcher, and its own child
    for _ in range(50):  # killed processes can take a moment to go
        if not any(alive(pid) for pid in started):
            break
        time.sleep(0.1)
    assert not any(alive(pid) for pid in started)


def test_when_signal_cli_dies_the_sidecar_exits_non_zero(tmp_path: Path) -> None:
    side = Sidecar(tmp_path, FAKE_SIGNAL_DIE="1")
    side.next("ready")
    assert side.proc.wait(timeout=20) == 3  # its stdin is still open: Attaché did not stop it
    assert any(e["type"] == "log" and "exited with code 3" in e["text"] for e in side.rest())
    assert b"Fatal Python error" not in side.proc.stderr.read()


@pytest.mark.parametrize(
    ("env", "why"),
    [
        ({"ATTACHE_NUMBER": ""}, "number must look like +447700900123"),
        ({"ATTACHE_NUMBER": "+44 7700 900123"}, "number must look like"),
        ({"ATTACHE_NUMBER": "+447700900123 & calc"}, "number must look like"),  # cmd.exe parses it
        ({"ATTACHE_SIGNAL_CLI": "no-such-signal-cli"}, "signal-cli not found"),
        ({"FAKE_SIGNAL_REFUSE": "1"}, "could not start the account"),  # signal-cli exits 1
    ],
    ids=["no-number", "spaces", "injection", "no-signal-cli", "not-registered"],
)
def test_a_setup_problem_is_explained_and_then_it_waits(
    tmp_path: Path, env: dict[str, str], why: str
) -> None:
    side = Sidecar(tmp_path, **env)
    time.sleep(3)
    assert side.proc.poll() is None  # still up, so Attaché does not restart it every minute
    code, stderr = side.stop()
    assert code == 0 and why in stderr and "run doctor" in stderr
    assert not [e for e in side.rest() if e["type"] in ("ready", "message")]
    if "ATTACHE_NUMBER" in env:
        assert not side.requests.exists()  # signal-cli never ran
    if env.get("FAKE_SIGNAL_REFUSE"):
        assert "is not registered" in stderr  # signal-cli's own words come first


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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    real_which = shutil.which

    def no_java(name: str, *args: Any, **kwargs: Any) -> str | None:
        return None if name == "java" else real_which(name, *args, **kwargs)

    monkeypatch.setattr(channel.shutil, "which", no_java)
    monkeypatch.setenv("ATTACHE_SIGNAL_CLI", launcher(tmp_path))
    monkeypatch.setattr(channel, "release_date", lambda version: date.today() - timedelta(120))
    assert channel.doctor([NUMBER]) == 1  # java is missing
    out = capsys.readouterr().out
    assert "WARN  signal-cli 0.14.9" in out and "120 days old" in out
    assert f"ok    account {NUMBER} is registered; the bot's Signal id is {BOT}" in out

    assert channel.doctor(["07700 900123"]) == 1
    assert "'07700 900123' is not a number like +447700900123" in capsys.readouterr().out

    monkeypatch.setattr(channel, "release_date", lambda version: None)  # offline
    assert channel.doctor(["+447700900999"]) == 1
    out = capsys.readouterr().out
    assert "release date unknown" in out and "+447700900999 is not registered" in out
