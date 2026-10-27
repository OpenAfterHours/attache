from __future__ import annotations

import asyncio
import json
import os
import tomllib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.testclient import TestClient

from attache import config, settings, web
from attache.backends.openai import ChatAdapter, _client
from attache.store import Store


@pytest.fixture
def editor(home: Path) -> settings.Settings:
    text = (
        config._template("Alex", "claude")
        + """
# private-comment-secret
[channels.slack]
enabled = false  # keep channel note
token = "channel-secret"
[endpoints.private]
api = "openai-chat"
model = "local"
base_url = "https://user:url-secret@example.com/v1?key=query-secret"
key_env = "PRIVATE_API_KEY"
headers = {Authorization = "Bearer option-secret"}
"""
    )
    (home / "attache.toml").write_text(text, encoding="utf-8")
    return settings.Settings(config.load(home))


def proposal(editor: settings.Settings, **changes: Any) -> dict[str, Any]:
    return {"revision": editor.read()["revision"], "changes": changes}


def test_projection_never_contains_secrets(
    editor: settings.Settings, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("PRIVATE_API_KEY", "environment-secret")
    view = editor.read()
    encoded = json.dumps(view)
    for secret in (
        "private-comment",
        "channel-secret",
        "url-secret",
        "query-secret",
        "option-secret",
        "environment-secret",
    ):
        assert secret not in encoded
    assert view["saved"]["endpoints"]["private"]["credential_status"] == "configured"
    assert view["saved"]["endpoints"]["private"]["base_url_hidden"] is True
    assert "base_url" not in view["saved"]["endpoints"]["private"]
    assert view["saved"]["channels"] == {"slack": {"enabled": False}}
    assert "credential_status" not in view["toml"]
    assert tomllib.loads(view["toml"])["assistant"]["name"] == "Attaché"
    assert view["restart_required"] is False


def test_validate_save_and_restore(editor: settings.Settings):
    original = editor.path.read_bytes()
    data = proposal(
        editor, **{"assistant.name": "New Name", "web.port": 9191, "channels.slack.enabled": True}
    )
    preview = editor.update(data)
    assert preview["valid"] is True
    assert preview["saved"]["assistant"]["name"] == "New Name"
    assert preview["running"]["assistant"]["name"] == "Attaché"
    assert preview["restart_required"] is True
    assert editor.path.read_bytes() == original
    assert not editor.backup.exists()
    saved = editor.update(data, save=True)
    assert saved["revision"] != data["revision"]
    assert saved["backup_available"] is True
    assert editor.backup.read_bytes() == original
    assert "# keep channel note" in editor.path.read_text(encoding="utf-8")
    assert "private-comment-secret" in editor.path.read_text(encoding="utf-8")
    assert config.load(editor.path.parent).channels["slack"]["token"] == "channel-secret"
    restored = editor.update(
        {
            "revision": saved["revision"],
            "restore": True,
            "backup_revision": saved["backup_revision"],
        },
        save=True,
    )
    assert restored["restart_required"] is False
    assert editor.path.read_bytes() == original
    assert editor.read()["saved"]["assistant"]["name"] == "Attaché"


def test_advanced_toml_round_trip_preserves_unknown_options(editor: settings.Settings):
    view = editor.read()
    toml = view["toml"].replace('"name" = "Attaché"', '"name" = "My assistant"')
    result = editor.update({"revision": view["revision"], "toml": toml}, save=True)
    assert result["saved"]["assistant"]["name"] == "My assistant"
    raw = tomllib.loads(editor.path.read_text(encoding="utf-8"))
    assert raw["endpoints"]["private"]["headers"] == {"Authorization": "Bearer option-secret"}


@pytest.mark.parametrize(
    "changes",
    [
        {"web.port": True},
        {"web.port": -1},
        {"web.host": "0.0.0.0"},
        {"limits.guest_daily": 0},
        {"routing.own_chats": "missing"},
        {"routing.group_chats": "claude"},
        {"endpoints.claude.model": []},
        {"endpoints.private.base_url": "https://user:secret@example.com"},
        {"endpoints.private.base_url": "https://example.com/?api_key=secret"},
        {"endpoints.private.base_url": "https://example.com/#secret"},
        {"endpoints.private.key_env": "sk-secret"},
        {"endpoints.private.api": "unknown"},
        {"endpoints.private.headers": "secret"},
        {"channels.slack.token": "secret"},
        {"owner.name": "other"},
        {"endpoints.private.model": ""},
        {"assistant.name": ""},
        {"assistant.name": " \t\n"},
    ],
)
def test_invalid_edits_do_not_touch_source(editor: settings.Settings, changes: dict[str, Any]):
    original = editor.path.read_bytes()
    with pytest.raises(settings.SettingsError) as caught:
        editor.update(proposal(editor, **changes), save=True)
    assert caught.value.status == 400
    assert "secret" not in str(caught.value)
    assert editor.path.read_bytes() == original
    assert not editor.backup.exists()


def test_unknown_and_missing_toml_fields_rejected(editor: settings.Settings):
    view = editor.read()
    for text in ["[bad", view["toml"] + "\n[owner]\nname='hijack'", "[assistant]\nname='only'"]:
        with pytest.raises(settings.SettingsError):
            editor.update({"revision": view["revision"], "toml": text}, save=True)


@pytest.mark.parametrize("mode", ["forms", "toml"])
def test_clearing_base_url_restores_provider_default(
    editor: settings.Settings, monkeypatch: pytest.MonkeyPatch, mode: str
):
    custom_url = "http://localhost:11434/v1"
    source = editor.path.read_text(encoding="utf-8").replace(
        "https://user:url-secret@example.com/v1?key=query-secret", custom_url
    )
    editor.path.write_text(source, encoding="utf-8")
    view = editor.read()
    payload = {"revision": view["revision"], "changes": {"endpoints.private.base_url": ""}}
    if mode == "toml":
        payload = {
            "revision": view["revision"],
            "toml": view["toml"].replace(f'"base_url" = "{custom_url}"', '"base_url" = ""'),
        }
    editor.update(payload, save=True)
    endpoint = config.load(editor.path.parent).endpoints["private"]
    assert endpoint.base_url is None
    assert endpoint.options["headers"] == {"Authorization": "Bearer option-secret"}
    assert "# private-comment-secret" in editor.path.read_text(encoding="utf-8")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setenv("PRIVATE_API_KEY", "offline-test-key")
    client = asyncio.run(_client(ChatAdapter(endpoint)))
    try:
        assert str(client.base_url.join("chat/completions")) == (
            "https://api.openai.com/v1/chat/completions"
        )
    finally:
        asyncio.run(client.close())


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:wrong/v1",
        "http://localhost:-1/v1",
        "http://localhost:0/v1",
        "http://localhost:65536/v1",
        "http://localhost:12.5/v1",
        "http://localhost:\uff11\uff12\uff13/v1",
        "https://example.com/\npath",
        "https://exam\tple.com",
        " https://example.com",
        "https://example.com/with space",
        "https://example.com/\x00",
        "https://example.com/\x7f",
    ],
)
def test_invalid_endpoint_urls_are_rejected_without_saving(editor: settings.Settings, url: str):
    original = editor.path.read_bytes()
    with pytest.raises(settings.SettingsError, match="Endpoint URLs"):
        editor.update(proposal(editor, **{"endpoints.private.base_url": url}), save=True)
    assert editor.path.read_bytes() == original
    assert not editor.backup.exists()


@pytest.mark.parametrize(
    "url", ["http://localhost:1/v1", "http://[::1]:65535/v1", "https://example.com/v1"]
)
def test_valid_endpoint_urls_remain_editable(editor: settings.Settings, url: str):
    editor.update(proposal(editor, **{"endpoints.private.base_url": url}), save=True)
    assert config.load(editor.path.parent).endpoints["private"].base_url == url


def test_blank_assistant_name_is_rejected_in_toml_editor(editor: settings.Settings):
    view = editor.read()
    text = view["toml"].replace('"name" = "Attaché"', '"name" = "   "')
    with pytest.raises(settings.SettingsError, match="assistant name"):
        editor.update({"revision": view["revision"], "toml": text}, save=True)
    assert config.load(editor.path.parent).name == "Attaché"


def test_stale_edit_and_backup_are_rejected(editor: settings.Settings):
    first = proposal(editor, **{"assistant.name": "First"})
    editor.update(proposal(editor, **{"assistant.name": "Second"}), save=True)
    with pytest.raises(settings.SettingsError) as caught:
        editor.update(first, save=True)
    assert caught.value.status == 409
    current = editor.read()
    with pytest.raises(settings.SettingsError) as caught:
        editor.update(
            {"revision": current["revision"], "restore": True, "backup_revision": "wrong"},
            save=True,
        )
    assert caught.value.status == 409
    editor.lock.write_text("busy", encoding="utf-8")
    with pytest.raises(settings.SettingsError) as caught:
        editor.update(proposal(editor, **{"assistant.name": "Third"}), save=True)
    assert caught.value.status == 409


def test_failed_replace_preserves_original_and_cleans_temporary(
    editor: settings.Settings, monkeypatch: pytest.MonkeyPatch
):
    original = editor.path.read_bytes()
    replace = os.replace

    def fail(path: Path, target: Path):
        if target == editor.path:
            raise OSError("disk full")
        return replace(path, target)

    monkeypatch.setattr(settings.os, "replace", fail)
    with pytest.raises(OSError):
        editor.update(proposal(editor, **{"assistant.name": "Changed"}), save=True)
    assert editor.path.read_bytes() == original
    assert editor.backup.read_bytes() == original
    assert not list(editor.path.parent.glob(".settings-*"))
    assert not editor.lock.exists()


def test_file_size_limit(editor: settings.Settings):
    editor.path.write_bytes(b"#" + b"x" * settings.MAX_BYTES)
    with pytest.raises(settings.SettingsError) as caught:
        editor.read()
    assert caught.value.status == 413


def test_symlinks_rejected(editor: settings.Settings, tmp_path: Path):
    target = tmp_path / "outside.toml"
    target.write_text("outside", encoding="utf-8")
    try:
        editor.backup.symlink_to(target)
    except OSError:
        pytest.skip("symlinks need Windows developer mode")
    with pytest.raises(settings.SettingsError) as caught:
        editor.read()
    assert caught.value.status == 403


@pytest.mark.parametrize(
    "source",
    [
        '[assistant]\nname = """Multiple\nlines # text\n[not-a-table]\n""" # kept comment\n',
        "[assistant]\n\"name\" = 'Name # quoted' # kept comment\n",
        'assistant.name = "Dotted" # kept comment\n',
    ],
)
def test_surgical_merge_handles_strings_comments_and_dotted_keys(
    editor: settings.Settings, source: str
):
    template = config._template("Alex", "claude")
    source += template[template.index("[owner]") :]
    editor.path.write_text(source, encoding="utf-8")
    editor.update(proposal(editor, **{"assistant.name": "Replacement"}), save=True)
    assert config.load(editor.path.parent).name == "Replacement"
    assert "# kept comment" in editor.path.read_text(encoding="utf-8")


def test_inline_table_is_rejected_without_rewriting(editor: settings.Settings):
    source = (
        'assistant = {name = "Inline"}\n'
        + config._template("Alex", "claude").split("[owner]", 1)[1]
    )
    source = source.replace('name = "Alex"', '[owner]\nname = "Alex"', 1)
    editor.path.write_text(source, encoding="utf-8")
    with pytest.raises(settings.SettingsError, match="layout"):
        editor.update(proposal(editor, **{"assistant.name": "Replacement"}), save=True)
    assert editor.path.read_text(encoding="utf-8") == source


def test_add_missing_section_and_fields(editor: settings.Settings):
    editor.update(
        proposal(
            editor,
            **{
                "limits.guest_daily": 11,
                "limits.member_daily": 23,
                "endpoints.claude.key_env": "CLAUDE_KEY",
            },
        ),
        save=True,
    )
    loaded = config.load(editor.path.parent)
    assert loaded.limits["guest_daily"] == 11
    assert loaded.limits["member_daily"] == 23
    assert loaded.endpoints["claude"].key_env == "CLAUDE_KEY"


def test_local_endpoint_does_not_claim_key_is_missing(
    editor: settings.Settings, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    text = (
        editor.path.read_text(encoding="utf-8")
        .replace(
            'base_url = "https://user:url-secret@example.com/v1?key=query-secret"',
            'base_url = "http://localhost:11434/v1"',
        )
        .replace('key_env = "PRIVATE_API_KEY"', "")
    )
    editor.path.write_text(text, encoding="utf-8")
    assert editor.read()["saved"]["endpoints"]["private"]["credential_status"] == "not-required"


def test_keyring_endpoint_reports_keyring_and_refuses_key_env(editor: settings.Settings):
    text = (
        editor.path.read_text(encoding="utf-8")
        .replace(
            'base_url = "https://user:url-secret@example.com/v1?key=query-secret"',
            'base_url = "https://example.com/v1"',
        )
        .replace('key_env = "PRIVATE_API_KEY"', 'key_ref = "opaque-ref"')
    )
    editor.path.write_text(text, encoding="utf-8")
    view = editor.read()
    assert view["saved"]["endpoints"]["private"]["credential_status"] == "keyring"
    assert "opaque-ref" not in json.dumps(view)
    with pytest.raises(settings.SettingsError):
        editor.update(proposal(editor, **{"endpoints.private.key_env": "OTHER_KEY"}))


@pytest.fixture
def api(editor: settings.Settings):
    store = Store(editor.path.parent / "attache.db")
    services = SimpleNamespace(config=editor.running, store=store, ui_token="owner-token")
    client = TestClient(web.create_app(services, web.WebChannel()), base_url="http://localhost")
    public_services = SimpleNamespace(
        **{**vars(services), "config": replace(editor.running, public_url="https://pc.ts.net")}
    )
    public = TestClient(
        web.create_app(public_services, web.WebChannel(), public=True), base_url="http://localhost"
    )
    yield client, public, services
    client.close()
    public.close()
    store.close()


def test_settings_api_read_validate_save_and_no_cache(api):
    client, _, _ = api
    headers = {"X-Attache-Token": "owner-token"}
    response = client.get("/api/settings", headers=headers)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    data = {"revision": response.json()["revision"], "changes": {"assistant.name": "Web name"}}
    preview = client.post("/api/settings/validate", headers=headers, json=data)
    assert preview.status_code == 200
    assert (
        client.get("/api/settings", headers=headers).json()["saved"]["assistant"]["name"]
        == "Attaché"
    )
    saved = client.put("/api/settings", headers=headers, json=data)
    assert saved.status_code == 200
    assert saved.json()["saved"]["assistant"]["name"] == "Web name"
    assert client.put("/api/settings", headers=headers, json=data).status_code == 409


@pytest.mark.parametrize(
    "route,method",
    [
        ("/api/settings", "get"),
        ("/api/settings", "put"),
        ("/api/settings/validate", "post"),
        ("/api/settings/restore", "post"),
    ],
)
def test_settings_never_reachable_by_artifact_or_public_tokens(api, route: str, method: str):
    client, public, services = api
    call = getattr(client, method)
    assert call(route).status_code == 401
    assert call(route, headers={"X-Attache-Share": "share-token"}).status_code == 401
    headers = {
        "X-Attache-Artifact": "home",
        "X-Attache-Token": web.page_token(services, "home"),
        "Origin": "null",
    }
    response = call(route, headers=headers)
    assert response.status_code == 403
    assert "access-control-allow-origin" not in response.headers
    assert (
        getattr(public, method)(route, headers={"X-Attache-Token": "owner-token"}).status_code
        == 404
    )
    assert (
        client.options(
            route, headers={"Origin": "null", "Access-Control-Request-Method": "PUT"}
        ).status_code
        == 405
    )


@pytest.mark.parametrize(
    "body,status",
    [
        ("[]", 400),
        ("null", 400),
        ("{", 400),
        ("{}", 409),
        ('"' + "a" * (settings.MAX_BYTES * 2) + '"', 413),
    ],
    ids=["array", "null", "invalid-json", "missing-revision", "oversized"],
)
def test_settings_api_invalid_requests(api, body: str, status: int):
    client, _, _ = api
    response = client.put(
        "/api/settings",
        headers={"X-Attache-Token": "owner-token", "Content-Type": "application/json"},
        content=body,
    )
    assert response.status_code == status
    assert response.json()["error"]


def test_settings_api_wrong_content_type(api):
    client, _, _ = api
    response = client.put("/api/settings", headers={"X-Attache-Token": "owner-token"}, content="{}")
    assert response.status_code == 415
