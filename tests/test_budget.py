"""The core stays small. If this fails, the feature belongs in a skill."""

from __future__ import annotations

from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "attache"
CORE_LIMIT = 2000
SKILL_SCRIPT_LIMIT = 300


def _lines(paths: list[Path]) -> int:
    return sum(len(p.read_text(encoding="utf-8").splitlines()) for p in paths)


def test_core_stays_small() -> None:
    files = [p for p in SRC.rglob("*.py") if "bundled" not in p.relative_to(SRC).parts]
    lines = _lines(files)
    assert lines <= CORE_LIMIT, f"core is {lines} lines; move the feature into a skill"


def test_bundled_skill_scripts_stay_small() -> None:
    for skill in sorted((SRC / "bundled" / "skills").glob("*/")):
        lines = _lines(list(skill.rglob("*.py")))
        assert lines <= SKILL_SCRIPT_LIMIT, f"{skill.name} scripts are {lines} lines"
