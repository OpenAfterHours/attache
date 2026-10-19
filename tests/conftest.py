from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty Attaché home that $ATTACHE_HOME points at."""
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("ATTACHE_HOME", str(path))
    return path
