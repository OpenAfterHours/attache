"""The docx, xlsx and pptx skills: their path checks, and how each spec maps to library calls.

python-docx, openpyxl and python-pptx are not in the dev environment (each script names its
own in a PEP 723 header), so fake modules stand in for them here and record what the scripts
ask for.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import types
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from attache import skills, tools

KINDS = {"docx": "python-docx", "xlsx": "openpyxl", "pptx": "python-pptx"}
CHAT = "slack-c0123-7a947790"  # a chat's files folder, as tools.chat_slug names them


def load(kind: str) -> ModuleType:
    path = skills.BUNDLED / kind / "scripts" / "make.py"
    spec = importlib.util.spec_from_file_location(f"attache_make_{kind}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # no fake installed yet: the library is imported later
    return module


@pytest.fixture
def chat(home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The working folder the script tool gives: <home>/files/<chat>/."""
    folder = home / "files" / CHAT
    folder.mkdir(parents=True)
    monkeypatch.chdir(folder)
    return folder


def saved(path: Any) -> None:
    Path(path).write_bytes(b"PK")  # what every fake's save() writes


# python-docx


class DocxCell:
    def __init__(self) -> None:
        self.text = ""
        self.paragraphs = [SimpleNamespace(runs=[SimpleNamespace(bold=None)])]


class DocxTable:
    def __init__(self, rows: int, cols: int) -> None:
        self.style = None
        self.cells = [[DocxCell() for _ in range(cols)] for _ in range(rows)]

    def cell(self, row: int, col: int) -> DocxCell:
        return self.cells[row][col]


class DocxDocument:
    def __init__(self) -> None:
        self.core_properties = SimpleNamespace(title=None)
        self.body: list[tuple[Any, ...]] = []

    def add_heading(self, text: str, level: int) -> None:
        self.body.append(("heading", level, text))

    def add_paragraph(self, text: str, style: str | None = None) -> None:
        self.body.append(("paragraph", style, text))

    def add_table(self, rows: int, cols: int) -> DocxTable:
        table = DocxTable(rows, cols)
        self.body.append(("table", table))
        return table

    def add_page_break(self) -> None:
        self.body.append(("page break",))

    def save(self, path: str) -> None:
        saved(path)


# openpyxl


def column_index_from_string(letters: str) -> int:
    if not re.fullmatch(r"[A-Z]{1,3}", letters):
        raise ValueError(f"{letters!r} is not a column")
    index = 0
    for letter in letters:
        index = index * 26 + ord(letter) - 64
    return index


def get_column_letter(index: int) -> str:
    letters = ""
    while index:
        index, rest = divmod(index - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


class XlsxCell:
    """Types its value as openpyxl does: text starting with "=" becomes a formula ("f")."""

    def __init__(self) -> None:
        self.font, self.number_format, self.quotePrefix = None, "General", False
        self._value, self.data_type = None, "n"

    @property
    def value(self) -> Any:
        return self._value

    @value.setter
    def value(self, value: Any) -> None:
        self._value = value
        if isinstance(value, str):
            self.data_type = "f" if len(value) > 1 and value.startswith("=") else "s"
        else:
            self.data_type = "d" if isinstance(value, date) else "n"


class Sheet:
    def __init__(self, title: str) -> None:
        self.title, self.cells, self.freeze_panes = title, {}, None
        self.column_dimensions = defaultdict(lambda: SimpleNamespace(width=None))

    def cell(self, row: int, column: int) -> XlsxCell:
        return self.cells.setdefault((row, column), XlsxCell())

    def values(self, row: int) -> list[Any]:
        return [cell.value for (r, _), cell in sorted(self.cells.items()) if r == row]


class Workbook:
    def __init__(self) -> None:
        self.active = Sheet("Sheet")
        self.sheets = [self.active]

    def remove(self, sheet: Sheet) -> None:
        self.sheets.remove(sheet)

    def create_sheet(self, title: str) -> Sheet:
        self.sheets.append(Sheet(title))
        return self.sheets[-1]

    def save(self, path: str) -> None:
        saved(path)


def font(**options: Any) -> SimpleNamespace:
    return SimpleNamespace(**options)


# python-pptx


class TextFrame:
    def __init__(self) -> None:
        self.paragraphs = [SimpleNamespace(text="", level=0)]

    def add_paragraph(self) -> SimpleNamespace:
        self.paragraphs.append(SimpleNamespace(text="", level=0))
        return self.paragraphs[-1]


class Placeholder:
    def __init__(self) -> None:
        self.text, self.text_frame = "", TextFrame()
        self.left = self.top = self.width = self.height = None


class PptxCell:
    def __init__(self) -> None:
        self.text = ""
        run = SimpleNamespace(font=SimpleNamespace(size=None))
        self.text_frame = SimpleNamespace(paragraphs=[SimpleNamespace(runs=[run])])


class Shapes:
    def __init__(self) -> None:
        self.title, self.tables = Placeholder(), []

    def add_table(self, rows: int, cols: int, *box: Any) -> SimpleNamespace:
        cells = [[PptxCell() for _ in range(cols)] for _ in range(rows)]
        self.tables.append((cells, box))
        return SimpleNamespace(table=SimpleNamespace(cell=lambda r, c: cells[r][c]))


class Slide:
    def __init__(self, layout: int) -> None:
        self.layout, self.shapes, self.placeholders = layout, Shapes(), {1: Placeholder()}
        self.notes_slide = SimpleNamespace(notes_text_frame=SimpleNamespace(text=""))


class Slides(list):
    def add_slide(self, layout: int) -> Slide:
        self.append(Slide(layout))
        return self[-1]


class Presentation:
    def __init__(self) -> None:
        self.slide_layouts = list(range(11))  # a layout is its own index here
        self.slides, self.core_properties = Slides(), SimpleNamespace(title=None)

    def save(self, path: str) -> None:
        saved(path)


def inches(value: float) -> tuple[str, float]:
    return ("in", value)


def points(value: float) -> tuple[str, float]:
    return ("pt", value)


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake python-docx, openpyxl and python-pptx, for the scripts' imports inside build()."""
    attributes = {
        "docx": {"Document": DocxDocument},
        "openpyxl": {"Workbook": Workbook},
        "openpyxl.styles": {"Font": font},
        "openpyxl.utils": {
            "column_index_from_string": column_index_from_string,
            "get_column_letter": get_column_letter,
        },
        "pptx": {"Presentation": Presentation},
        "pptx.util": {"Inches": inches, "Pt": points},
    }
    for name, values in attributes.items():
        module = types.ModuleType(name)
        for key, value in values.items():
            setattr(module, key, value)
        monkeypatch.setitem(sys.modules, name, module)


# The skills themselves


@pytest.mark.parametrize("kind", KINDS)
def test_member_skills_with_a_pep_723_script(kind: str) -> None:
    folder = skills.BUNDLED / kind
    meta, body = skills.parse((folder / "SKILL.md").read_text(encoding="utf-8"))
    skill = skills.Skill(kind, meta["description"], folder, meta)
    assert tools.for_members(skill)  # group chats may ask for documents too
    for step in (f'script {{skill: "{kind}", script: "make.py"', "send {", f".{kind}"):
        assert step in body, step
    script = (folder / "scripts" / "make.py").read_text(encoding="utf-8")
    header = re.search(r"# /// script\n(.*?)# ///", script, re.S)
    assert header and f'"{KINDS[kind]}>=' in header[1]
    assert [p.name for p in (folder / "scripts").glob("*.py")] == ["make.py"]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    "name",
    [
        "",
        "/etc/report.{kind}",
        "\\\\server\\share\\report.{kind}",
        "C:\\Users\\you\\report.{kind}",
        "C:report.{kind}",  # relative to the current folder on drive C
        "report.{kind}:hidden",  # an NTFS stream
        "../report.{kind}",
        "..\\report.{kind}",
        "sub/../../report.{kind}",
        ".report.{kind}",
        "sub/.git/report.{kind}",
        "report.txt",
        "report.{kind}.txt",
        "report",
    ],
)
def test_paths_stay_in_the_working_folder(kind: str, name: str, chat: Path) -> None:
    make = load(kind)
    with pytest.raises(ValueError):
        make.inside(name.format(kind=kind), f".{kind}")
    assert make.inside(f"Report.{kind.upper()}", f".{kind}") == chat / f"Report.{kind.upper()}"
    assert make.inside(f"sub/report.{kind}", f".{kind}") == chat / "sub" / f"report.{kind}"
    assert make.inside(f"./report.{kind}", f".{kind}") == chat / f"report.{kind}"


@pytest.mark.parametrize("kind", KINDS)
def test_links_out_of_the_folder_are_refused(kind: str, chat: Path, tmp_path: Path) -> None:
    outside, link = tmp_path / "outside", chat / "out"
    outside.mkdir()
    try:
        os.symlink(outside, link, target_is_directory=True)
    except OSError:  # Windows without the right to make symbolic links: a junction needs none
        if os.name == "nt":
            junction = ["cmd", "/c", "mklink", "/J", str(link), str(outside)]
            subprocess.run(junction, capture_output=True, check=False)
        if not link.exists():
            pytest.skip("cannot make a link here")
    with pytest.raises(ValueError, match="outside"):
        load(kind).inside(f"out/report.{kind}", f".{kind}")


def test_json_values_are_data_never_paths(chat: Path, fakes: None, capsys: Any) -> None:
    writer = (skills.BUNDLED / "skill-writer" / "SKILL.md").read_text(encoding="utf-8")
    assert "as data, never as a path" in " ".join(writer.split())  # the rule for member skills
    spec = {"title": "../../evil.docx", "blocks": [{"paragraph": "C:\\Windows\\win.ini"}]}
    assert load("docx").main([json.dumps(spec), "plan.docx"]) == 0
    assert capsys.readouterr().out.strip() == f"files/{CHAT}/plan.docx"
    assert [p.name for p in chat.parent.parent.rglob("*.docx")] == ["plan.docx"]


@pytest.mark.parametrize("kind", KINDS)
def test_main_writes_into_the_chat_folder_and_prints_the_path_for_send(
    kind: str, chat: Path, fakes: None, capsys: Any
) -> None:
    make = load(kind)
    spec = {"docx": {"title": "Plan"}, "xlsx": {"sheets": [{"rows": [[1]]}]}, "pptx": {}}[kind]
    assert make.main([json.dumps(spec), f"plan.{kind}"]) == 0
    assert capsys.readouterr().out.strip() == f"files/{CHAT}/plan.{kind}"
    assert (chat / f"plan.{kind}").read_bytes() == b"PK"
    (chat / "spec.json").write_text(json.dumps(spec), encoding="utf-8")  # or from a file
    assert make.main(["spec.json", f"again/plan.{kind}"]) == 0
    assert capsys.readouterr().out.strip() == f"files/{CHAT}/again/plan.{kind}"


@pytest.mark.parametrize("kind", KINDS)
def test_main_refuses_with_one_line(kind: str, chat: Path, fakes: None, capsys: Any) -> None:
    make = load(kind)
    (chat.parent / "spec.json").write_text("{}", encoding="utf-8")
    for args, says in (
        (["{}", f"../plan.{kind}"], "give a file name"),
        (["{}", "plan.html"], f"must end in .{kind}"),
        (["../spec.json", f"plan.{kind}"], "give a file name"),
        (["missing.json", f"plan.{kind}"], "missing.json"),
        (["{not json", f"plan.{kind}"], "error"),
        (["[1, 2]", f"plan.{kind}"], "must end in .json"),  # not an object: a file name
        (['{"colour": "red"}', f"plan.{kind}"], "unknown key colour"),
    ):
        assert make.main(args) == 1, args
        err = capsys.readouterr().err
        assert err.startswith("error: ") and says in err, (args, err)
    assert list(chat.iterdir()) == []


def test_main_prints_a_home_relative_path_without_attache_home(
    chat: Path, fakes: None, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.delenv("ATTACHE_HOME")
    assert load("docx").main(['{"title": "x"}', "x.docx"]) == 0
    assert capsys.readouterr().out.strip() == f"files/{CHAT}/x.docx"  # found by its files/


# Spec to calls


def test_docx_spec(fakes: None) -> None:
    spec = {
        "title": "Trip plan",
        "blocks": [
            {"heading": "Saturday"},
            {"paragraph": "We leave at 9."},
            {"bullets": ["Passports", ["phone", ["charger"]], "Tickets"]},
            {"heading": "Costs", "level": 2},
            {"table": [["Item", "Cost"], ["Hotel", 300], ["Train"]]},
            {"page_break": True},
            {"numbered": ["Book", "Pay"]},
            {"table": [["a", None]], "header": False},
        ],
    }
    document = load("docx").build(spec)
    assert document.core_properties.title == "Trip plan"
    tables = [part[1] for part in document.body if part[0] == "table"]
    body = [part if part[0] != "table" else ("table",) for part in document.body]
    assert body == [
        ("heading", 0, "Trip plan"),
        ("heading", 1, "Saturday"),
        ("paragraph", None, "We leave at 9."),
        ("paragraph", "List Bullet", "Passports"),
        ("paragraph", "List Bullet 2", "phone"),
        ("paragraph", "List Bullet 3", "charger"),
        ("paragraph", "List Bullet", "Tickets"),
        ("heading", 2, "Costs"),
        ("table",),
        ("page break",),
        ("paragraph", "List Number", "Book"),
        ("paragraph", "List Number", "Pay"),
        ("table",),
    ]
    costs, plain = tables
    assert costs.style == "Table Grid"
    assert [[cell.text for cell in row] for row in costs.cells] == [
        ["Item", "Cost"],
        ["Hotel", "300"],
        ["Train", ""],  # short rows are filled out
    ]
    bold = [[cell.paragraphs[0].runs[0].bold for cell in row] for row in costs.cells]
    assert bold == [[True, True], [None, None], [None, None]]  # the header row
    assert [cell.text for cell in plain.cells[0]] == ["a", ""]
    assert plain.cells[0][0].paragraphs[0].runs[0].bold is None


@pytest.mark.parametrize(
    "spec",
    [
        {"blocks": [{"heading": "x", "paragraph": "y"}]},  # one kind a block
        {"blocks": [{"text": "x"}]},
        {"blocks": [{"heading": "x", "level": 7}]},
        {"blocks": [{"paragraph": "x", "style": "Fancy"}]},
        {"blocks": [{"bullets": "not a list"}]},
        {"blocks": [{"table": []}]},
        {"blocks": [{"table": ["not a row"]}]},
        {"blocks": "not a list"},
    ],
)
def test_docx_refuses_bad_specs(spec: dict[str, Any], fakes: None) -> None:
    with pytest.raises(ValueError):
        load("docx").build(spec)


def test_xlsx_spec(fakes: None) -> None:
    spec = {
        "sheets": [
            {
                "name": "Budget",
                "rows": [
                    ["Item", "Cost", "Paid on"],
                    ["Hotel", 300, "2026-11-02"],
                    ["Train", 84.5, "2026-11-02T09:30"],
                    ["A rather long description", {"formula": "=SUM(B2:B3)"}, None, True],
                ],
                "widths": {"a": 12},
                "formats": {"B": "£#,##0.00", "C": "d mmm yyyy"},
            },
            {"rows": [["x"]], "header": False},
        ]
    }
    book = load("xlsx").build(spec)
    budget, second = book.sheets  # the default empty sheet is gone
    assert (budget.title, second.title) == ("Budget", "Sheet2")
    assert budget.values(2) == ["Hotel", 300, date(2026, 11, 2)]
    assert budget.values(3)[2] == datetime(2026, 11, 2, 9, 30)
    assert budget.values(4) == ["A rather long description", "=SUM(B2:B3)", None, True]
    assert budget.cells[(4, 2)].data_type == "f"  # the one way to a formula
    assert [budget.cells[(1, c)].font for c in (1, 2, 3)] == [SimpleNamespace(bold=True)] * 3
    assert budget.freeze_panes == "A2"
    widths = {letter: d.width for letter, d in budget.column_dimensions.items()}
    assert widths == {"A": 12.0, "B": 8.0, "C": 18.0, "D": 8.0}  # A given; others fitted
    assert [budget.cells[(r, 2)].number_format for r in (2, 3, 4)] == ["£#,##0.00"] * 3
    assert budget.cells[(2, 3)].number_format == "d mmm yyyy"
    assert budget.cells[(1, 2)].number_format == "General"  # formats skip the header
    assert second.freeze_panes is None and second.cells[(1, 1)].font is None  # no header


def test_xlsx_text_never_becomes_a_formula(fakes: None) -> None:
    risky = ['=HYPERLINK("http://example.com","click")', '=WEBSERVICE("http://x")', "+1", "@a"]
    row = [*risky, "-2", "plain", {"formula": "SUM(A1:A2)"}]
    sheet = load("xlsx").build({"sheets": [{"rows": [row], "header": False}]}).sheets[0]
    cells = [sheet.cells[(1, column)] for column in range(1, len(row) + 1)]
    assert sheet.values(1)[:6] == [*risky, "-2", "plain"]  # shown exactly as written
    assert [(c.data_type, c.quotePrefix) for c in cells[:5]] == [("s", True)] * 5
    assert (cells[5].data_type, cells[5].quotePrefix) == ("s", False)
    assert (cells[6].value, cells[6].data_type, cells[6].quotePrefix) == ("=SUM(A1:A2)", "f", False)


def test_xlsx_fitted_widths_stay_between_8_and_60(fakes: None) -> None:
    rows = [["x" * 200, "y"]]
    sheet = load("xlsx").build({"sheets": [{"rows": rows}]}).sheets[0]
    assert (sheet.column_dimensions["A"].width, sheet.column_dimensions["B"].width) == (60, 8)


@pytest.mark.parametrize(
    "spec",
    [
        {},
        {"sheets": []},
        {"sheets": [{"rows": [["x"]], "colour": "red"}]},
        {"sheets": [{"rows": "x"}]},
        {"sheets": [{"rows": [[{"nested": 1}]]}]},
        {"sheets": [{"rows": [[{"formula": 1}]]}]},
        {"sheets": [{"rows": [[{"formula": "="}]]}]},
        {"sheets": [{"rows": [[{"formula": "=A1", "note": "x"}]]}]},
        {"sheets": [{"rows": [[["a list"]]]}]},
        {"sheets": [{"rows": [["2026-13-45"]]}]},  # not a date
        {"sheets": [{"rows": [["x"]], "widths": {"1": 10}}]},
        {"sheets": [{"rows": [["x"]], "formats": ["0.00"]}]},
        {"sheets": ["not a sheet"]},
    ],
)
def test_xlsx_refuses_bad_specs(spec: dict[str, Any], fakes: None) -> None:
    with pytest.raises(ValueError):
        load("xlsx").build(spec)


def test_pptx_spec(fakes: None) -> None:
    make = load("pptx")
    spec = {
        "title": "Q3 review",
        "subtitle": "October 2026",
        "slides": [
            {"title": "Highlights", "bullets": ["Up 12%", ["North", "South"]], "notes": "Thanks"},
            {"title": "By region", "table": [["Region", "Sales"], ["North", 120], ["South"]]},
            {"title": "Next", "bullets": ["Hire two"], "table": [["Role"], ["Designer"]]},
            {"title": "Questions?"},
        ],
    }
    deck = make.build(spec)
    cover, highlights, regions, both, last = deck.slides
    assert deck.core_properties.title == "Q3 review"
    assert (cover.layout, cover.shapes.title.text, cover.placeholders[1].text) == (
        0,
        "Q3 review",
        "October 2026",
    )
    assert highlights.layout == 1 and highlights.shapes.tables == []
    lines = [(p.text, p.level) for p in highlights.placeholders[1].text_frame.paragraphs]
    assert lines == [("Up 12%", 0), ("North", 1), ("South", 1)]
    assert highlights.notes_slide.notes_text_frame.text == "Thanks"
    assert highlights.placeholders[1].height is None  # bullets alone keep the layout's box

    assert regions.layout == 5  # title only, then the table under it
    (cells, box), *_ = regions.shapes.tables
    assert [[cell.text for cell in row] for row in cells] == [
        ["Region", "Sales"],
        ["North", "120"],
        ["South", ""],
    ]
    assert box == (("in", 0.5), ("in", 1.75), ("in", 9.0), ("in", pytest.approx(1.2)))
    sizes = {run.font.size for row in cells for c in row for run in c.text_frame.paragraphs[0].runs}
    assert sizes == {("pt", 14)}

    body = both.placeholders[1]
    assert both.layout == 1  # bullets above, the table below
    assert (body.left, body.top, body.width, body.height) == (
        ("in", 0.5),
        ("in", 1.75),
        ("in", 9.0),
        ("in", 2.25),
    )
    (_, box), *_ = both.shapes.tables
    assert box[1] == ("in", 4.25)
    assert (last.layout, last.shapes.title.text, last.shapes.tables) == (5, "Questions?", [])


def test_pptx_without_a_title_has_no_cover(fakes: None) -> None:
    deck = load("pptx").build({"slides": [{"title": "Only"}]})
    assert [slide.shapes.title.text for slide in deck.slides] == ["Only"]


@pytest.mark.parametrize(
    "spec",
    [
        {"slides": "x"},
        {"slides": ["not a slide"]},
        {"slides": [{"title": "x", "image": "y.png"}]},
        {"slides": [{"title": "x", "bullets": "not a list"}]},
        {"slides": [{"title": "x", "table": [["a"], "b"]}]},
    ],
)
def test_pptx_refuses_bad_specs(spec: dict[str, Any], fakes: None) -> None:
    with pytest.raises(ValueError):
        load("pptx").build(spec)
