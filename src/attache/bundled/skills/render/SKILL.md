---
name: render
description: Turn a page (an artifact such as a board, list or checklist) into a PNG picture or
  a PDF, to send into a chat app. Use when someone in Slack, Signal or a group chat wants to
  see a page, or asks for a page as an image or a PDF.
metadata:
  attache-roles: "member"
---

# Render

Pages open only inside the Attaché window, so people in Slack, Signal and group chats cannot
follow a page link. Send them a picture of it instead.

## A page as a picture

1. Publish or update the page with the `artifact` tool. Its result has two links:
   `{"url": ..., "snapshot": ...}`. `url` is for the owner's Attaché window; `snapshot` is a
   one-page pass that lets the render browser open the page on this machine.
2. Render it:
   `script {skill: "render", script: "render.py", args: ["<snapshot>", "page.png"]}`.
   On success the output is the file's path, such as `files/slack-c0123/page.png`.
3. Send it: `send {text: "The packing list", files: ["files/slack-c0123/page.png"]}`, using
   the path the script printed.

Name files after what they show (`packing-list.png`); the same name replaces the last picture.
For a PDF, give `page.pdf` instead. For a phone-shaped picture add
`"--width", "390", "--height", "844"` to `args`; the default is 1280 x 900.

## Rules

- **Never send the `snapshot` link itself.** It holds the page's tokens and works only on
  this machine; send the picture.
- Snapshot links change every time Attaché restarts: take the `snapshot` from the latest
  `artifact` result, and render straight after publishing.
- The script only renders pages on this machine (`localhost` or `127.0.0.1`), and the browser
  cannot reach the internet while it renders. It writes only into this chat's files folder.
- It needs Microsoft Edge or Google Chrome. If the script says none was found, tell the person
  plainly and offer the list as text instead.
- If the picture looks empty, the page may still have been loading: add `"--wait", "8"`.
- A page made in a group chat shows only what is shared with that chat, so a picture of it is
  safe to send there. Never render the owner's own pages into a group.
