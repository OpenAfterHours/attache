from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def no_real_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests must never prompt for, read, or modify the developer's credential store."""
    import keyring

    def forbidden() -> None:
        pytest.fail("inject a fake keyring backend; OS credential access is forbidden in tests")

    monkeypatch.setattr(keyring, "get_keyring", forbidden)


@pytest.fixture
def model_keyring(monkeypatch: pytest.MonkeyPatch) -> Any:
    """An in-memory store shared by runtime and credential-command tests."""
    from attache import credentials

    class MemoryKeyring:
        def __init__(self) -> None:
            self.records: dict[tuple[str, str], str] = {}

        def get_password(self, service: str, username: str) -> str | None:
            return self.records.get((service, username))

        def set_password(self, service: str, username: str, password: str) -> None:
            self.records[service, username] = password

        def delete_password(self, service: str, username: str) -> None:
            del self.records[service, username]

    backend = MemoryKeyring()
    monkeypatch.setattr(credentials, "keyring_backend", lambda: backend)
    return backend


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty Attaché home that $ATTACHE_HOME points at."""
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("ATTACHE_HOME", str(path))
    return path
