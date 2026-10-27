---
icon: lucide/globe
---

# Web

The `web` skill lets the assistant look things up online: search the web, read the pages it
finds, and follow their links until it has the answer. Ask "what time does the museum open on
Sunday?" or "check the council's bin collection page", and it searches, reads the most
promising pages, and answers with the addresses it used, so you can check them.

It is a member skill, so people in your group chats can ask too. There is nothing to set
up: `attache init` installs it, and it needs no account or key.

## What it does

The assistant runs one script, `skills/web/scripts/web.py`, with the `script` tool:

| Command | What comes back |
|---|---|
| `search <words>` | Up to 10 results, each a title, its address and a snippet |
| `read <address>` | The page's title and text, with each link numbered after its text (`Plan your visit [#4]`) and the numbered addresses listed at the end |
| `read <address> --from 10000` | The next part of a long page: each read gives 10,000 characters and says where to read on |

You can run it yourself to see what the assistant sees:

```console
$ attache skill web search british museum opening hours
$ attache skill web read https://www.britishmuseum.org/visit
```

Search uses DuckDuckGo's plain HTML page, which needs no key. DuckDuckGo sometimes turns away
automated searches; when it does, the assistant says so and reads a site it knows instead.
For steadier search, get a [Brave Search API](https://brave.com/search/api/) key and set it
as `BRAVE_API_KEY` in the environment Attaché starts in. The script then searches through
Brave instead.

## What it can't do

- **Run a page's scripts.** It reads the HTML a site sends, as a browser with JavaScript off
  would. Most information pages work; apps that build the page in the browser come back
  nearly empty, and the assistant tries another source.
- **Sign in, fill in forms, book or buy.** It only reads. The assistant gives you the link to
  do those yourself.
- **Read files.** A PDF, an image or a download is reported as a file, with its address.

## Safety

- **Public internet only.** Every address a request goes to, the first and each redirect,
  must resolve to public IP addresses only. Your computer (`localhost`), your home or office
  network, Tailscale peers and cloud metadata addresses are refused, so neither someone in a
  group chat nor a page's link can make Attaché read your router's admin page. Without a
  proxy, each connection is checked again at the address it actually connects to, so a name
  that changes its answer after the check (DNS rebinding) fails too. Behind a proxy
  (`HTTPS_PROXY`, or the system's proxy settings) the proxy makes the connection, and the
  check of the name before each request is the guard.
- **Pages are data.** The script's output starts by saying the text is untrusted web
  content, and the skill tells the assistant never to follow instructions found on a page.
  Text a page hides from readers (`hidden`, `aria-hidden`, `display: none`), a common place
  for instructions aimed at assistants, is left out.
- **Nothing private goes out.** The skill tells the assistant to open only addresses from
  the request, from search results or from pages it read for the task, and never to put
  anything from your memory, files, mail or other chats into a search or an address.
- **Read-only.** The script writes no files and sends no cookies.

To keep the skill to your own chats, delete the `metadata` lines from
`~/.attache/skills/web/SKILL.md`; see
[sharing a skill](skills.md#sharing-a-skill-with-group-chats).

With the `claude-cli` backend, your own turns also have Claude Code's WebSearch and WebFetch
tools, and the skill tells the assistant to use those when it has them. Member turns never
use that backend, so in group chats the skill is how the assistant reaches the web.
