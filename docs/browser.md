---
icon: lucide/app-window
---

# Browser

The `browser` skill lets the assistant read your company's own sites with your sign-in:
SharePoint pages and search, Power BI reports and dashboards, an intranet. It drives Microsoft
Edge or Google Chrome, whichever you choose, opens a page, clicks through links, tabs, filters
and slicers, and reads what is there. Ask "what does the finance hub say about the budget
deadline?" or "what were North's sales on the Q3 dashboard?", and it reads the page and
answers with the address it used. It can also send you a picture of a dashboard.

It is yours alone: it never runs in a group chat or for anyone else. It only reads, and only
the sites you list. For the public internet, the assistant uses the [web](web.md) skill.

## Setting it up

1. Make the settings file, in a terminal:

    ```console
    $ attache skill browser setup
    ```

2. List your sites in `~/.attache/skills/browser/browser.toml`. A name covers its subdomains
   too:

    ```toml
    sites = [
      "contoso.sharepoint.com",     # SharePoint Online
      "contoso-my.sharepoint.com",  # OneDrive for work
      "app.powerbi.com",            # Power BI
    ]
    browser = "edge"                # or "chrome", or the full path of another Chromium browser
    ```

    `edge` is the default on Windows and `chrome` elsewhere. If your company signs in through
    a page of its own (ADFS, say `sts.contoso.com`), list that too.

3. Sign in once:

    ```console
    $ attache skill browser login
    ```

    A browser window opens at your first site. Sign in as you normally would (with your
    authenticator, if it asks), tick "Stay signed in" if it is offered, then close the window.

The browser runs with a profile of its own in `~/.attache/.browser/`, so your everyday Edge
or Chrome, its tabs and its sign-ins are never touched, and you can keep using it. Each
browser keeps its own sign-in. When the sign-in runs out, the assistant tells you, and you run
`login` again.

The first run installs [Playwright](https://playwright.dev/python/), which the script uses to
drive the browser you already have; nothing else is downloaded.

## What it does

The assistant runs `skills/browser/scripts/browser.py`:

| Command | What it does |
|---|---|
| `read <address>` | Opens the page and prints it as an accessibility snapshot: headings, text, links with their addresses, buttons, tabs, and the values Power BI visuals announce. Frames on the page, such as a Power BI report on a SharePoint page, are included. 10,000 characters at a time; `--from` reads on. |
| `--click "link: Quarterly results"` | Clicks something by its role and name, as the snapshot shows them, or `"text: North"` for something showing that text. |
| `--fill "searchbox: Search=budget"`, `--press Enter` | Types into a search box and presses a key. |
| `--wait 10` | Waits for slow pages, such as dashboards, up to a minute. |
| `--shot sales.png` | Also saves a picture of the page in the chat's files folder, so the assistant can send it to you. |
| `--show` | Shows the window while it works, which helps when something doesn't behave. |

Steps run in order, so the assistant can open a report, pick a page and a slicer, and read
the result in one go. Each read starts afresh, without the clicks of the one before.

You can run it yourself to see what the assistant sees:

```console
$ attache skill browser read https://contoso.sharepoint.com/sites/finance
$ attache skill browser read "https://contoso.sharepoint.com/_layouts/15/search.aspx?q=budget"
```

## What it can't do

- **Change anything.** The skill tells the assistant to click only to navigate or show
  things: links, tabs, report pages, filters, slicers. It never saves, posts, shares,
  deletes, approves or requests access; it gives you the link to do that yourself.
- **Open other sites.** A page or frame from a site not in the list is blocked. Ask for one
  and the assistant tells you which site to add.
- **Get past your company's sign-in rules.** If your company allows only managed devices, sign
  in with Edge on your work computer. A sign-in the company refuses for this profile can't be
  worked around, and shouldn't be.
- **Download files.** Documents open as Office for the web shows them, and downloads are
  refused.

## Safety

The browser's profile holds a sign-in to your work, so the skill is fenced in:

- **Yours alone.** The skill has no `attache-roles`, so members can't use it, and the script
  refuses to run unless the request is yours, also when you ask from a group chat (where
  your requests have member rights). What it reads stays in your own chats.
- **Your sites only.** The assistant can open only addresses on the sites you list. Pages and
  frames may load from those sites and from Microsoft's sign-in pages; a link, form or frame
  that leads anywhere else is blocked before its request goes. Playwright sees only the first
  address of each load, so if one of your sites redirects somewhere else the browser follows,
  but nothing from that page is read. Microsoft's sign-in pages can't be opened directly,
  because their own redirects could carry what is in the address to another site.
- **Pages are data.** The output starts by saying it is untrusted content, and the skill tells
  the assistant never to follow instructions found on a page, even on your company's own
  sites, which many people can edit. Text hidden from readers isn't in the snapshot.
- **Kept to itself.** The profile lives in `~/.attache/.browser/`, which the assistant's file
  tools refuse to open and git never commits. Downloads are refused, and service workers
  blocked.

!!! warning "What the list does not stop"

    The site list keeps the signed-in browser on your company's sites. It is not a wall
    against a page that talks the assistant into sending what it read somewhere else: your
    own chats also have the shell and the web skill. The defence there is the rule every
    prompt carries, that pages are data and never instructions. List only sites you trust.

!!! info "Your company's data goes to your model"

    What the assistant reads from SharePoint or Power BI goes to the model you configured,
    like everything else in a turn. Check that your company allows that, and use an
    endpoint it approves; your personal Claude login (the `claude-cli` backend) may not be
    one.

## When something goes wrong

| It says | Do this |
|---|---|
| the site wants you to sign in | Run `attache skill browser login` and sign in again. |
| not on the sites in browser.toml | Add the site to `sites`, if you want it read. |
| the page went to …, not one of the sites | One of your sites sent the browser elsewhere, often a sign-in page of your company's: add that site. |
| Chromium distribution 'msedge' is not found | Set `browser = "chrome"`, or the full path of the browser you have. |
| the profile is already in use by another instance | Another read, or a `login` window, is still running: wait for it, or close the window. |
