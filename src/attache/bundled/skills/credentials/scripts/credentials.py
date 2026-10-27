# /// script
# requires-python = ">=3.11"
# dependencies = ["keyring>=25,<26", "tomlkit>=0.13"]
# ///
"""Manage model credentials from the owner's terminal. No secrets in arguments or output."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import sys
import tempfile
import uuid
import warnings
from pathlib import Path
from typing import Any

import tomlkit

# uv isolates skill dependencies. Use the CLI's installed package for the credential contract.
if package_root := os.environ.get("ATTACHE_PACKAGE_ROOT"):
    sys.path.append(package_root)
try:
    from attache import credentials as store
    from attache.types import Endpoint
except ImportError:
    raise SystemExit(
        "Run this through: attache skill credentials login|status|logout ENDPOINT"
    ) from None


def read_config(home: Path, name: str) -> tuple[bytes, Any, Endpoint]:
    """Read an endpoint and the original bytes for a comment-preserving atomic update."""
    try:
        original = (home / "attache.toml").read_bytes()
        document = tomlkit.parse(original.decode("utf-8-sig"))
        table = document["endpoints"][name]
        endpoint = Endpoint(
            name,
            table["api"],
            table.get("model", ""),
            table.get("base_url"),
            table.get("key_env"),
            key_ref=table.get("key_ref"),
        )
    except Exception:
        raise store.CredentialError(
            "cannot read that endpoint from attache.toml; check the file and name"
        ) from None
    store.validate(endpoint)
    store.destination(endpoint.api, endpoint.base_url)
    return original, document, endpoint


def write_config(path: Path, original: bytes, document: Any) -> None:
    """Replace the configuration atomically, detecting edits made since it was read."""
    encoded = tomlkit.dumps(document).encode("utf-8")
    if original.startswith(b"\xef\xbb\xbf"):
        encoded = b"\xef\xbb\xbf" + encoded
    fd, name = tempfile.mkstemp(prefix=".attache-config-", suffix=".toml", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        shutil.copymode(path, temporary)
        if path.read_bytes() != original:
            raise store.CredentialError(
                "attache.toml changed during login; retry with the current file"
            )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def prompt_key() -> str:
    """Read hidden input, refusing getpass's fallback to echoed input."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            key = getpass.getpass("API key (hidden): ").strip()
        except (EOFError, getpass.GetPassWarning):
            raise store.CredentialError(
                "hidden input unavailable; run login in your own terminal"
            ) from None
    if not key:
        raise store.CredentialError("no key entered; nothing changed")
    return key


def login(home: Path, original: bytes, document: Any, endpoint: Endpoint) -> None:
    """Save a new record before changing its reference; preserve the old record on failure."""
    family, url = store.destination(endpoint.api, endpoint.base_url)
    print(f"Endpoint {endpoint.name}: {endpoint.api} at {url}")
    backend, reference = store.keyring_backend(), uuid.uuid4().hex
    record = json.dumps({"version": 1, "api_key": prompt_key(), "family": family, "base_url": url})
    try:
        backend.set_password(store.SERVICE, reference, record)
        if backend.get_password(store.SERVICE, reference) != record:
            raise store.CredentialError("credential could not be verified after saving")
        table = document["endpoints"][endpoint.name]
        table.pop("key_env", None)
        table["key_ref"] = reference
        write_config(home / "attache.toml", original, document)
    except (Exception, KeyboardInterrupt) as error:
        # A cancellation can arrive after replace() commits. Never delete a referenced key.
        try:
            current = tomlkit.parse((home / "attache.toml").read_text(encoding="utf-8-sig"))
            referenced = any(t.get("key_ref") == reference for t in current["endpoints"].values())
        except Exception:
            referenced = True  # cannot prove rollback is safe
        if referenced:
            raise store.CredentialError(
                "credential retained; check configuration/status and restart Attaché"
            ) from None
        try:
            if backend.get_password(store.SERVICE, reference) is not None:
                backend.delete_password(store.SERVICE, reference)
        except Exception:
            print(
                f"Remove unused credential {reference} from the {store.SERVICE} OS store.",
                file=sys.stderr,
            )
        message = (
            str(error)
            if isinstance(error, store.CredentialError)
            else ("could not save credential/configuration; previous setup retained")
        )
        raise store.CredentialError(message) from None
    # A manually shared reference may still belong to another configured endpoint.
    used = {table.get("key_ref") for table in document["endpoints"].values()}
    if endpoint.key_ref and endpoint.key_ref not in used:
        try:
            if backend.get_password(store.SERVICE, endpoint.key_ref) is not None:
                backend.delete_password(store.SERVICE, endpoint.key_ref)
        except Exception:
            print("Saved, but the previous OS credential could not be removed.", file=sys.stderr)
    print(f"Saved credential for {endpoint.name}. Restart Attaché to use it.")
    if endpoint.key_env:
        print(
            f"After checking the endpoint, remove {endpoint.key_env} from your startup environment."
        )


def logout(endpoint: Endpoint) -> None:
    """Delete the local record, retaining key_ref so future requests fail closed."""
    if not endpoint.key_ref:
        raise store.CredentialError(
            "this endpoint has no key_ref; its environment settings were not changed"
        )
    backend = store.keyring_backend()
    try:
        if backend.get_password(store.SERVICE, endpoint.key_ref) is not None:
            backend.delete_password(store.SERVICE, endpoint.key_ref)
    except Exception:
        raise store.CredentialError(
            "could not remove the credential; check the OS credential store"
        ) from None
    print(
        f"Removed local credential for {endpoint.name}. Restart Attaché to discard its cached key."
    )
    print("The provider's key is still valid until you revoke it with the provider.")


def main(argv: list[str] | None = None) -> int:
    """Login, inspect availability or logout; mutations require a private interactive terminal."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["login", "status", "logout"])
    parser.add_argument("endpoint")
    args = parser.parse_args(argv)
    home = Path(os.environ.get("ATTACHE_HOME") or Path.home() / ".attache").expanduser()
    try:
        if args.action != "status" and (
            not sys.stdin.isatty() or {"ATTACHE_ROLE", "ATTACHE_WEB_URL"} & set(os.environ)
        ):
            raise store.CredentialError(
                "run credential login/logout in your own interactive terminal"
            )
        original, document, endpoint = read_config(home, args.endpoint)
        if args.action == "login":
            login(home, original, document, endpoint)
        elif args.action == "logout":
            logout(endpoint)
        else:
            store.api_key(endpoint)
            source = (
                "keyring credential available"
                if endpoint.key_ref
                else (
                    f"environment variable {endpoint.key_env} available"
                    if endpoint.key_env
                    else "no explicit credential source"
                )
            )
            print(
                f"Endpoint {endpoint.name}: {source}. Provider authentication has not been tested."
            )
    except (store.CredentialError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
