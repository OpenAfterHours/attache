from __future__ import annotations

import getpass
import importlib.util
import json
import os
import sys
import warnings
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from attache import config, credentials

SECRET = "fake-new-model-key-do-not-print"
CONFIG = """# Keep this home and its comments.
[routing]
own_chats = "custom"
group_chats = "custom"

[endpoints.custom]
api = "openai-chat" # compatible API
model = "example-model"
base_url = "https://models.example/v1"
key_env = "CUSTOM_API_KEY"

[assistant]
name = "Attaché" # untouched
"""


@pytest.fixture
def command(home: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    path = config.BUNDLED / "skills" / "credentials" / "scripts" / "credentials.py"
    spec = importlib.util.spec_from_file_location("model_credentials_skill", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.delenv("ATTACHE_ROLE", raising=False)
    monkeypatch.delenv("ATTACHE_WEB_URL", raising=False)
    monkeypatch.setattr(module.getpass, "getpass", lambda _: SECRET)
    (home / "attache.toml").write_text(CONFIG, encoding="utf-8")
    return module


@pytest.mark.parametrize("bom", [False, True])
def test_login_migrates_only_after_saving_and_preserves_comments(
    command: Any,
    home: Path,
    model_keyring: Any,
    capsys: Any,
    bom: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = home / "attache.toml"
    if bom:
        path.write_text(CONFIG, encoding="utf-8-sig")
    monkeypatch.setenv("CUSTOM_API_KEY", "old-environment-key")
    environment = dict(os.environ)
    assert command.main(["login", "custom"]) == 0
    ep = config.load(home).endpoints["custom"]
    assert ep.key_env is None and len(ep.key_ref) == 32
    assert credentials.api_key(ep) == SECRET
    record = json.loads(model_keyring.get_password(credentials.SERVICE, ep.key_ref))
    assert record == {
        "version": 1,
        "api_key": SECRET,
        "family": "openai",
        "base_url": "https://models.example/v1/",
    }
    text = path.read_text(encoding="utf-8-sig")
    assert "# compatible API" in text and "# untouched" in text and "# Keep this home" in text
    assert path.read_bytes().startswith(b"\xef\xbb\xbf") is bom
    assert SECRET not in text and "key_env" not in text
    assert dict(os.environ) == environment
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err
    assert "https://models.example/v1/" in output.out and "Restart Attaché" in output.out
    assert "remove CUSTOM_API_KEY" in output.out
    assert list(home.glob(".attache-config-*")) == []


@pytest.mark.parametrize("shared", [False, True])
def test_rotation_keeps_old_key_until_commit_and_preserves_shared_refs(
    command: Any, home: Path, model_keyring: Any, monkeypatch: pytest.MonkeyPatch, shared: bool
) -> None:
    assert command.main(["login", "custom"]) == 0
    old = config.load(home).endpoints["custom"].key_ref
    if shared:
        with (home / "attache.toml").open("a", encoding="utf-8") as file:
            file.write(f'\n[endpoints.other]\napi="openai-chat"\nkey_ref="{old}"\n')
    original_write = command.write_config

    def check_then_write(*args: Any) -> None:
        assert model_keyring.get_password(credentials.SERVICE, old) is not None
        original_write(*args)

    monkeypatch.setattr(command, "write_config", check_then_write)
    assert command.main(["login", "custom"]) == 0
    new = config.load(home).endpoints["custom"].key_ref
    assert new != old
    assert (model_keyring.get_password(credentials.SERVICE, old) is not None) is shared
    assert credentials.api_key(config.load(home).endpoints["custom"]) == SECRET


@pytest.mark.parametrize("stage", ["set", "verify", "write", "cancel"])
def test_failed_migration_rolls_back_new_record_and_leaves_config(
    command: Any,
    home: Path,
    model_keyring: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: Any,
    stage: str,
) -> None:
    original = (home / "attache.toml").read_bytes()

    def fail(*args: Any) -> None:
        if stage == "cancel":
            raise KeyboardInterrupt
        raise OSError(SECRET)

    if stage == "set":
        monkeypatch.setattr(model_keyring, "set_password", fail)
    elif stage == "verify":
        get = model_keyring.get_password
        first = True

        def wrong_once(*args: Any) -> str | None:
            nonlocal first
            if first:
                first = False
                return "wrong-value"
            return get(*args)

        monkeypatch.setattr(model_keyring, "get_password", wrong_once)
    else:
        monkeypatch.setattr(command, "write_config", fail)
    assert command.main(["login", "custom"]) == 1
    assert (home / "attache.toml").read_bytes() == original
    assert model_keyring.records == {}
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err


def test_failed_rotation_preserves_previous_record(
    command: Any, home: Path, model_keyring: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert command.main(["login", "custom"]) == 0
    before = dict(model_keyring.records)
    original = (home / "attache.toml").read_bytes()

    def fail(*args: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(command, "write_config", fail)
    assert command.main(["login", "custom"]) == 1
    assert model_keyring.records == before
    assert (home / "attache.toml").read_bytes() == original


def test_interruption_after_commit_retains_referenced_key(
    command: Any, home: Path, model_keyring: Any, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    original_write = command.write_config

    def interrupted(*args: Any) -> None:
        original_write(*args)
        raise KeyboardInterrupt

    monkeypatch.setattr(command, "write_config", interrupted)
    assert command.main(["login", "custom"]) == 1
    assert credentials.api_key(config.load(home).endpoints["custom"]) == SECRET
    assert "credential retained" in capsys.readouterr().err


def test_concurrent_config_edit_is_preserved(
    command: Any, home: Path, model_keyring: Any, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    changed = CONFIG + "\n# a simultaneous edit\n"

    def prompt() -> str:
        (home / "attache.toml").write_text(changed, encoding="utf-8")
        return SECRET

    monkeypatch.setattr(command, "prompt_key", prompt)
    assert command.main(["login", "custom"]) == 1
    assert (home / "attache.toml").read_text(encoding="utf-8") == changed
    assert model_keyring.records == {}
    assert "changed during login" in capsys.readouterr().err
    assert list(home.glob(".attache-config-*")) == []


def test_atomic_replace_failure_cleans_temp_file(
    command: Any, home: Path, model_keyring: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: Any) -> None:
        raise PermissionError(SECRET)

    monkeypatch.setattr(Path, "replace", fail)
    assert command.main(["login", "custom"]) == 1
    assert (home / "attache.toml").read_text(encoding="utf-8") == CONFIG
    assert model_keyring.records == {} and list(home.glob(".attache-config-*")) == []


def test_logout_retains_reference_and_is_idempotent(
    command: Any, home: Path, model_keyring: Any, capsys: Any
) -> None:
    assert command.main(["login", "custom"]) == 0
    original = (home / "attache.toml").read_bytes()
    assert command.main(["status", "custom"]) == 0
    assert "authentication has not been tested" in capsys.readouterr().out
    assert command.main(["logout", "custom"]) == 0
    assert command.main(["logout", "custom"]) == 0
    assert (home / "attache.toml").read_bytes() == original
    assert model_keyring.records == {}
    assert command.main(["status", "custom"]) == 1
    output = capsys.readouterr()
    assert "revoke it with the provider" in output.out
    assert SECRET not in output.out + output.err


@pytest.mark.parametrize("guard", ["pipe", "ATTACHE_ROLE", "ATTACHE_WEB_URL"])
@pytest.mark.parametrize("action", ["login", "logout"])
def test_mutations_require_owners_terminal(
    command: Any,
    home: Path,
    model_keyring: Any,
    monkeypatch: pytest.MonkeyPatch,
    guard: str,
    action: str,
) -> None:
    if guard == "pipe":
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    else:
        monkeypatch.setenv(guard, "owner")
    assert command.main([action, "custom"]) == 1
    assert model_keyring.records == {}
    assert (home / "attache.toml").read_text(encoding="utf-8") == CONFIG


@pytest.mark.parametrize("input_type", ["empty", "echo", "eof", "cancel"])
def test_unsafe_or_cancelled_input_leaves_no_record(
    command: Any, model_keyring: Any, monkeypatch: pytest.MonkeyPatch, input_type: str
) -> None:
    def prompt(*args: Any) -> str:
        if input_type == "echo":
            warnings.warn("cannot hide input", getpass.GetPassWarning, stacklevel=2)
        if input_type == "eof":
            raise EOFError
        if input_type == "cancel":
            raise KeyboardInterrupt
        return " "

    monkeypatch.setattr(command.getpass, "getpass", prompt)
    assert command.main(["login", "custom"]) == 1
    assert model_keyring.records == {}


def test_environment_status_does_not_require_keyring(
    command: Any, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.setenv("CUSTOM_API_KEY", SECRET)
    assert command.main(["status", "custom"]) == 0
    assert "CUSTOM_API_KEY available" in capsys.readouterr().out
    assert command.main(["logout", "custom"]) == 1
    assert os.environ["CUSTOM_API_KEY"] == SECRET


def test_failed_cleanup_reports_only_orphan_reference(
    command: Any, model_keyring: Any, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    def fail(*args: Any) -> None:
        raise RuntimeError(SECRET)

    monkeypatch.setattr(command, "write_config", fail)
    monkeypatch.setattr(model_keyring, "delete_password", fail)
    assert command.main(["login", "custom"]) == 1
    output = capsys.readouterr()
    assert "Remove unused credential" in output.err
    assert SECRET not in output.out + output.err
