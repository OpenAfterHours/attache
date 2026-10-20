---
name: docx
description: Make a Word document (.docx), such as a report, letter, plan or minutes, with a
  title, headings, paragraphs, bullet and numbered lists, and tables. Use when someone asks
  for a Word document, a .docx, or a document to edit, print or attach.
metadata:
  attache-roles: "member"
---

# Word documents

1. Write the spec, a JSON object:

   ```json
   {"title": "Trip plan",
    "blocks": [
      {"heading": "Saturday"},
      {"paragraph": "We leave at 9 from the station."},
      {"bullets": ["Passports", "Chargers", ["phone", "camera"]]},
      {"heading": "Costs", "level": 2},
      {"table": [["Item", "Cost"], ["Hotel", "£300"], ["Train", "£84"]]},
      {"page_break": true},
      {"numbered": ["Book the hotel", "Pay the deposit"]}
    ]}
   ```

2. Make the file:
   `script {skill: "docx", script: "make.py", args: ["<the spec, as one line of JSON>", "trip-plan.docx"]}`.
   On success the output is the file's path, such as `files/slack-c0123-7a947790/trip-plan.docx`.
3. Send it: `send {text: "The trip plan", files: ["<that path>"]}`, using exactly the path the
   script printed.

## The spec

| Key | Meaning |
|---|---|
| `title` | Optional. The document's title, at the top in Word's Title style. |
| `blocks` | The content, in order. Each block has exactly one of the keys below. |
| `heading` | Text, with `level` 1 (the default) to 4. |
| `paragraph` | Text. |
| `bullets`, `numbered` | A list of strings. A list inside the list is indented, up to three levels. |
| `table` | A list of rows, each a list of cells. The first row is bold; `"header": false` turns that off. |
| `page_break` | `true`. |

Text is plain: Markdown such as `**bold**` shows as typed. The script refuses unknown keys and
names them: fix the spec and run it again.

## Rules

- The file goes into this chat's files folder and nowhere else. Give a plain name ending in
  `.docx`, named after what it holds; the same name replaces the last file.
- The first run downloads python-docx, which takes a few seconds.
- A spec longer than about 20,000 characters is too long for one argument. In the owner's own
  chat, write it to a `.json` file in this chat's files folder with `write_file`, and give that
  file's name instead of the JSON. Elsewhere, split the document or shorten it.
