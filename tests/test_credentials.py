from __future__ import annotations

import asyncio
import json
import os
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import keyring
import pytest

from attache import cli, config, credentials
from attache.backends.anthropic import AnthropicAdapter
from attache.backends.openai import ChatAdapter, ResponsesAdapter
from attache.backends.openai import _client as openai_client
from attache.types import Endpoint

SECRET = "fake-model-key-never-print-this"
ENDPOINT = Endpoint("custom", "openai-chat", "model", "https://models.example/v1", key_ref="ref")


def saved(endpoint: Endpoint = ENDPOINT, **changes: Any) -> str:
    family, url = credentials.destination(endpoint.api, endpoint.base_url)
    return json.dumps(
        {"version": 1, "api_key": SECRET, "family": family, "base_url": url, **changes}
    )


def test_environment_behavior_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    ep = replace(ENDPOINT, key_ref=None, key_env="ATTACHE_TEST_KEY")
    monkeypatch.setenv("ATTACHE_TEST_KEY", SECRET)
    assert credentials.client_options(ep) == {"api_key": SECRET, "base_url": ep.base_url}
    for empty in (True, False):
        if empty:
            monkeypatch.setenv("ATTACHE_TEST_KEY", "")
        else:
            monkeypatch.delenv("ATTACHE_TEST_KEY")
        with pytest.raises(RuntimeError, match="endpoint 'custom' needs the ATTACHE_TEST_KEY"):
            credentials.api_key(ep)
    assert credentials.api_key(replace(ep, key_env=None)) is None


@pytest.mark.parametrize(
    "source", ['key_ref = ""', "key_ref = 123", 'key_ref = "x"\nkey_env = "X"']
)
def test_config_rejects_invalid_sources_without_opening_store(home: Path, source: str) -> None:
    (home / "attache.toml").write_text(
        '[endpoints.custom]\napi = "openai-chat"\n' + source, encoding="utf-8"
    )
    with pytest.raises(ValueError, match="key_ref"):
        config.load(home)
    assert cli.main(["run"]) == 1  # configuration error, not an uncaught startup exception


def test_config_keeps_reference_out_of_backend_options(home: Path) -> None:
    (home / "attache.toml").write_text(
        '[endpoints.custom]\napi = "openai-chat"\nkey_ref = "ref"\nmax_tokens = 20',
        encoding="utf-8",
    )
    ep = config.load(home).endpoints["custom"]
    assert ep.key_ref == "ref" and ep.key_env is None
    assert ep.options == {"max_tokens": 20}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "https://api.openai.com/v1/"),
        ("HTTPS://Models.Example:443/v1", "https://models.example/v1/"),
        ("http://[::1]:8080/v1/", "http://[::1]:8080/v1/"),
        ("http://localhost:80", "http://localhost/"),
    ],
)
def test_destination_normalizes_equivalent_urls(raw: str | None, expected: str) -> None:
    assert credentials.destination("openai-chat", raw) == ("openai", expected)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "file:///tmp/key",
        "https://user:secret@host/v1",
        "https://host/#secret",
        "https://host:bad",
        " https://host",
        "https://host/\n",
    ],
)
def test_invalid_destination_does_not_echo_input(url: str) -> None:
    with pytest.raises(credentials.CredentialError) as error:
        credentials.destination("openai-chat", url)
    assert "secret" not in str(error.value)


def test_reference_is_bound_to_destination_not_name_or_model(model_keyring: Any) -> None:
    model_keyring.set_password(credentials.SERVICE, "ref", saved())
    compatible = replace(ENDPOINT, name="renamed", model="new", api="openai-responses")
    assert credentials.api_key(compatible) == SECRET
    for changed in (
        replace(ENDPOINT, base_url="https://other.example/v1"),
        replace(ENDPOINT, base_url="https://models.example/v2"),
        replace(ENDPOINT, api="anthropic"),
    ):
        with pytest.raises(credentials.CredentialError, match="different API URL or family"):
            credentials.api_key(changed)


@pytest.mark.parametrize(
    "record",
    [
        "bad json",
        "[]",
        "null",
        saved(version=True),
        saved(version=2),
        saved(api_key=""),
        saved(api_key=123),
        '{"version":1}',
    ],
)
def test_malformed_records_fail_without_echoing_secrets(model_keyring: Any, record: str) -> None:
    model_keyring.set_password(credentials.SERVICE, "ref", record)
    with pytest.raises(credentials.CredentialError, match="invalid saved credential") as error:
        credentials.api_key(ENDPOINT)
    assert SECRET not in str(error.value)


def test_missing_and_locked_stores_do_not_fall_back(
    model_keyring: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-key")
    with pytest.raises(credentials.CredentialError, match="login custom"):
        credentials.api_key(ENDPOINT)

    def locked(*args: Any) -> None:
        raise RuntimeError(SECRET)

    monkeypatch.setattr(model_keyring, "get_password", locked)
    with pytest.raises(credentials.CredentialError, match="unavailable or locked") as error:
        credentials.api_key(ENDPOINT)
    assert SECRET not in str(error.value)


@pytest.mark.parametrize("name", sorted(credentials.BACKENDS))
def test_supported_os_backends_are_selected(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    module, _, cls = name.rpartition(".")
    backend = type(cls, (), {"__module__": module})()
    monkeypatch.setattr(keyring, "get_keyring", lambda: backend)
    assert credentials.keyring_backend() is backend


def test_plaintext_null_and_unknown_backends_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "keyrings.alt.file.PlaintextKeyring",
        "keyring.backends.null.Keyring",
        "user.Store",
    ):
        module, _, cls = name.rpartition(".")
        backend = type(cls, (), {"__module__": module})()
        monkeypatch.setattr(keyring, "get_keyring", lambda backend=backend: backend)
        with pytest.raises(credentials.CredentialError, match="no supported OS"):
            credentials.keyring_backend()


def test_chainer_selects_os_backend_without_insecure_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secure = type("Keyring", (), {"__module__": "keyring.backends.SecretService"})()
    chain = type("ChainerBackend", (), {"__module__": "keyring.backends.chainer"})()
    chain.backends = [object(), secure]
    monkeypatch.setattr(keyring, "get_keyring", lambda: chain)
    assert credentials.keyring_backend() is secure
    chain.backends = [object()]
    with pytest.raises(credentials.CredentialError):
        credentials.keyring_backend()


@pytest.mark.parametrize("adapter_type", [ChatAdapter, ResponsesAdapter, AnthropicAdapter])
def test_clients_resolve_once_off_thread_and_keep_key_only_in_memory(
    adapter_type: Any, model_keyring: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ep = replace(ENDPOINT, api="anthropic" if adapter_type is AnthropicAdapter else ENDPOINT.api)
    model_keyring.set_password(credentials.SERVICE, "ref", saved(ep))
    lookups: list[int] = []
    original = model_keyring.get_password

    def get(*args: Any) -> str | None:
        lookups.append(threading.get_ident())
        return original(*args)

    monkeypatch.setattr(model_keyring, "get_password", get)
    monkeypatch.setattr("openai.AsyncOpenAI", lambda **kw: SimpleNamespace(**kw))
    monkeypatch.setattr("anthropic.AsyncAnthropic", lambda **kw: SimpleNamespace(**kw))
    monkeypatch.setattr("openai.DefaultAsyncHttpxClient", lambda **kw: SimpleNamespace(**kw))
    monkeypatch.setattr("anthropic.DefaultAsyncHttpxClient", lambda **kw: SimpleNamespace(**kw))
    before = dict(os.environ)
    adapter = adapter_type(ep)
    assert not lookups

    async def client(a: Any) -> Any:
        return await a._client() if adapter_type is AnthropicAdapter else await openai_client(a)

    async def exercise() -> None:
        clients = await asyncio.gather(*(client(adapter) for _ in range(8)))
        assert all(c is clients[0] for c in clients)
        assert clients[0].api_key == SECRET
        assert len(lookups) == 1 and lookups[0] != threading.get_ident()
        model_keyring.records.clear()
        assert (await client(adapter)).api_key == SECRET  # restart required to discard cached key
        with pytest.raises(credentials.CredentialError, match="no saved credential"):
            await client(adapter_type(ep))
        model_keyring.set_password(credentials.SERVICE, "ref", saved(ep))
        assert (await client(adapter_type(ep))).api_key == SECRET

    asyncio.run(exercise())
    assert dict(os.environ) == before


@pytest.mark.parametrize("api", ["openai-chat", "openai-responses", "anthropic"])
def test_sdk_requests_use_saved_key_and_configured_url(
    api: str, model_keyring: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import anthropic
    import openai

    # Recent Anthropic releases use httpx2; keep the transport on the SDK's HTTP library.
    http = getattr(anthropic._base_client, "httpx2", httpx) if api == "anthropic" else httpx
    ep = replace(
        ENDPOINT,
        api=api,
        base_url=("https://models.example" if api == "anthropic" else ENDPOINT.base_url),
    )
    model_keyring.set_password(credentials.SERVICE, "ref", saved(ep))
    for name in ("OPENAI_BASE_URL", "ANTHROPIC_BASE_URL"):
        monkeypatch.setenv(name, "https://wrong.example/")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "wrong-token")
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return http.Response(200, json={})

    async def exercise() -> None:
        async with http.AsyncClient(transport=http.MockTransport(handle)) as transport:
            if api == "anthropic":
                monkeypatch.setattr(anthropic, "DefaultAsyncHttpxClient", lambda **kw: transport)
                client = await AnthropicAdapter(ep)._client()
                await client.messages.create(model="model", max_tokens=1, messages=[])
            else:
                monkeypatch.setattr(openai, "DefaultAsyncHttpxClient", lambda **kw: transport)
                client = await openai_client(ChatAdapter(ep))
                if api == "openai-chat":
                    await client.chat.completions.create(model="model", messages=[])
                else:
                    await client.responses.create(model="model", input="test")

    asyncio.run(exercise())
    (request,) = requests
    paths = {
        "openai-chat": "chat/completions",
        "openai-responses": "responses",
        "anthropic": "messages",
    }
    assert str(request.url) == "https://models.example/v1/" + paths[api]
    if api == "anthropic":
        assert request.headers["x-api-key"] == SECRET and "authorization" not in request.headers
    else:
        assert request.headers["authorization"] == f"Bearer {SECRET}"


@pytest.mark.parametrize("api", ["openai-chat", "openai-responses", "anthropic"])
@pytest.mark.parametrize("use_keyring", [True, False])
@pytest.mark.parametrize("redirect", ["https://models.example/other", "https://other.example/api"])
def test_keyring_clients_refuse_redirects_without_changing_env_clients(
    api: str,
    use_keyring: bool,
    redirect: str,
    model_keyring: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import anthropic
    import openai

    sdk = anthropic if api == "anthropic" else openai
    http = getattr(anthropic._base_client, "httpx2", httpx) if api == "anthropic" else httpx
    ep = replace(
        ENDPOINT,
        api=api,
        key_ref="ref" if use_keyring else None,
        key_env=None if use_keyring else "ATTACHE_TEST_KEY",
    )
    model_keyring.set_password(credentials.SERVICE, "ref", saved(ep))
    monkeypatch.setenv("ATTACHE_TEST_KEY", SECRET)
    requests: list[Any] = []

    def handle(request: Any) -> Any:
        requests.append(request)
        if len(requests) == 1:
            return http.Response(307, headers={"Location": redirect})
        return http.Response(200, json={})

    async def exercise() -> None:
        async with http.AsyncClient(
            transport=http.MockTransport(handle), follow_redirects=True
        ) as transport:

            def http_client(**options: Any) -> Any:
                transport.follow_redirects = options["follow_redirects"]
                return transport

            monkeypatch.setattr(sdk, "DefaultAsyncHttpxClient", http_client)
            cls = "AsyncAnthropic" if api == "anthropic" else "AsyncOpenAI"
            factory = getattr(sdk, cls)
            monkeypatch.setattr(sdk, cls, lambda **kw: factory(**{"http_client": transport, **kw}))
            client = (
                await AnthropicAdapter(ep)._client()
                if api == "anthropic"
                else await openai_client(ChatAdapter(ep))
            )

            async def request() -> Any:
                if api == "anthropic":
                    return await client.messages.create(model="model", max_tokens=1, messages=[])
                if api == "openai-chat":
                    return await client.chat.completions.create(model="model", messages=[])
                return await client.responses.create(model="model", input="test")

            if use_keyring:
                with pytest.raises(sdk.APIStatusError) as error:
                    await request()
                assert error.value.status_code == 307
            else:
                await request()

    asyncio.run(exercise())
    assert len(requests) == (1 if use_keyring else 2)


def test_doctor_checks_keyring_without_printing_key(
    home: Path, model_keyring: Any, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    (home / "attache.toml").write_text(
        '[routing]\nown_chats="custom"\ngroup_chats="custom"\n'
        '[endpoints.custom]\napi="openai-chat"\nbase_url="https://models.example/v1"\nkey_ref="ref"',
        encoding="utf-8",
    )
    monkeypatch.setattr(cli.shutil, "which", lambda _: "/fake/tool")
    assert cli.main(["doctor"]) == 1
    assert "no saved credential" in capsys.readouterr().out
    model_keyring.set_password(credentials.SERVICE, "ref", saved())
    assert cli.main(["doctor"]) == 0
    output = capsys.readouterr().out
    assert "keyring credential available" in output and SECRET not in output
