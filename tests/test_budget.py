"""The core stays small. If this fails, the feature belongs in a skill.

Only lines of code count: blank lines, comments and docstrings are free, so the budget never
pushes code to be denser than it should be.
"""

from __future__ import annotations

import ast
import io
import tokenize
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "attache"
CORE_LIMIT = 2050  # 1,600 in phase 1, 1,900 in phase 2, then sharing and page turns in phase 3
SKILL_SCRIPT_LIMIT = 300
SKILL_LIMITS = {"mail": 1350}  # shared and IMAP ~600, Outlook COM and Graph ~700 (owner's call)


def code_lines(path: Path) -> int:
    source = path.read_text(encoding="utf-8")
    free: set[int] = set()  # lines inside docstrings
    for node in ast.walk(ast.parse(source)):
        body = getattr(node, "body", None)
        first = body[0] if isinstance(body, list) and body else None
        value = first.value if isinstance(first, ast.Expr) else None
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            free.update(range(first.lineno, first.end_lineno + 1))
    layout = {tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT}
    lines: set[int] = set()
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type not in layout | {tokenize.ENDMARKER}:
            lines.update(range(tok.start[0], tok.end[0] + 1))
    return len(lines - free)


def test_core_stays_small() -> None:
    files = [p for p in SRC.rglob("*.py") if "bundled" not in p.relative_to(SRC).parts]
    lines = sum(code_lines(p) for p in files)
    assert lines <= CORE_LIMIT, f"core is {lines} lines of code; move the feature into a skill"


def test_bundled_skill_scripts_stay_small() -> None:
    for skill in sorted((SRC / "bundled" / "skills").glob("*/")):
        lines = sum(code_lines(p) for p in skill.rglob("*.py"))
        limit = SKILL_LIMITS.get(skill.name, SKILL_SCRIPT_LIMIT)
        assert lines <= limit, f"{skill.name} scripts are {lines} lines of code (cap {limit})"


def test_counting_ignores_blanks_comments_and_docstrings(tmp_path: Path) -> None:
    sample = tmp_path / "m.py"
    lines = ['"""Doc."""', "", "# note", "x = 1  # trailing", "", "", "def f():"]
    lines += ['    """Doc', '    more."""', "    return x"]
    sample.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert code_lines(sample) == 3
