"""An isolated local fixture for the optional Playwright workspace checks and screenshots.

No personal Attaché home, model, credentials or external requests are used.
"""

from __future__ import annotations

import socket
import tempfile
import time
from datetime import date, timedelta
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
STYLE = """<style>
body{font:15px/1.6 system-ui;margin:0;padding:32px;background:#0d0e11;color:#e2e4e8}
main{max-width:620px;margin:0 auto}h1{font:600 28px/1.2 system-ui;margin:4px 0 24px;color:#fff}
small{font:600 11px/1 monospace;letter-spacing:.08em;text-transform:uppercase;color:#8b93a1}
.box{border:1px solid #2a3142;border-radius:12px;background:#11141a;margin-bottom:20px}
label,.row{display:flex;gap:12px;align-items:center;padding:12px 14px;border-top:1px solid #1f2430}
label:first-child,.row:first-child{border-top:0}input[type=checkbox]{accent-color:#ff9100}
.row span:last-child{margin-left:auto;color:#3dd68c;font:12px monospace}
input[type=text]{width:100%;padding:10px 12px;background:#11141a;border:1px solid #3b4356;
color:inherit;border-radius:10px;box-sizing:border-box}</style>"""
PAGES = {
    "home": """<small>This week</small><h1>Home</h1><div class="box">
<label><input type="checkbox">Send Sam the review deck</label>
<label><input type="checkbox">Book the boiler service</label>
<label><input type="checkbox">Pick up the dry cleaning</label></div>""",
    "lisbon-trip": """<small>6 to 9 November · 3 nights</small><h1>Lisbon trip</h1><div class="box">
<div class="row"><b>Flight out</b><span>Thu 07:05 LHR → LIS</span><span>confirmed</span></div>
<div class="row"><b>Apartment</b><span>Bairro Alto, 3 nights</span><span>confirmed</span></div>
<div class="row"><b>Flight back</b><span>Sun 18:30 LIS → LHR</span><span>confirmed</span></div>
</div>""",
    "notes": """<small>Notes</small><h1>Notes</h1><div class="box"><div class="row">
Ideas for the weekend</div></div>""",
    "analysis-sample": """<small>Analysis</small><h1>Sample analysis</h1><div class="box">
<div class="row">Monthly totals, saved from a notebook</div></div>""",
}


def page(body: str) -> str:
    return (
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="attache-collections" content="todos">'
        f"<title>Page</title>{STYLE}<main>{body}"
        '<p><input type="text" aria-label="Page draft" placeholder="Add a note…"></p></main></html>'
    )


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="attache-ui-test-") as directory:
        home = Path(directory)
        (home / "attache.toml").write_text(SETTINGS, encoding="utf-8")
        (home / "artifacts").mkdir()
        for name, body in PAGES.items():
            (home / "artifacts" / f"{name}.html").write_text(page(body), encoding="utf-8")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        store = Store(home / "attache.db")
        store.add_message("local:me", "user", "Can you put the Lisbon bookings on a page?")
        store.add_message(
            "local:me",
            "assistant",
            f"Done: I made [Lisbon trip](http://127.0.0.1:{port}/#page=lisbon-trip) from the "
            "three confirmation emails. It opens beside the chat.",
        )
        today = date.today()
        todos = [
            ("Send Sam the review deck", today),
            ("Book the boiler service", today - timedelta(days=2)),
            ("Draft the budget notes", today + timedelta(days=2)),
            ("Pick up the dry cleaning", None),
        ]
        for title, due in todos:
            todo = {"title": title, "list": "Inbox", "status": "todo"}
            if due:
                todo["due"] = due.isoformat()
            store.doc_put("todos", None, todo)
        store.task_add("local:me", "Review this week's plan", time.time() + 86400)
        store.task_add("local:me", "Put the bins out", time.time() + 2 * 86400, cron="30 7 * * 4")
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
            channels=SimpleNamespace(
                pending={}, stop_turns=lambda _: False, answer=lambda ask_id, person, choice: True
            ),
        )
        app = web.create_app(services, channel)

        async def republish(_: Request) -> JSONResponse:
            (home / "artifacts" / "home.html").write_text(
                page(PAGES["home"]) + "<!-- updated -->", encoding="utf-8"
            )
            return JSONResponse({"ok": True})

        async def reply(request: Request) -> JSONResponse:
            body = await request.json()  # a reply, or with "ask" a question; neither is stored
            if body.get("ask"):
                await channel.ask(
                    "me", body.get("id", "fixture-ask"), body["text"], body["options"]
                )
            else:
                await channel.send(Outbound("local:me", body["text"]))
            return JSONResponse({"ok": True})

        async def activity(request: Request) -> JSONResponse:
            body = await request.json()  # a turn's progress: {"kind": "step", "label": ...}
            await channel.activity("me", body.pop("kind"), body)
            return JSONResponse({"ok": True})

        app.routes.append(Route("/__test/republish", republish, methods=["POST"]))
        app.routes.append(Route("/__test/reply", reply, methods=["POST"]))
        app.routes.append(Route("/__test/activity", activity, methods=["POST"]))
        print(f"UI_TEST_URL=http://127.0.0.1:{port}/#t={TOKEN}", flush=True)
        try:
            uvicorn.run(app, host="127.0.0.1", port=port, log_level="error")
        finally:
            store.close()


if __name__ == "__main__":
    main()
