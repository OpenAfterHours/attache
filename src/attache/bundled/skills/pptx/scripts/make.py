# /// script
# requires-python = ">=3.11"
# dependencies = ["python-pptx>=1.0"]
# ///
"""Make a PowerPoint deck from a small JSON spec (SKILL.md describes it).

    make.py <spec> <out.pptx>

<spec> is the spec itself, as JSON text starting with "{", or the name of a .json file. Both
files must be in the current folder, the chat's files folder when the script tool runs this:
absolute paths, drives, ":", ".." and hidden names are refused, and so is anything that
resolves outside the folder (through a link, say). The output must end in .pptx. On success
the file's path relative to the Attaché home is printed, ready for `send`.

The deck uses python-pptx's default template (4:3). python-pptx is imported inside build(),
so the checks work (and are tested) without it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path, PureWindowsPath
from typing import Any

TOP_KEYS = {"title", "subtitle", "slides"}
SLIDE = {"title", "bullets", "table", "notes"}
TITLE, CONTENT, TITLE_ONLY = 0, 1, 5  # the default template's slide layouts
# The content area under the title on the default 10 x 7.5 inch slides, in inches
LEFT, TOP, WIDTH, BOTTOM = 0.5, 1.75, 9.0, 7.0
SPLIT = 2.25  # the bullets' height when a table shares the slide
ROW = 0.4  # a table row's height
TABLE_FONT = 14  # points: the template's 18 fits too few rows


def inside(name: str, suffix: str) -> Path:
    """`name` as a file in the current folder with the given suffix, or ValueError."""
    here = Path.cwd().resolve()
    windows = PureWindowsPath(name)  # splits on / and \ and sees drives, on every system
    hidden = any(part.startswith(".") for part in windows.parts)  # also ".."
    if not name or ":" in name or windows.anchor or hidden:
        raise ValueError(f"{name!r}: give a file name in this folder, such as slides{suffix}")
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
    """The path relative to the Attaché home (files/<chat>/slides.pptx), as `send` takes it."""
    home = os.environ.get("ATTACHE_HOME")
    if home and path.is_relative_to(Path(home).resolve()):
        return path.relative_to(Path(home).resolve()).as_posix()
    base = next((parent.parent for parent in path.parents if parent.name == "files"), None)
    return (path.relative_to(base) if base else path).as_posix()


def check_keys(found: dict[str, Any], allowed: set[str], where: str) -> None:
    if unknown := sorted(set(found) - allowed):
        raise ValueError(f"{where}: unknown key {', '.join(unknown)}; known: {sorted(allowed)}")


def items(values: Any, level: int = 0) -> Iterator[tuple[int, str]]:
    """(level, text) for each bullet; a list inside the list is one level deeper (at most
    three levels)."""
    if not isinstance(values, list):
        raise ValueError("bullets are a JSON array of strings (an array inside it is indented)")
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


def add_slide(deck: Any, spec: Any, number: int, inches: Any, points: Any) -> None:
    """One slide: the title, then bullets, a table, or bullets above a table; and notes."""
    if not isinstance(spec, dict):
        raise ValueError(f"slide {number}: a slide is an object with a title")
    check_keys(spec, SLIDE, f"slide {number}")
    bullets = list(items(spec.get("bullets", [])))
    rows = grid(spec["table"]) if "table" in spec else []
    slide = deck.slides.add_slide(deck.slide_layouts[CONTENT if bullets else TITLE_ONLY])
    slide.shapes.title.text = str(spec.get("title", ""))
    top = TOP
    if bullets:
        body = slide.placeholders[1]
        if rows:  # set all four: a placeholder given only some loses the rest
            body.left, body.top, body.width = inches(LEFT), inches(TOP), inches(WIDTH)
            body.height = inches(SPLIT)
            top = TOP + SPLIT + 0.25
        frame = body.text_frame
        for index, (level, text) in enumerate(bullets):
            paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
            paragraph.text, paragraph.level = text, level
    if rows:
        height = min(ROW * len(rows), BOTTOM - top)
        size = (inches(LEFT), inches(top), inches(WIDTH), inches(height))
        table = slide.shapes.add_table(len(rows), len(rows[0]), *size).table
        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                cell = table.cell(r, c)
                cell.text = value
                for paragraph in cell.text_frame.paragraphs:
                    for run in paragraph.runs:
                        run.font.size = points(TABLE_FONT)
    if notes := spec.get("notes"):
        slide.notes_slide.notes_text_frame.text = str(notes)


def build(spec: dict[str, Any]) -> Any:
    """The python-pptx Presentation for `spec`."""
    from pptx import Presentation
    from pptx.util import Inches, Pt

    check_keys(spec, TOP_KEYS, "the spec")
    slides = spec.get("slides", [])
    if not isinstance(slides, list):
        raise ValueError('"slides" is a list of slides')
    deck = Presentation()
    if title := spec.get("title"):
        deck.core_properties.title = str(title)
        cover = deck.slides.add_slide(deck.slide_layouts[TITLE])
        cover.shapes.title.text = str(title)
        cover.placeholders[1].text = str(spec.get("subtitle", ""))
    for number, slide in enumerate(slides, 1):
        add_slide(deck, slide, number, Inches, Pt)
    return deck


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Make a PowerPoint deck from a JSON spec.")
    parser.add_argument("spec", help='the spec as JSON text ("{...}"), or a .json file here')
    parser.add_argument("out", help="the .pptx file to write in the current folder")
    args = parser.parse_args(argv)
    try:
        out = inside(args.out, ".pptx")
        deck = build(load(args.spec))
        out.parent.mkdir(parents=True, exist_ok=True)
        deck.save(str(out))
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(shown(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
