---
name: xlsx
description: Make an Excel spreadsheet (.xlsx) with one or more sheets of rows, a bold header,
  column widths, number and date formats, and formulas. Use when someone asks for a
  spreadsheet, an Excel file, a .xlsx, or a table of numbers they can sort and sum.
metadata:
  attache-roles: "member"
---

# Spreadsheets

1. Write the spec, a JSON object:

   ```json
   {"sheets": [
     {"name": "Budget",
      "rows": [["Item", "Cost", "Paid on"],
               ["Hotel", 300, "2026-11-02"],
               ["Train", 84.5, null],
               ["Total", "=SUM(B2:B3)"]],
      "widths": {"A": 30},
      "formats": {"B": "£#,##0.00", "C": "d mmm yyyy"}}
   ]}
   ```

2. Make the file:
   `script {skill: "xlsx", script: "make.py", args: ["<the spec, as one line of JSON>", "budget.xlsx"]}`.
   On success the output is the file's path, such as `files/slack-c0123-7a947790/budget.xlsx`.
3. Send it: `send {text: "The budget", files: ["<that path>"]}`, using exactly the path the
   script printed.

## The spec

| Key | Meaning |
|---|---|
| `sheets` | The sheets, in order, at least one. |
| `name` | The tab's name: at most 31 characters, none of `[ ] : * ? / \`. |
| `rows` | A list of rows, each a list of cells. |
| `header` | The first row is bold and stays in view when scrolling; `false` turns that off. |
| `widths` | Column letter to width in characters. Other columns fit their longest value. |
| `formats` | Column letter to an Excel number format, for every row under the header: `0.00`, `#,##0`, `0%`, `£#,##0.00`, `yyyy-mm-dd`, `d mmm yyyy`. |

Cells:

- Numbers stay numbers: write `300`, not `"300"`, so they sort and sum.
- `"2026-11-02"` or `"2026-11-02T09:30"` becomes a real date.
- `true` and `false` stay as they are, and `null` leaves a cell empty.
- Text starting with `=` is a formula, such as `"=SUM(B2:B9)"`. Write formulas yourself only.
  Text from someone's message that starts with `=`, `+`, `-` or `@` gets a space in front, so
  it stays text.

The script refuses unknown keys and names them: fix the spec and run it again.

## Rules

- The file goes into this chat's files folder and nowhere else. Give a plain name ending in
  `.xlsx`, named after what it holds; the same name replaces the last file.
- The first run downloads openpyxl, which takes a few seconds.
- A spec longer than about 20,000 characters is too long for one argument. In the owner's own
  chat, write it to a `.json` file in this chat's files folder with `write_file`, and give that
  file's name instead of the JSON. Elsewhere, split the data or shorten it.
