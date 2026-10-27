"""An isolated local fixture for the optional Playwright workspace checks and screenshots.

No personal Attaché home, model, credentials or external requests are used.
"""

from __future__ import annotations

import socket
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import uvicorn
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from attache import config, web
from attache.router import NEW_CHAT
from attache.store import Store
from attache.types import Inbound, Outbound

TOKEN = "isolated-browser-test-token"
SETTINGS = """# Fixture comment preserved by the settings editor.
[owner]
name = "Alex"
[assistant]
name = "Attaché"
[routing]
own_chats = "local"
group_chats = "api"
[endpoints.local]
api = "openai-chat"
model = "local-model"
base_url = "http://localhost:11434/v1"
[endpoints.api]
api = "anthropic"
model = "example-model"
key_env = "ATTACHE_BROWSER_TEST_KEY"
"""
PAGE = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="attache-collections" content="todos"><title>Tasks</title>
<style>body{font:16px/1.6 system-ui;margin:32px;background:#11141a;color:#e2e4e8}
h1{font-weight:600;font-size:26px}
label{display:block;padding:16px 0;border-bottom:1px solid #2a3142}
input[type=checkbox]{accent-color:#ff9100;margin-right:12px}p{color:#a0a4ab}
input[type=text]{padding:10px;background:#161719;border:1px solid #3b4356;
color:inherit;border-radius:6px}
</style><h1>Your tasks</h1><p>A small plan for the week · sample workspace</p>
<label><input type="checkbox">Review the workspace proposal</label>
<label><input type="checkbox">Try the full-page chat layout</label>
<label><input type="checkbox">Choose a dataset to explore</label>
<p><input type="text" aria-label="Page draft" placeholder="Add a task…"></p></html>"""


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="attache-ui-test-") as directory:
        home = Path(directory)
        (home / "attache.toml").write_text(SETTINGS, encoding="utf-8")
        (home / "artifacts").mkdir()
        for name in ("home", "notes", "analysis-sample"):
            (home / "artifacts" / f"{name}.html").write_text(PAGE, encoding="utf-8")
        store = Store(home / "attache.db")
        store.add_message("local:me", "user", "Can we make this workspace fit how I work?")
        store.add_message(
            "local:me",
            "assistant",
            "Use Chat for a focused conversation, Split to keep a page beside it, "
            "or Workspace when you need more room. Drag the divider to find a comfortable fit.",
        )
        store.task_add("local:me", "Review this week's plan", time.time() + 86400)
        channel = web.WebChannel()

        async def handle(message: Inbound) -> None:
            store.add_message("local:me", "user", message.text)
            if message.text == "/new":  # what Router.new_chat sends, with nothing to stop
                store.new_chat("local:me")
                await channel.activity("me", "new", {})
                await channel.send(Outbound("local:me", NEW_CHAT))

        channel.on_message = handle
        services = SimpleNamespace(
            config=config.load(home),
            store=store,
            ui_token=TOKEN,
            gateway={},
            snapshots={},
            channels=SimpleNamespace(pending={}, stop_turns=lambda _: False),
        )
        app = web.create_app(services, channel)

        async def republish(_: Request) -> JSONResponse:
            (home / "artifacts" / "home.html").write_text(
                PAGE + "<!-- updated -->", encoding="utf-8"
            )
            return JSONResponse({"ok": True})

        app.routes.append(Route("/__test/republish", republish, methods=["POST"]))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        print(f"UI_TEST_URL=http://127.0.0.1:{port}/#t={TOKEN}", flush=True)
        try:
            uvicorn.run(app, host="127.0.0.1", port=port, log_level="error")
        finally:
            store.close()


if __name__ == "__main__":
    main()
