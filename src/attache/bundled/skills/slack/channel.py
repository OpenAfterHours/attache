# /// script
# requires-python = ">=3.11"
# dependencies = ["slack-bolt>=1.30", "aiohttp>=3.9", "keyring>=25"]
# ///
"""Slack channel for Attaché: a sidecar that speaks JSON lines on stdin and stdout.

The protocol is in docs/channels.md. It connects over Socket Mode, so Slack needs no public URL.
The tokens live in the OS keychain under the service "attache-slack":

    uv run channel.py setup            store the bot (xoxb-) and app-level (xapp-) tokens
    uv run channel.py check [U...]     test them; with your member id, print your DM's chat key
    uv run channel.py                  run the sidecar (Attaché does this)

The protocol translation is plain functions and slack-bolt is imported only inside run() and
check(), so tests can import this file without slack-bolt installed.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import re
import sys
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

SERVICE = "attache-slack"  # keychain service; usernames bot_token and app_token
SETUP = "uv run ~/.attache/skills/slack/channel.py setup"
PLAIN = {None, "file_share", "thread_broadcast"}  # message subtypes a person typed
COMMANDS = ("send", "ask", "typing", "stop")
KEEP = 1000  # message ids remembered for dedupe and threading
SPECIAL = re.compile(r"<!([^>|]+)(?:\|[^>]*)?>")  # <!channel>, <!here>, <!subteam^S01>
Json = dict[str, Any]


def emit(obj: Json) -> None:
    """Write one protocol line to stdout."""
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def log(level: str, text: str) -> None:
    """Send a line for Attaché's log."""
    emit({"type": "log", "level": level, "text": text})


def remember(store: dict[Any, Any], key: Any, value: Any = None) -> None:
    """Keep key in a dict used as a bounded memory, dropping the oldest entries."""
    store[key] = value
    while len(store) > KEEP:
        del store[next(iter(store))]


def escape(text: str) -> str:
    """Slack mrkdwn needs &, < and > escaped."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def unescape(text: str) -> str:
    """Undo Slack's escaping of incoming text."""
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def quiet(text: str) -> str:
    """Turn @channel, @here and group pings into plain words, so a reply never notifies a
    whole channel because a message it quoted asked for that."""
    return SPECIAL.sub(r"@\1", text)


def inbound(event: Json, self_id: str, names: dict, seen: dict | None = None) -> Json | None:
    """A Slack `message` or `app_mention` event as a protocol "message", or None to ignore it.

    Ignored: bots (this one included), edits, joins and other subtypes, and, when `seen` is
    given, repeats: Slack delivers a channel mention twice, as app_mention and as message.*.
    `names` maps user ids to display names; `files` is left for the caller to download.
    """
    user = event.get("user")
    if event.get("bot_id") or not user or user == self_id or event.get("subtype") not in PLAIN:
        return None
    key = (event.get("channel"), event.get("ts"))
    if seen is not None:
        if key in seen:
            return None
        remember(seen, key)
    mention = re.compile(rf"<@{re.escape(self_id)}(?:\|[^>]*)?>")
    raw = event.get("text") or ""
    text = unescape(mention.sub("", raw)).strip()
    if not text and not event.get("files"):
        return None
    return {
        "type": "message",
        "chat_id": key[0],
        "message_id": key[1],
        "sender_id": user,
        "sender_name": names.get(user, user),
        "text": text,
        "is_group": event.get("channel_type") != "im",  # app_mention has none: it's a channel
        "mentioned": bool(mention.search(raw)) or event.get("parent_user_id") == self_id,
        "files": [],
    }


def thread_root(event: Json) -> str | None:
    """Where a reply to this message goes: its thread, a new thread under it in a channel, or
    the top level of a direct message."""
    if event.get("thread_ts"):
        return event["thread_ts"]
    return None if event.get("channel_type") == "im" else event.get("ts")


def reply_thread(reply_to: str | None, chat_id: str, roots: dict[str, str | None]) -> str | None:
    """The thread_ts for a reply. Slack wants the thread's parent, not a reply inside it."""
    if reply_to in roots:
        return roots[reply_to]
    return None if not reply_to or chat_id.startswith("D") else reply_to  # D...: a DM


def ask_blocks(text: str, options: list[str], ask_id: str) -> list[Json]:
    """Block Kit for an "ask": the question, then one button per option."""
    text = escape(text) or "?"
    sections = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text[i : i + 3000]}}
        for i in range(0, len(text), 3000)  # a section holds 3,000 characters
    ]
    buttons = [
        {
            "type": "button",
            "action_id": f"attache_answer_{i}",
            "text": {"type": "plain_text", "text": option[:75]},
            "value": json.dumps({"ask_id": ask_id, "choice": option}, ensure_ascii=False),
        }
        for i, option in enumerate(options[:25])
    ]
    if buttons:
        buttons[0]["style"] = "primary"
    return [*sections, {"type": "actions", "elements": buttons}]


def answer_from_action(body: Json) -> Json:
    """A button press on an ask as a protocol "answer". Attaché checks who may answer."""
    value = json.loads(body["actions"][0]["value"])
    return {
        "type": "answer",
        "ask_id": value["ask_id"],
        "sender_id": body["user"]["id"],
        "choice": value["choice"],
    }


def answered_blocks(blocks: list[Json], choice: str, user: str) -> list[Json]:
    """The ask's blocks once answered: the buttons give way to who chose what."""
    done = {"type": "mrkdwn", "text": f"✓ {escape(choice)} by <@{user}>"}
    kept = [block for block in blocks if block.get("type") != "actions"]
    return [*kept, {"type": "context", "elements": [done]}]


def parse_command(line: str) -> Json | None:
    """One stdin line as a command, or None when it isn't JSON or isn't a known command."""
    try:
        command = json.loads(line)
    except ValueError:
        return None
    return command if isinstance(command, dict) and command.get("type") in COMMANDS else None


def local_name(file: Json) -> str:
    """A safe file name for a Slack file: its id and name, with no path or odd characters."""
    name = f"{file.get('id', 'file')}-{file.get('name') or 'file'}"
    return re.sub(r"[^\w.\- ]", "_", name).strip(". ")[:120]


def is_slack_url(url: str) -> bool:
    """Only send the bot token to Slack's own file servers."""
    parts = urlparse(url)
    return parts.scheme == "https" and (parts.hostname or "").endswith(".slack.com")


def read_commands(loop: asyncio.AbstractEventLoop, queue: asyncio.Queue[Json]) -> None:
    """Queue each stdin command. A thread, because asyncio can't read stdin on Windows. The end
    of input counts as stop: Attaché has closed the pipe."""
    for line in sys.stdin:
        if (command := parse_command(line)) is not None:
            loop.call_soon_threadsafe(queue.put_nowait, command)
    loop.call_soon_threadsafe(queue.put_nowait, {"type": "stop"})


async def name_of(client: Any, names: dict[str, str], user: str) -> str:
    """A user's display name from users.info, cached; the id when Slack won't say."""
    if user not in names:
        try:
            info = (await client.users_info(user=user))["user"]
            profile = info.get("profile") or {}
            names[user] = profile.get("display_name") or info.get("real_name") or info["name"]
        except Exception:
            names[user] = user
    return names[user]


async def download(files: list[Json], token: str) -> list[str]:
    """Save a message's files into ATTACHE_FILES and return their absolute paths."""
    if not files:
        return []
    import aiohttp

    folder = Path(os.environ.get("ATTACHE_FILES") or "inbox").resolve()
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    async with aiohttp.ClientSession(headers={"Authorization": f"Bearer {token}"}) as session:
        for file in files:
            url = file.get("url_private_download") or ""
            if not is_slack_url(url):
                continue  # external or hidden files have no download link
            path = folder / local_name(file)
            try:
                async with session.get(url) as response:
                    # Without files:read, Slack answers with its sign-in page instead.
                    html = response.content_type == "text/html"
                    if response.status != 200 or (html and file.get("mimetype") != "text/html"):
                        why = f"HTTP {response.status}, {response.content_type}; files:read?"
                        log("warning", f"could not download {file.get('name')} ({why})")
                        continue
                    with path.open("wb") as out:
                        async for chunk in response.content.iter_chunked(65536):
                            out.write(chunk)
            except aiohttp.ClientError as error:
                log("warning", f"could not download {file.get('name')}: {error}")
                continue
            paths.append(str(path))
    return paths


async def send(client: Any, command: Json, roots: dict[str, str | None]) -> None:
    """Post a "send": text as Markdown, or files with the text as their comment."""
    chat, text = command["chat_id"], quiet(command.get("text") or "")
    thread = reply_thread(command.get("reply_to"), chat, roots)
    uploads = []
    for path in command.get("files") or []:
        if Path(path).is_file():
            uploads.append({"file": path})
        else:
            log("warning", f"not sending {path}: no such file")
    if uploads:
        await client.files_upload_v2(
            channel=chat, file_uploads=uploads, initial_comment=text or None, thread_ts=thread
        )
        return
    for i in range(0, len(text), 12000):  # markdown_text holds 12,000 characters
        part = text[i : i + 12000]
        await client.chat_postMessage(channel=chat, thread_ts=thread, markdown_text=part)


async def ask(client: Any, command: Json) -> None:
    """Post an "ask" with one button per option."""
    text = command["text"]
    blocks = ask_blocks(text, command["options"], command["ask_id"])
    await client.chat_postMessage(channel=command["chat_id"], text=escape(text), blocks=blocks)


async def run(bot_token: str, app_token: str) -> int:
    """The sidecar: Slack events out as JSON lines, commands from stdin in, until stop."""
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
    from slack_bolt.async_app import AsyncApp

    app = AsyncApp(token=bot_token)
    client = app.client
    self_id = (await client.auth_test())["user_id"]
    names: dict[str, str] = {}
    seen: dict[Any, Any] = {}
    roots: dict[str, str | None] = {}

    async def on_event(event: Json) -> None:
        user = event.get("user")
        if user and not event.get("bot_id"):
            await name_of(client, names, user)
        message = inbound(event, self_id, names, seen)
        if message is not None:
            remember(roots, message["message_id"], thread_root(event))
            message["files"] = await download(event.get("files") or [], bot_token)
            emit(message)

    async def on_answer(ack: Any, body: Json) -> None:
        await ack()
        answer = answer_from_action(body)
        emit(answer)
        asked = body["message"]
        blocks = answered_blocks(asked.get("blocks") or [], answer["choice"], answer["sender_id"])
        await client.chat_update(
            channel=body["channel"]["id"], ts=asked["ts"], text=asked.get("text", ""), blocks=blocks
        )

    async def on_error(error: Exception) -> None:
        log("error", f"slack: {error}")

    app.event("message")(on_event)
    app.event("app_mention")(on_event)
    app.action(re.compile("^attache_answer_"))(on_answer)
    app.error(on_error)

    commands: asyncio.Queue[Json] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    threading.Thread(target=read_commands, args=(loop, commands), daemon=True).start()
    handler = AsyncSocketModeHandler(app, app_token)
    await handler.connect_async()  # not start_async(): that prints a banner to stdout
    emit({"type": "ready", "self_id": self_id, "name": await name_of(client, names, self_id)})
    while (command := await commands.get())["type"] != "stop":
        try:
            if command["type"] == "send":
                await send(client, command, roots)
            elif command["type"] == "ask":
                await ask(client, command)
        except Exception as error:
            log("error", f"{command['type']} failed: {error}")
    with contextlib.suppress(Exception):  # Attaché waits 5 seconds at most
        await asyncio.wait_for(handler.close_async(), 4)
    return 0


def load_tokens() -> tuple[str, str] | None:
    """(bot token, app-level token) from the keychain, or None if either is missing."""
    import keyring

    bot, app = (keyring.get_password(SERVICE, name) for name in ("bot_token", "app_token"))
    return (bot, app) if bot and app else None


def setup() -> int:
    """Ask for both tokens without echoing them and store them in the keychain."""
    import getpass

    import keyring

    tokens = [
        ("bot_token", "xoxb-", "Bot User OAuth Token"),
        ("app_token", "xapp-", "App-Level Token"),
    ]
    for name, prefix, label in tokens:
        value = getpass.getpass(f"{label} ({prefix}...), or Enter to keep the current one: ")
        if not value.strip():
            continue
        if not value.strip().startswith(prefix):
            print(f"That isn't a {label}: it starts with {prefix}", file=sys.stderr)
            return 1
        keyring.set_password(SERVICE, name, value.strip())
    print(f"Saved in the keychain as {SERVICE}. Test them with: uv run channel.py check")
    return 0


def check(bot_token: str, app_token: str, member: str | None = None) -> int:
    """Test both tokens; with a member id, also print that person's DM with the bot."""
    from slack_sdk import WebClient

    bot = WebClient(token=bot_token)
    step = "The bot token (xoxb-)"
    try:
        auth = bot.auth_test()
        step = "The app-level token (xapp-)"
        WebClient().apps_connections_open(app_token=app_token)
        print(f"Both tokens work: bot user {auth['user_id']} in {auth['team']} ({auth['team_id']})")
        if member:
            step = f"Opening a DM with {member}"
            dm = bot.conversations_open(users=member)["channel"]["id"]
            print(f'Your DM with the bot, for [owner]: approvals = "slack:{dm}"')
    except Exception as error:
        print(f"{step} failed: {error}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """`setup`, `check`, or with no command the sidecar."""
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    parser = argparse.ArgumentParser(
        description="Attaché's Slack channel. setup stores the tokens in the keychain, check "
        "tests them, and with no command it runs the sidecar."
    )
    parser.add_argument("command", nargs="?", choices=["setup", "check"])
    parser.add_argument("member", nargs="?", help="for check: your member id (U...)")
    args = parser.parse_args(argv)
    if args.command == "setup":
        return setup()

    def fail(text: str) -> int:
        if args.command == "check":
            print(text, file=sys.stderr)
        else:
            log("error", text)
        return 2

    try:
        tokens = load_tokens()
    except Exception as error:  # no keychain, e.g. on a headless Linux box
        return fail(f"cannot read the keychain: {error}")
    if tokens is None:
        return fail(f"Slack tokens are missing. Run: {SETUP}")
    if args.command == "check":
        return check(*tokens, args.member)
    logging.basicConfig(level=logging.WARNING)  # to stderr, which goes to Attaché's log
    try:
        return asyncio.run(run(*tokens))
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        log("error", f"slack: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
