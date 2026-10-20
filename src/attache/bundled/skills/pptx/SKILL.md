---
name: pptx
description: Make a PowerPoint deck (.pptx) - a title slide, then slides with a title, bullets,
  a table or both, and speaker notes. Use when someone asks for slides, a presentation, a deck,
  a PowerPoint or a .pptx.
metadata:
  attache-roles: "member"
---

# Slides

1. Write the spec, a JSON object:

   ```json
   {"title": "Q3 review", "subtitle": "October 2026",
    "slides": [
      {"title": "Highlights", "bullets": ["Sales up 12%", ["North led", "South flat"]],
       "notes": "Thank the north team."},
      {"title": "By region", "table": [["Region", "Sales"], ["North", "£120k"], ["South", "£80k"]]},
      {"title": "Next quarter", "bullets": ["Hire two people"],
       "table": [["Role", "When"], ["Designer", "November"]]}
    ]}
   ```

2. Make the file:
   `script {skill: "pptx", script: "make.py", args: ["<the spec, as one line of JSON>", "q3-review.pptx"]}`.
   On success the output is the file's path, such as `files/slack-c0123-7a947790/q3-review.pptx`.
3. Send it: `send {text: "The Q3 slides", files: ["<that path>"]}`, using exactly the path the
   script printed.

## The spec

| Key | Meaning |
|---|---|
| `title`, `subtitle` | Optional. A title slide first. |
| `slides` | The slides, in order. Each has the keys below. |
| `title` | The slide's title. |
| `bullets` | A list of strings. A list inside the list is indented, up to three levels. |
| `table` | A list of rows, each a list of cells; the first row is the header. |
| `notes` | Optional speaker notes. |

Bullets fill the slide, and a table sits under the title; with both, the bullets go above the
table. Keep to about six bullets or eight table rows a slide, and split anything longer across
slides. Text is plain: Markdown such as `**bold**` shows as typed. The script refuses unknown
keys and names them: fix the spec and run it again.

## Rules

- The file goes into this chat's files folder and nowhere else. Give a plain name ending in
  `.pptx`, named after what it holds; the same name replaces the last file.
- The first run downloads python-pptx, which takes a few seconds.
- A spec longer than about 20,000 characters is too long for one argument. In the owner's own
  chat, write it to a `.json` file in this chat's files folder with `write_file`, and give that
  file's name instead of the JSON. Elsewhere, make fewer slides.
