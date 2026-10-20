# /// script
# requires-python = ">=3.11"
# dependencies = ["slack-bolt>=1.30", "aiohttp>=3.9", "keyring>=25"]
# ///
"""Slack channel for Attaché: a sidecar that speaks JSON lines on stdin and stdout.

The protocol is in docs/channels.md. It connects over Socket Mode, so Slack needs no public URL.
The tokens live in the OS keychain under the service "attache-slack":

    uv run channel.py setup     store the bot (xoxb-) and app-level (xapp-) tokens
    uv run channel.py check     test them
    uv run channel.py           run the sidecar (Attaché does this)

The protocol translation is plain functions and slack-bolt is imported only inside run() and
check(), so tests can import this file without slack-bolt installed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
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
COMMANDS = ("send", "ask", "answered", "stop")
KEEP = 1000  # messages and button presses remembered
MAX_FILE = 25 * 1024 * 1024  # bytes in one downloaded file
MAX_TOTAL = 50 * 1024 * 1024  # bytes downloaded for one message
MAX_FILES = 10  # files downloaded for one message
SPECIAL = re.compile(r"<!([^>|]+)(?:\|[^>]*)?>")  # <!channel>, <!here>, <!subteam^S01>
BROADCAST = re.compile(r"(?<!\w)@(channel|here|everyone)\b", re.I)
TOKEN_ERRORS = ("invalid_auth", "not_authed", "account_inactive", "token_revoked")
NOT_TAKEN = "That answer wasn't taken: only the person asked can answer, while it's still open."
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
    """Defuse @channel, @here, @everyone and group pings, written as <!here> or as plain words,
    so a reply never notifies a whole channel because a message it quoted asked for that."""
    text = SPECIAL.sub(r"@\1", text)
    return BROADCAST.sub("@\N{ZERO WIDTH JOINER}\\1", text)


def inbound(event: Json, self_id: str, names: dict, seen: dict) -> Json | None:
    """A Slack `message` or `app_mention` event as a protocol "message", or None to ignore it.

    Ignored: bots (this one included), edits, joins and other subtypes, and repeats: Slack
    delivers a channel mention twice, as app_mention and as message.*. `seen` remembers each
    message passed on, as (chat, ts) -> the thread a reply to it belongs in. `names` maps user
    ids to display names; `files` is left for the caller to download.
    """
    user = event.get("user")
    if event.get("bot_id") or not user or user == self_id or event.get("subtype") not in PLAIN:
        return None
    key = (event.get("channel"), event.get("ts"))
    if key in seen:
        return None
    remember(seen, key, thread_root(event))
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


def reply_thread(reply_to: str | None, chat_id: str, seen: dict) -> str | None:
    """The thread_ts for a reply. Slack wants the thread's parent, not a reply inside it."""
    if (chat_id, reply_to) in seen:
        return seen[(chat_id, reply_to)]
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


def wait_for_stop() -> None:
    """Read stdin until a stop command or its end."""
    for line in sys.stdin:
        if (parse_command(line) or {}).get("type") == "stop":
            return


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
    """Save a message's files into ATTACHE_FILES and return their absolute paths. Downloads
    come before Attaché's access check, so a message gets MAX_FILES and MAX_TOTAL at most."""
    import aiohttp

    if len(files) > MAX_FILES:
        log("warning", f"downloading only the first {MAX_FILES} of {len(files)} files")
    folder = Path(os.environ.get("ATTACHE_FILES") or "inbox").resolve()
    folder.mkdir(parents=True, exist_ok=True)
    paths, room = [], MAX_TOTAL
    async with aiohttp.ClientSession(headers={"Authorization": f"Bearer {token}"}) as session:
        for file in files[:MAX_FILES]:
            if path := await fetch(session, file, folder, min(MAX_FILE, room)):
                paths.append(path)
                room -= Path(path).stat().st_size
    return paths


async def fetch(session: Any, file: Json, folder: Path, limit: int) -> str | None:
    """Download one file of up to `limit` bytes, or log why not and give None. Redirects
    aren't followed, so the bot token goes only to the Slack link."""
    url, path = file.get("url_private_download") or "", folder / local_name(file)
    if not is_slack_url(url):
        return None  # external or hidden files have no download link
    try:
        if (file.get("size") or 0) > limit:
            raise ValueError("too big (25 MB a file, 50 MB a message)")
        async with session.get(url, allow_redirects=False) as response:
            # Without files:read, Slack answers with a redirect or its sign-in page.
            html = response.content_type == "text/html" and file.get("mimetype") != "text/html"
            if response.status != 200 or html:
                raise ValueError(f"HTTP {response.status}; does the app have files:read?")
            with path.open("wb") as out:
                async for chunk in response.content.iter_chunked(65536):
                    out.write(chunk)
                    if out.tell() > limit:
                        raise ValueError("too big (25 MB a file, 50 MB a message)")
    except Exception as error:
        path.unlink(missing_ok=True)
        log("warning", f"could not download {file.get('name')}: {error}")
        return None
    return str(path)


async def send(client: Any, command: Json, seen: dict) -> None:
    """Post a "send": text as Markdown, or files with the text as their comment."""
    chat, text = command["chat_id"], quiet(command.get("text") or "")
    thread = reply_thread(command.get("reply_to"), chat, seen)
    uploads = [{"file": path} for path in command.get("files") or []]
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


async def answered(client: Any, command: Json, clicks: dict[Any, Json]) -> None:
    """Attaché's verdict on a button press: a taken answer replaces the buttons with the choice;
    otherwise only the person who pressed is told it wasn't taken."""
    user = command["sender_id"]
    body = clicks.pop((command["ask_id"], user), None)
    if body is None:
        return
    channel, asked = body["channel"]["id"], body["message"]
    if command.get("accepted"):
        choice = answer_from_action(body)["choice"]
        blocks = answered_blocks(asked.get("blocks") or [], choice, user)
        await client.chat_update(channel=channel, ts=asked["ts"], text=asked["text"], blocks=blocks)
    else:
        await client.chat_postEphemeral(channel=channel, user=user, text=NOT_TAKEN)


async def run(bot_token: str, app_token: str) -> int:
    """The sidecar: Slack events out as JSON lines, commands from stdin in, until stop."""
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
    from slack_bolt.async_app import AsyncApp

    app = AsyncApp(token=bot_token)
    client = app.client
    self_id = (await client.auth_test())["user_id"]
    await client.apps_connections_open(app_token=app_token)  # connect() retries a bad one forever
    names: dict[str, str] = {}
    seen: dict[Any, str | None] = {}  # (chat, ts) -> thread root, for dedupe and replies
    clicks: dict[Any, Json] = {}  # (ask_id, user) -> the button press, until Attaché answers

    async def on_event(event: Json) -> None:
        message = inbound(event, self_id, names, seen)
        if message is None:
            return
        message["sender_name"] = await name_of(client, names, message["sender_id"])
        if event.get("files") and (message["mentioned"] or not message["is_group"]):
            message["files"] = await download(event["files"], bot_token)  # DMs and mentions
        emit(message)

    async def on_answer(ack: Any, body: Json) -> None:
        await ack()
        answer = answer_from_action(body)
        remember(clicks, (answer["ask_id"], answer["sender_id"]), body)
        emit(answer)

    app.event("message")(on_event)
    app.event("app_mention")(on_event)
    app.action(re.compile("^attache_answer_"))(on_answer)

    handler = AsyncSocketModeHandler(app, app_token)
    await handler.connect_async()  # not start_async(): that prints a banner to stdout
    commands: asyncio.Queue[Json] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    threading.Thread(target=read_commands, args=(loop, commands), daemon=True).start()
    emit({"type": "ready", "self_id": self_id, "name": await name_of(client, names, self_id)})
    while (command := await commands.get())["type"] != "stop":
        try:
            if command["type"] == "send":
                await send(client, command, seen)
            elif command["type"] == "ask":
                await ask(client, command)
            elif command["type"] == "answered":
                await answered(client, command, clicks)
        except Exception as error:
            log("error", f"{command['type']} failed: {error}")
    await handler.close_async()
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


def check(bot_token: str, app_token: str) -> int:
    """Test both tokens: auth.test takes the xoxb- one, apps.connections.open the xapp- one."""
    from slack_sdk import WebClient

    try:
        auth = WebClient(token=bot_token).auth_test()
        WebClient().apps_connections_open(app_token=app_token)
    except Exception as error:
        print(f"Slack refused a token: {error}", file=sys.stderr)
        return 1
    print(f"Both tokens work: bot user {auth['user_id']} in {auth['team']} ({auth['team_id']})")
    return 0


def main(argv: list[str] | None = None) -> int:
    """`setup`, `check`, or with no command the sidecar."""
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    parser = argparse.ArgumentParser(description="Attaché's Slack sidecar, or its setup and check.")
    parser.add_argument("command", nargs="?", choices=["setup", "check"])
    args = parser.parse_args(argv)
    if args.command == "setup":
        return setup()

    def fail(text: str) -> int:
        """Say why on stderr. The sidecar then idles until stop rather than exiting, because
        Attaché would only restart it, and it can't run until the setup is fixed."""
        print(text, file=sys.stderr)
        if args.command is None:
            wait_for_stop()
        return 2

    try:
        tokens = load_tokens()
    except Exception as error:  # no keychain, e.g. on a headless Linux box
        return fail(f"Slack: cannot read the keychain: {error}")
    if tokens is None:
        return fail(f"Slack tokens are missing. Run: {SETUP}")
    if args.command == "check":
        return check(*tokens)
    try:
        return asyncio.run(run(*tokens))
    except Exception as error:
        from slack_sdk.errors import SlackApiError

        if isinstance(error, SlackApiError) and error.response["error"] in TOKEN_ERRORS:
            return fail(f"Slack refused a token ({error.response['error']}). Run: {SETUP}")
        log("error", f"slack: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
