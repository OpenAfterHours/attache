"""Model credentials: explicit environment variables or destination-bound OS keyring records."""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from attache.types import Endpoint

SERVICE = "attache-models"
DEFAULTS = {"openai": "https://api.openai.com/v1/", "anthropic": "https://api.anthropic.com/"}
BACKENDS = {
    "keyring.backends.Windows.WinVaultKeyring",
    "keyring.backends.macOS.Keyring",
    "keyring.backends.SecretService.Keyring",
    "keyring.backends.kwallet.DBusKeyring",
    "keyring.backends.kwallet.DBusKeyringKWallet4",
}


class CredentialError(RuntimeError):
    """An actionable credential error that contains no secret or raw backend exception."""


def destination(api: str, base_url: str | None) -> tuple[str, str]:
    """The API family and normalized destination, independent of ambient SDK variables."""
    family = "openai" if api in ("openai-chat", "openai-responses") else api
    if family not in DEFAULTS:
        raise CredentialError("keyring supports openai-chat, openai-responses and anthropic")
    raw = DEFAULTS[family] if base_url is None else base_url
    try:
        if not isinstance(raw, str) or any(c.isspace() or ord(c) < 32 for c in raw):
            raise ValueError
        url = urlsplit(raw)
        if url.scheme not in ("http", "https") or not url.hostname or url.username is not None:
            raise ValueError
        if url.fragment:
            raise ValueError
        host = url.hostname.encode("idna").decode("ascii").lower()
        host = f"[{host}]" if ":" in host else host
        port = url.port
        if port is not None and port != {"http": 80, "https": 443}[url.scheme]:
            host += f":{port}"
        return family, urlunsplit((url.scheme, host, url.path.rstrip("/") + "/", url.query, ""))
    except (ValueError, UnicodeError):
        raise CredentialError(
            "keyring needs a valid HTTP(S) base_url without user info or fragment"
        ) from None


def validate(endpoint: Endpoint) -> None:
    """Validate an explicit keyring source without accessing the credential store."""
    if endpoint.key_ref is None:
        return
    if not isinstance(endpoint.key_ref, str) or not endpoint.key_ref.strip():
        raise CredentialError(f"endpoint '{endpoint.name}': key_ref must be a nonempty string")
    if endpoint.key_env is not None:
        raise CredentialError(f"endpoint '{endpoint.name}': choose key_ref or key_env, not both")
    destination(endpoint.api, endpoint.base_url)


def keyring_backend() -> Any:
    """Choose a supported OS backend; never fall back to a plaintext or null backend."""
    try:
        import keyring

        backend = keyring.get_keyring()
        kind = f"{type(backend).__module__}.{type(backend).__name__}"
        candidates = (
            backend.backends if kind == "keyring.backends.chainer.ChainerBackend" else [backend]
        )
        for candidate in candidates:
            if f"{type(candidate).__module__}.{type(candidate).__name__}" in BACKENDS:
                return candidate
    except Exception:
        raise CredentialError(
            "OS credential store unavailable or locked; unlock it or use key_env"
        ) from None
    raise CredentialError(
        "no supported OS credential store; configure key_env for this installation"
    )


def api_key(endpoint: Endpoint) -> str | None:
    """Read the selected source, preserving key_env's existing missing-variable behavior."""
    validate(endpoint)
    if endpoint.key_ref is None:
        env = endpoint.key_env
        if env and not os.environ.get(env):
            raise RuntimeError(f"endpoint '{endpoint.name}' needs the {env} environment variable")
        return os.environ[env] if env else None
    backend = keyring_backend()
    try:
        raw = backend.get_password(SERVICE, endpoint.key_ref)
    except Exception:
        raise CredentialError(
            f"endpoint '{endpoint.name}': credential store unavailable or locked"
        ) from None
    if raw is None:
        raise CredentialError(
            f"endpoint '{endpoint.name}': no saved credential; "
            f"run attache skill credentials login {endpoint.name}"
        )
    try:
        record = json.loads(raw)
        if (
            not isinstance(record, dict)
            or type(record.get("version")) is not int
            or record["version"] != 1
        ):
            raise ValueError
        key = record["api_key"]
        if not isinstance(key, str) or not key.strip():
            raise ValueError
        saved = record["family"], record["base_url"]
    except (ValueError, TypeError, KeyError):
        raise CredentialError(
            f"endpoint '{endpoint.name}': invalid saved credential; run login again"
        ) from None
    if saved != destination(endpoint.api, endpoint.base_url):
        raise CredentialError(
            f"endpoint '{endpoint.name}': saved credential belongs to a different API URL or family"
        )
    return key


def client_options(endpoint: Endpoint) -> dict[str, Any]:
    """SDK constructor arguments; keyring keys never enter the process environment."""
    key = api_key(endpoint)
    base_url = (
        destination(endpoint.api, endpoint.base_url)[1] if endpoint.key_ref else endpoint.base_url
    )
    return {"api_key": key, "base_url": base_url}
