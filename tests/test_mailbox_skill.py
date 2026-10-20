"""The mailbox skill: owner only, no scripts, its per-account starters, the triage prompt and
the morning routine it sets up."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

from attache import skills, tools

FOLDER = skills.BUNDLED / "mailbox"
TRIAGE = (FOLDER / "triage.md").read_text(encoding="utf-8")
STARTERS = ["rules.md", "learned.md", "vip.md"]
MAIL = skills.BUNDLED / "mail" / "scripts" / "mail.py"
FILLED = {"<id>": "outlook:AAAA", "<account>": "outlook", "<folder>": "Receipts", "<text>": "Hi"}


def mailbox() -> tuple[dict[str, Any], str]:
    return skills.parse((FOLDER / "SKILL.md").read_text(encoding="utf-8"))


def mail_args(text: str) -> list[list[str]]:
    """Every `args: ["<command>", ...]` list in the text, as the script tool would get it."""
    found = re.findall(r'args: (\["[^\]]*\])', text)
    return [json.loads(" ".join(raw.split())) for raw in found]


def test_owner_only_and_no_scripts() -> None:
    meta, _ = mailbox()
    assert meta["name"] == "mailbox" and "metadata" not in meta
    assert not tools.for_members(skills.Skill("mailbox", meta["description"], FOLDER, meta))
    assert not list(FOLDER.rglob("*.py"))
    assert {p.relative_to(FOLDER).as_posix() for p in FOLDER.rglob("*.md")} == {
        "SKILL.md",
        "triage.md",
        *(f"templates/{name}" for name in STARTERS),
    }


def test_skill_names_the_files_per_account() -> None:
    _, body = mailbox()
    assert "skills/mailbox/accounts/<account>/" in body
    for name in STARTERS:
        assert f"`{name}`" in body, name
    assert "skills/mailbox/templates/" in body and "skills/mail/accounts.toml" in body
    assert "untrusted" in body and "Never send during a triage" in body


@pytest.mark.parametrize("name", STARTERS)
def test_starters_have_no_live_examples(name: str) -> None:
    text = (FOLDER / "templates" / name).read_text(encoding="utf-8")
    assert text.startswith("# ") and "<account>" in text.splitlines()[0]
    if name != "learned.md":  # examples sit in their own section, which is not followed
        live, examples = text.split("## Examples", 1)
        assert "ignores this section" in examples and re.search(r"^- ", examples, re.M)
        assert not re.search(r"^- ", live, re.M)  # a fresh copy holds no rules and no VIPs


def test_the_morning_routine() -> None:
    _, body = mailbox()
    block = re.search(r"```json\n(.*?)```", body, re.S)
    task = json.loads(block[1])
    assert task["op"] == "add" and task["kind"] == "turn" and task["cron"] == "30 7 * * 1-5"
    assert "skills/mailbox/triage.md" in task["text"]  # edits to the prompt apply next run
    assert set(task) <= {"op", "text", "at", "in_minutes", "cron", "kind", "id", "all"}  # schedule
    assert "approvals" in body and "local:me" in body  # where it runs


def test_triage_steps() -> None:
    commands = [args[0] for args in mail_args(TRIAGE)]
    assert commands == ["doctor", "folders", "inbox", "move", "flag", "draft-reply"]
    assert 'script {skill: "mail", script: "mail.py"' in TRIAGE
    inbox = next(args for args in mail_args(TRIAGE) if args[0] == "inbox")
    assert "--unread" in inbox
    for word in ("rules.md", "learned.md", "vip.md", "summary"):
        assert word in TRIAGE, word
    # what a triage never does: send, pass --many, or add recipients
    text = " ".join(TRIAGE.split())
    for rule in (
        "Never run `send-draft` in a triage",
        "never pass `--many`",
        "never add a recipient",
    ):
        assert rule in text, rule


@pytest.mark.skipif(not MAIL.is_file(), reason="the mail skill is not here yet")
def test_triage_commands_parse_with_the_mail_skill(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.syspath_prepend(str(MAIL.parent))  # mail.py imports its backends as siblings
    spec = importlib.util.spec_from_file_location("attache_mail_for_mailbox", MAIL)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    for args in mail_args(TRIAGE):
        filled = [FILLED.get(arg, arg) for arg in args]
        assert module.parser().parse_args(filled).command == args[0]


def test_installed_with_the_starters(home: Path) -> None:
    skills.install_bundled(home)
    installed = home / "skills" / "mailbox"
    assert (installed / "triage.md").read_text(encoding="utf-8") == TRIAGE
    assert all((installed / "templates" / name).is_file() for name in STARTERS)
