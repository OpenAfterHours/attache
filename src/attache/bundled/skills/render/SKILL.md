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
   one-page pass, valid for 10 minutes, that lets the render browser open the page on this
   machine. In a shared chat with sharing on, there is also `link`, the page's share link.
2. Render it:
   `script {skill: "render", script: "render.py", args: ["<snapshot>", "page.png"]}`.
   On success the output is the file's path in this chat's files folder, such as
   `files/slack-c0123-7a947790/page.png` (the folder ends in 8 hex digits).
3. Send it: `send {text: "The packing list", files: ["files/slack-c0123-7a947790/page.png"]}`,
   using exactly the path the script printed. If the result had a `link`, put it in the text:
   people can open the page from it for 7 days.

Name files after what they show (`packing-list.png`); the same name replaces the last picture.
For a PDF, give `page.pdf` instead. For a phone-shaped picture add
`"--width", "390", "--height", "844"` to `args`; the default is 1280 x 900, the most is 4000 x
4000, and `--wait` is at most 30 seconds.

## Rules

- **Never send the `snapshot` link itself.** It holds the page's tokens and works only on
  this machine; send the picture.
- A snapshot link works for 10 minutes, and not after Attaché restarts: render straight after
  publishing, with the `snapshot` from that `artifact` result. If the script says the page
  answered 403, publish the page again for a fresh link.
- The script renders only Attaché's own pages, from a `snapshot` link (no other address, local
  or not, and no files), and the browser cannot reach the internet while it renders. It writes
  only into this chat's files folder.
- It needs Microsoft Edge or Google Chrome. If the script says none was found, tell the person
  plainly and offer the list as text instead.
- If the picture looks empty, the page may still have been loading: add `"--wait", "8"`.
- A page made in a group chat shows only what is shared with that chat, so a picture of it is
  safe to send there. Never render the owner's own pages into a group.
