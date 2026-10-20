"""A stand-in for signal-cli, enough for the Signal channel's tests. No Java, no network.

    fake_signal_cli.py --version                        prints a version
    fake_signal_cli.py -o json listAccounts             lists the bot's account
    fake_signal_cli.py -a +447700900123 jsonRpc         speaks JSON-RPC lines on stdin/stdout

In jsonRpc mode it answers `version` and then delivers a direct message from Alex (with an
attachment), a group message from Sam that mentions the bot and Alex, a group message with an
attachment that mentions nobody, and envelopes the sidecar must ignore. It answers `send` with a
timestamp, and when the text asks for a reaction it reacts 👍 as Alex. Every argv (with the
JAVA_TOOL_OPTIONS it was given) and request is appended to $FAKE_SIGNAL_LOG.

Failure modes, for the tests:
- $FAKE_SIGNAL_DIE: exit 3 (an IO error) right after the first deliveries.
- $FAKE_SIGNAL_REFUSE: the account is not registered: listAccounts lists nothing, and jsonRpc
  says so on stderr and exits 1, as signal-cli does.
- $FAKE_SIGNAL_HANG=<file>: start a child process, write both process ids to <file>, and keep
  running after stdin ends, like a Java process that will not quit.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

NUMBER = "+447700900123"
BOT = "11111111-1111-4111-8111-111111111111"
OWNER = "22222222-2222-4222-8222-222222222222"
SAM = "33333333-3333-4333-8333-333333333333"
GROUP = "Z3JvdXBpZA=="
P = "￼"  # what Signal puts in the text where a mention goes


def record(entry: dict) -> None:
    if path := os.environ.get("FAKE_SIGNAL_LOG"):
        with open(path, "a", encoding="utf-8") as log:
            log.write(json.dumps(entry, ensure_ascii=False) + "\n")


def write(obj: dict) -> None:
    sys.stdout.buffer.write(json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def deliver(envelope: dict) -> None:
    params = {"envelope": envelope, "account": NUMBER}
    write({"jsonrpc": "2.0", "method": "receive", "params": params})


def utf16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def group_message(timestamp: int, text: str, **data: object) -> dict:
    group = {"groupId": GROUP, "groupName": "Family", "revision": 3, "type": "DELIVER"}
    message = {"timestamp": timestamp, "message": text, "groupInfo": group, **data}
    return {"sourceUuid": SAM, "sourceName": "Sam", "timestamp": timestamp, "dataMessage": message}


def first_deliveries() -> None:
    attachment = {"id": "abc123.txt", "contentType": "text/plain", "filename": "list.txt"}
    message = {"timestamp": 1729000000001, "message": "Pack my bag ☀️", "attachments": [attachment]}
    deliver(
        {
            "source": OWNER,
            "sourceNumber": None,
            "sourceUuid": OWNER,
            "sourceName": "Alex",
            "sourceDevice": 1,
            "timestamp": 1729000000001,
            "dataMessage": message,
        }
    )
    text = f"🙂 {P} what time is the ferry, {P}?"
    alex = {"name": OWNER, "number": "+447700900999", "uuid": OWNER}  # numbers must not leak
    mentions = [
        {"name": BOT, "number": NUMBER, "uuid": BOT, "start": utf16("🙂 "), "length": 1},
        {**alex, "start": utf16(text[: text.rindex(P)]), "length": 1},
    ]
    deliver(group_message(1729000000002, text, mentions=mentions))
    deliver(group_message(1729000000003, "holiday photos", attachments=[attachment]))
    receipt = {"when": 1729000000004, "isDelivery": True, "timestamps": [1]}
    deliver({"sourceUuid": OWNER, "timestamp": 1729000000004, "receiptMessage": receipt})
    sync = {"sentMessage": {"message": "hi", "timestamp": 1729000000005}}
    deliver({"sourceUuid": BOT, "timestamp": 1729000000005, "syncMessage": sync})


def start_child(pids: str) -> None:
    """A grandchild for signal-cli's launcher, as Java is for signal-cli.bat."""
    with open(pids, "a", encoding="utf-8") as out:
        out.write(f"{os.getpid()}\n")
    code = "import os, sys, time; open(sys.argv[1], 'a').write(f'{os.getpid()}\\n'); time.sleep(60)"
    subprocess.Popen([sys.executable, "-c", code, pids])


def json_rpc() -> int:
    if os.environ.get("FAKE_SIGNAL_REFUSE"):
        print(f"User {NUMBER} is not registered.", file=sys.stderr)
        return 1
    if pids := os.environ.get("FAKE_SIGNAL_HANG"):
        start_child(pids)
    sent = 1729000001000
    for line in sys.stdin.buffer:
        request = json.loads(line)
        record(request)
        if request["method"] == "version":
            write({"jsonrpc": "2.0", "result": {"version": "0.14.9"}, "id": request["id"]})
            first_deliveries()
            if os.environ.get("FAKE_SIGNAL_DIE"):
                return 3
        elif request["method"] == "send":
            sent += 1
            result = {"timestamp": sent, "results": []}
            write({"jsonrpc": "2.0", "result": result, "id": request["id"]})
            if "React 👍" in request["params"].get("message", ""):
                reaction = {"emoji": "👍", "targetAuthorUuid": BOT, "targetSentTimestamp": sent}
                data = {"timestamp": sent + 5, "reaction": {**reaction, "isRemove": False}}
                deliver({"sourceUuid": OWNER, "timestamp": sent + 5, "dataMessage": data})
        else:
            error = {"code": -32601, "message": f"Method not implemented: {request['method']}"}
            write({"jsonrpc": "2.0", "error": error, "id": request.get("id")})
    if os.environ.get("FAKE_SIGNAL_HANG"):
        time.sleep(60)
    return 0


def main(argv: list[str]) -> int:
    record({"argv": argv, "java": os.environ.get("JAVA_TOOL_OPTIONS")})
    if "--version" in argv:
        print("signal-cli 0.14.9")
    elif "listAccounts" in argv:
        refused = os.environ.get("FAKE_SIGNAL_REFUSE")
        print(json.dumps([] if refused else [{"number": NUMBER, "aci": BOT}]))
    elif "jsonRpc" in argv:
        return json_rpc()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
