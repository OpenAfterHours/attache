"""A stand-in for signal-cli, enough for the Signal channel's tests. No Java, no network.

    fake_signal_cli.py --version                        prints a version
    fake_signal_cli.py -o json listAccounts             lists the bot's account
    fake_signal_cli.py -a +447700900123 jsonRpc         speaks JSON-RPC lines on stdin/stdout

In jsonRpc mode it answers `version` and then delivers a direct message (with an attachment), a
group message that mentions the bot, and envelopes the sidecar must ignore. It answers `send`
with a timestamp, and when the text asks for a reaction it reacts 👍 as the owner. Every argv
and request is appended to $FAKE_SIGNAL_LOG. With $FAKE_SIGNAL_DIE set it exits with code 3
right after the first deliveries.
"""

from __future__ import annotations

import json
import os
import sys

NUMBER = "+447700900123"
BOT = "11111111-1111-4111-8111-111111111111"
OWNER = "22222222-2222-4222-8222-222222222222"
SAM = "33333333-3333-4333-8333-333333333333"
GROUP = "Z3JvdXBpZA=="


def record(entry: dict) -> None:
    if path := os.environ.get("FAKE_SIGNAL_LOG"):
        with open(path, "a", encoding="utf-8") as log:
            log.write(json.dumps(entry, ensure_ascii=False) + "\n")


def write(obj: dict) -> None:
    sys.stdout.buffer.write(json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def deliver(envelope: dict) -> None:
    write(
        {"jsonrpc": "2.0", "method": "receive", "params": {"envelope": envelope, "account": NUMBER}}
    )


def first_deliveries() -> None:
    deliver(
        {
            "source": OWNER,
            "sourceNumber": None,
            "sourceUuid": OWNER,
            "sourceName": "Alex",
            "sourceDevice": 1,
            "timestamp": 1729000000001,
            "dataMessage": {
                "timestamp": 1729000000001,
                "message": "Pack my bag ☀️",
                "attachments": [
                    {
                        "id": "abc123.txt",
                        "contentType": "text/plain",
                        "filename": "list.txt",
                        "size": 12,
                    }
                ],
            },
        }
    )
    deliver(
        {
            "sourceUuid": SAM,
            "sourceName": "Sam",
            "timestamp": 1729000000002,
            "dataMessage": {
                "timestamp": 1729000000002,
                "message": "🙂 ￼ what time is the ferry?",
                "mentions": [{"name": BOT, "number": NUMBER, "uuid": BOT, "start": 3, "length": 1}],
                "groupInfo": {
                    "groupId": GROUP,
                    "groupName": "Family",
                    "revision": 3,
                    "type": "DELIVER",
                },
            },
        }
    )
    deliver(
        {
            "sourceUuid": OWNER,
            "timestamp": 1729000000003,
            "receiptMessage": {"when": 1729000000003, "isDelivery": True, "timestamps": [1]},
        }
    )
    deliver(
        {
            "sourceUuid": BOT,
            "timestamp": 1729000000004,
            "syncMessage": {"sentMessage": {"message": "hi", "timestamp": 1729000000004}},
        }
    )


def json_rpc() -> int:
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
            write(
                {
                    "jsonrpc": "2.0",
                    "result": {"timestamp": sent, "results": []},
                    "id": request["id"],
                }
            )
            if "React 👍" in request["params"].get("message", ""):
                reaction = {
                    "emoji": "👍",
                    "targetAuthorUuid": BOT,
                    "targetSentTimestamp": sent,
                    "isRemove": False,
                }
                deliver(
                    {
                        "sourceUuid": OWNER,
                        "sourceName": "Alex",
                        "timestamp": sent + 5,
                        "dataMessage": {"timestamp": sent + 5, "reaction": reaction},
                    }
                )
        else:
            error = {"code": -32601, "message": f"Method not implemented: {request['method']}"}
            write({"jsonrpc": "2.0", "error": error, "id": request.get("id")})
    return 0


def main(argv: list[str]) -> int:
    record({"argv": argv})
    if "--version" in argv:
        print("signal-cli 0.14.9")
    elif "listAccounts" in argv:
        print(json.dumps([{"number": NUMBER, "aci": BOT}]))
    elif "jsonRpc" in argv:
        return json_rpc()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
