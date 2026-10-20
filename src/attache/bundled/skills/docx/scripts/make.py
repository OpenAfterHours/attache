# /// script
# requires-python = ">=3.11"
# dependencies = ["python-docx>=1.1"]
# ///
"""Make a Word document from a small JSON spec (SKILL.md describes it).

    make.py <spec> <out.docx>

<spec> is the spec itself, as JSON text starting with "{", or the name of a .json file. Both
files must be in the current folder, the chat's files folder when the script tool runs this:
absolute paths, drives, ":", ".." and hidden names are refused, and so is anything that
resolves outside the folder (through a link, say). The output must end in .docx. On success
the file's path relative to the Attaché home is printed, ready for `send`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path, PureWindowsPath
from typing import Any

# Each block has one of these keys, plus the options listed for it.
OPTIONS = {
    "heading": {"level"},
    "paragraph": set(),
    "bullets": set(),
    "numbered": set(),
    "table": {"header"},
    "page_break": set(),
}
TOP = {"title", "blocks"}


def inside(name: str, suffix: str) -> Path:
    """`name` as a file in the current folder with the given suffix, or ValueError."""
    here = Path.cwd().resolve()
    windows = PureWindowsPath(name)  # splits on / and \ and sees drives, on every system
    hidden = any(part.startswith(".") for part in windows.parts)  # also ".."
    if not name or ":" in name or windows.anchor or hidden:
        raise ValueError(f"{name!r}: give a file name in this folder, such as report{suffix}")
    path = here.joinpath(*windows.parts).resolve()
    if not path.is_relative_to(here):
        raise ValueError(f"{name!r} is outside this folder")
    if path.suffix.lower() != suffix:
        raise ValueError(f"{name!r}: the file must end in {suffix}")
    return path


def load(spec: str) -> dict[str, Any]:
    """The spec: JSON text, or the name of a .json file in the current folder."""
    if spec.lstrip().startswith("{"):
        text = spec
    else:
        text = inside(spec, ".json").read_text(encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("the spec must be a JSON object")
    return data


def shown(path: Path) -> str:
    """The path relative to the Attaché home (files/<chat>/report.docx), as `send` takes it."""
    home = os.environ.get("ATTACHE_HOME")
    if home and path.is_relative_to(Path(home).resolve()):
        return path.relative_to(Path(home).resolve()).as_posix()
    base = next((parent.parent for parent in path.parents if parent.name == "files"), None)
    return (path.relative_to(base) if base else path).as_posix()


def check_keys(found: dict[str, Any], allowed: set[str], where: str) -> None:
    if unknown := sorted(set(found) - allowed):
        raise ValueError(f"{where}: unknown key {', '.join(unknown)}; known: {sorted(allowed)}")


def kind_of(block: Any, number: int) -> str:
    """The block's one kind (heading, paragraph, ...), checking its other keys."""
    kinds = [key for key in block if key in OPTIONS] if isinstance(block, dict) else []
    if len(kinds) != 1:
        raise ValueError(f"block {number}: give exactly one of {', '.join(OPTIONS)}")
    check_keys(block, {kinds[0], *OPTIONS[kinds[0]]}, f"block {number}")
    return kinds[0]


def items(values: Any, level: int = 0) -> Iterator[tuple[int, str]]:
    """(level, text) for each list item; a list inside the list is one level deeper (at most
    three levels, as Word's list styles go)."""
    if not isinstance(values, list):
        raise ValueError("a list is a JSON array of strings (an array inside it is indented)")
    for value in values:
        if isinstance(value, list):
            yield from items(value, min(level + 1, 2))
        else:
            yield level, str(value)


def grid(rows: Any) -> list[list[str]]:
    """A table's rows as text, all as wide as the widest row."""
    if not isinstance(rows, list) or not rows or not all(isinstance(r, list) for r in rows):
        raise ValueError("a table is a list of rows, each a list of cells")
    width = max(len(row) for row in rows)
    return [["" if v is None else str(v) for v in row] + [""] * (width - len(row)) for row in rows]


def add_table(document: Any, rows: list[list[str]], header: bool) -> None:
    table = document.add_table(rows=len(rows), cols=len(rows[0]))
    table.style = "Table Grid"
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            cell = table.cell(r, c)
            cell.text = value
            if header and r == 0:
                for run in cell.paragraphs[0].runs:
                    run.bold = True


def build(spec: dict[str, Any]) -> Any:
    """The python-docx Document for `spec`."""
    import docx  # here, so the checks above work (and are tested) without python-docx

    check_keys(spec, TOP, "the spec")
    blocks = spec.get("blocks", [])
    if not isinstance(blocks, list):
        raise ValueError('"blocks" is a list of blocks')
    document = docx.Document()
    if title := spec.get("title"):
        document.core_properties.title = str(title)
        document.add_heading(str(title), level=0)  # level 0 is Word's Title style
    for number, block in enumerate(blocks, 1):
        kind = kind_of(block, number)
        value = block[kind]
        if kind == "heading":
            level = block.get("level", 1)
            if level not in (1, 2, 3, 4):
                raise ValueError(f"block {number}: a heading level is 1 to 4")
            document.add_heading(str(value), level=level)
        elif kind == "paragraph":
            document.add_paragraph(str(value))
        elif kind in ("bullets", "numbered"):
            style = "List Bullet" if kind == "bullets" else "List Number"
            for level, text in items(value):
                document.add_paragraph(text, style=style + (f" {level + 1}" if level else ""))
        elif kind == "table":
            add_table(document, grid(value), block.get("header", True))
        elif value:
            document.add_page_break()
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Make a Word document from a JSON spec.")
    parser.add_argument("spec", help='the spec as JSON text ("{...}"), or a .json file here')
    parser.add_argument("out", help="the .docx file to write in the current folder")
    args = parser.parse_args(argv)
    try:
        out = inside(args.out, ".docx")
        document = build(load(args.spec))
        out.parent.mkdir(parents=True, exist_ok=True)
        document.save(str(out))
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(shown(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
