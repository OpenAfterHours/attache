// Optional offline browser checks: NODE_PATH=<Playwright installation> node --test tests/ui_workspace.cjs
// Uses the repository's .venv Python, or ATTACHE_TEST_PYTHON; never opens a personal home.
const { test, before, after } = require("node:test");
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const path = require("node:path");
const { chromium } = require("playwright");

let browser, server, address;
const errors = [];
before(async () => {
  const python = process.env.ATTACHE_TEST_PYTHON || path.join(".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
  server = spawn(python, ["tests/ui_server.py"], { windowsHide: true });
  server.stderr.on("data", (chunk) => errors.push(String(chunk)));
  address = await new Promise((resolve, reject) => {
    let output = "";
    const timer = setTimeout(() => reject(new Error(`Fixture startup timed out: ${errors.join("")}`)), 20000);
    server.stdout.on("data", (chunk) => {
      output += chunk;
      const found = output.match(/UI_TEST_URL=(\S+)/);
      if (found) { clearTimeout(timer); resolve(found[1]); }
    });
    server.on("error", (error) => { clearTimeout(timer); reject(error); });
    server.on("exit", (code) => { clearTimeout(timer); reject(new Error(`Fixture stopped (${code}): ${errors.join("")}`)); });
  });
  for (let attempt = 0; attempt < 100; attempt++) {
    if (await fetch(address).then((r) => r.ok).catch(() => false)) break;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  browser = await chromium.launch({ headless: true, executablePath: process.env.ATTACHE_TEST_BROWSER });
});
after(async () => { await browser?.close(); server?.kill(); });

async function open(options = {}, init = null) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, ...options });
  if (init) await context.addInitScript(init);
  const page = await context.newPage();
  const failures = [];
  page.on("pageerror", (error) => failures.push(error.message));
  await page.goto(address);
  await page.waitForSelector("#status.live");
  await page.waitForFunction(() => document.querySelectorAll("#page-list .pg").length === 4);
  return { context, page, failures };
}
// A view from the sidebar: Today, Chat, Settings, All pages, or on a narrow screen Pages.
async function go(page, name) {
  await page.locator("#sidebar").getByRole("button", { name, exact: true }).click();
}
// A page from the sidebar's list, by its name ("Home", "Notes"...).
async function openPage(page, name) {
  await page.getByRole("navigation", { name: "Pages", exact: true }).getByRole("button", { name: new RegExp(`^${name}`) }).click();
}
const post = (page, route, data) => page.request.post(new URL(route, address).href, { data });
const panel = (page) => page.locator("body").getAttribute("data-panel");

test("views and the page beside them keep chat and page drafts, and remember size and place", async () => {
  const { context, page, failures } = await open();
  try {
    assert.equal(await page.locator("body").getAttribute("data-view"), "today");
    assert.equal(await panel(page), "none");
    await page.locator("#today-text").fill("Today draft");
    await go(page, "Chat");
    await page.locator("#text").fill("My unfinished message");
    await openPage(page, "Home");
    assert.equal(await panel(page), "side");
    assert.equal(await page.locator("#page-title").textContent(), "Home");
    const divider = page.getByRole("separator");
    await divider.focus();
    const before = Number(await divider.getAttribute("aria-valuenow"));
    await page.keyboard.press("ArrowRight");
    assert.equal(Number(await divider.getAttribute("aria-valuenow")), before + 2);
    const work = await page.locator("#work").boundingBox();
    const box = await divider.boundingBox();
    await page.mouse.move(box.x + box.width / 2, box.y + 20);
    await page.mouse.down();
    await page.mouse.move(work.x + (work.width - 9) / 2, box.y + 20);
    await page.mouse.up();
    const width = Number(await divider.getAttribute("aria-valuenow"));
    assert.ok(width >= 49 && width <= 51, String(width));
    await page.frameLocator('iframe[title="Home"]').getByLabel("Page draft").fill("Unsaved page input");
    await openPage(page, "Notes");
    await page.frameLocator('iframe[title="Notes"]').getByLabel("Page draft").fill("Notes input");
    await go(page, "Settings");
    await go(page, "Today");
    await openPage(page, "Home");
    assert.equal(await page.frameLocator('iframe[title="Home"]').getByLabel("Page draft").inputValue(), "Unsaved page input");
    await page.getByRole("button", { name: "Use the whole window" }).click();
    assert.equal(await panel(page), "wide");
    assert.equal(await page.locator("#main").isVisible(), false);
    await go(page, "Chat"); // a view puts the page back beside it
    assert.equal(await panel(page), "side");
    await page.getByRole("button", { name: "Close page" }).click();
    assert.equal(await panel(page), "none");
    await openPage(page, "Home"); // closing keeps the page loaded
    assert.equal(await page.frameLocator('iframe[title="Home"]').getByLabel("Page draft").inputValue(), "Unsaved page input");
    assert.equal(await page.locator("#text").inputValue(), "My unfinished message");
    await go(page, "Today");
    assert.equal(await page.locator("#today-text").inputValue(), "Today draft");
    await page.reload();
    await page.waitForSelector("#status.live").catch(async (error) => {
      throw new Error(`${error.message}; page errors: ${JSON.stringify(failures)}; body: ${(await page.locator("body").innerText()).slice(0, 400)}`);
    });
    await page.waitForFunction(() => document.body.dataset.panel === "side");
    assert.equal(await page.locator("body").getAttribute("data-view"), "today");
    assert.equal(await page.locator("#page-title").textContent(), "Home");
    assert.equal(Number(await page.getByRole("separator").getAttribute("aria-valuenow")), width);
    await divider.focus();
    await page.keyboard.press("End");
    const preferred = await divider.getAttribute("aria-valuenow");
    await page.setViewportSize({ width: 390, height: 844 });
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.waitForFunction((value) => document.querySelector("#divider").getAttribute("aria-valuenow") === value, preferred);
    assert.equal(await divider.getAttribute("aria-valuenow"), preferred);
    assert.deepEqual(failures, []);
  } finally { await context.close(); }
});

test("a phone shows one thing at a time and survives blocked or corrupt storage", async () => {
  for (const blocked of [false, true]) {
    const { context, page, failures } = await open({ viewport: { width: 390, height: 844 } }, blocked
      ? () => { if (window === window.top) Object.defineProperty(window, "localStorage", { get() { throw new Error("Blocked"); } }); }
      : () => { if (window === window.top) localStorage.setItem("attache-workspace-v2", "invalid JSON"); });
    try {
      assert.equal(await page.getByRole("navigation", { name: "Pages", exact: true }).isVisible(), false);
      await page.locator("#reminder-list").getByText("Review this week's plan").waitFor();
      await go(page, "Chat");
      await page.locator("#text").fill("Mobile draft");
      await go(page, "Pages");
      await page.locator("#page-library").getByRole("button", { name: /^Notes/ }).click();
      assert.equal(await page.locator("#pages").isVisible(), true);
      assert.equal(await page.locator("#main").isVisible(), false);
      await page.getByRole("button", { name: "Close page" }).click();
      assert.equal(await page.locator("#library-view").isVisible(), true);
      await openPageFromToday(page, "Notes");
      await go(page, "Chat"); // a view replaces the page on a small screen
      assert.equal(await page.locator("#pages").isVisible(), false);
      assert.equal(await page.locator("#text").inputValue(), "Mobile draft");
      assert.deepEqual(failures, []);
    } finally { await context.close(); }
  }
});
async function openPageFromToday(page, name) {
  await go(page, "Today");
  await page.locator("#recent-pages").getByRole("button", { name: new RegExp(`^${name}`) }).click();
  assert.equal(await page.locator("#pages").isVisible(), true);
}

test("settings preserves draft across views and editors, validates, reviews then saves", async () => {
  const { context, page, failures } = await open();
  try {
    await go(page, "Settings");
    const name = page.getByLabel("Assistant name", { exact: true });
    await name.fill("Workspace helper");
    await go(page, "Today");
    await go(page, "Settings");
    assert.equal(await name.inputValue(), "Workspace helper");
    await page.getByRole("button", { name: "Editable settings (TOML)", exact: true }).click();
    const editor = page.getByLabel("Editable settings in TOML");
    await editor.waitFor({ state: "visible" });
    assert.ok((await editor.inputValue()).includes("Workspace helper"));
    const good = await editor.inputValue();
    await editor.fill("[broken");
    await page.getByRole("button", { name: "Review changes", exact: true }).click();
    await page.getByText(/Not validated:/).waitFor();
    assert.equal(await page.getByRole("button", { name: "Save settings", exact: true }).isVisible(), false);
    await editor.fill(good);
    await page.getByRole("button", { name: "Common settings", exact: true }).click();
    await name.waitFor({ state: "visible" });
    assert.equal(await name.inputValue(), "Workspace helper");
    await page.getByRole("button", { name: "Review changes", exact: true }).click();
    await page.locator("#settings-review").waitFor({ state: "visible" });
    assert.match(await page.locator("#settings-changes").innerText(), /assistant.name.*Workspace helper/);
    await name.fill("Workspace helper two");
    assert.equal(await page.locator("#settings-review").isVisible(), false);
    await page.getByRole("button", { name: "Review changes", exact: true }).click();
    await page.getByRole("button", { name: "Save settings", exact: true }).click();
    await page.getByText("Settings saved. The previous configuration was backed up.").waitFor();
    assert.match(await page.locator("#settings-state").innerText(), /differs from the running/);
    await name.fill("Unsaved draft before restore");
    await page.getByRole("button", { name: "Review previous configuration", exact: true }).click();
    await page.getByRole("button", { name: "Restore previous configuration", exact: true }).waitFor();
    assert.equal(await name.inputValue(), "Unsaved draft before restore");
    assert.match(await page.locator("#settings-changes").innerText(), /Workspace helper two.*Attaché/);
    await page.getByRole("button", { name: "Restore previous configuration", exact: true }).click();
    await page.waitForFunction(() => document.querySelector('#settings-fields input').value === "Attaché");
    assert.match(await page.locator("#settings-state").innerText(), /matches the running/);
    assert.deepEqual(failures, []);
  } finally { await context.close(); }
});

test("page updates are announced without silently replacing unsaved page state", async () => {
  const { context, page, failures } = await open();
  try {
    await openPage(page, "Home");
    const draft = page.frameLocator('iframe[title="Home"]').getByLabel("Page draft");
    await draft.fill("Keep me");
    assert.equal(await page.locator('iframe[title="Home"]').getAttribute("sandbox"), "allow-scripts allow-forms");
    await post(page, "/__test/republish");
    await go(page, "All pages");
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await page.getByRole("button", { name: "Updated — reload", exact: true }).waitFor();
    await page.locator('#page-list .pg[data-page="home"] .fresh').waitFor();
    assert.equal(await draft.inputValue(), "Keep me");
    page.once("dialog", (dialog) => dialog.accept());
    await page.getByRole("button", { name: "Updated — reload", exact: true }).click();
    await page.waitForFunction(() => document.querySelector("#reload-label").textContent === "Reload");
    await draft.waitFor();
    assert.equal(await draft.inputValue(), "");
    assert.equal(await page.locator("#page-list .fresh").count(), 0);
    await page.locator("#analysis-library").getByRole("button", { name: /^Analysis sample/ }).waitFor();
    assert.equal(await page.locator("#analysis-library").getByRole("button", { name: /^Notes/ }).count(), 0);
    assert.deepEqual(failures, []);
  } finally { await context.close(); }
});

test("expired owner authentication removes the workspace and ignores pending settings responses", async () => {
  const { context, page, failures } = await open();
  try {
    await openPage(page, "Home");
    await context.route("**/api/settings", async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 100));
      await route.fulfill({ status: 401, body: "expired" });
    });
    await go(page, "Settings");
    await page.getByText("Open Attaché from the link printed in your terminal.").waitFor();
    await page.setViewportSize({ width: 800, height: 900 });
    assert.equal(await page.locator("iframe").count(), 0);
    assert.equal(await page.locator("#settings-editor").count(), 0);
    assert.deepEqual(failures, []);
  } finally { await context.close(); }
});

test("a temporarily refused event stream reconnects without losing authentication", async () => {
  const context = await browser.newContext();
  let requests = 0;
  await context.route("**/api/events?*", (route) => {
    requests++;
    return requests === 1 ? route.fulfill({ status: 503, body: "Try again" }) : route.continue();
  });
  const page = await context.newPage();
  const failures = [];
  page.on("pageerror", (error) => failures.push(error.message));
  try {
    await page.goto(address);
    await page.waitForSelector("#status.live");
    assert.ok(requests >= 2);
    assert.equal(await page.locator("#status").getAttribute("title"), "Connected");
    assert.equal(await page.locator("#chat").count(), 1);
    assert.deepEqual(failures, []);
  } finally { await context.close(); }
});

test("replies show their Markdown as elements, never as HTML, and questions stay as typed", async () => {
  const { context, page, failures } = await open();
  try {
    await go(page, "Chat");
    await post(page, "/__test/reply", { text: [
      "## Plan", "", "Use **bold**, *care*, `code` and [the docs](https://example.com/docs).", "",
      "1. First", "2. Second", "   - [x] nested", "", "```", "<b>as typed</b>", "```", "",
      "| Day | Task |", "|-----|-----:|", "| Mon | Write |", "",
      "<img src=x onerror=alert(1)> [run](javascript:alert(1)) ![pixel](https://example.com/p.png)",
    ].join("\n") });
    const reply = page.locator("#log .msg.bot .md").last();
    await reply.locator("h4", { hasText: "Plan" }).waitFor();
    assert.equal(await reply.locator("strong").textContent(), "bold");
    assert.equal(await reply.locator("em").textContent(), "care");
    assert.equal(await reply.locator("p code").textContent(), "code");
    assert.equal(await reply.getByRole("link", { name: "the docs" }).getAttribute("href"), "https://example.com/docs");
    assert.equal(await reply.locator("ol > li").count(), 2);
    assert.equal(await reply.locator("ol ul input[type=checkbox]").isChecked(), true);
    assert.equal(await reply.locator("pre code").textContent(), "<b>as typed</b>");
    assert.equal(await reply.locator("pre b").count(), 0);
    assert.equal(await reply.locator("td").last().evaluate((td) => td.style.textAlign), "right");
    assert.equal(await reply.locator("img").count(), 0); // an image is only a link: nothing loads
    assert.equal(await reply.getByRole("link", { name: "pixel" }).getAttribute("href"), "https://example.com/p.png");
    assert.ok((await reply.textContent()).includes("<img src=x onerror=alert(1)> [run](javascript:alert(1))"));
    assert.equal(await reply.locator('a[href^="javascript"]').count(), 0);
    await post(page, "/__test/reply", { ask: true, text: "Send this?\n\n**To:** sam@example.com", options: ["Send", "Not now"] });
    const ask = page.locator("#log .msg.ask").last();
    await ask.getByRole("button", { name: "Send", exact: true }).waitFor();
    assert.equal(await ask.locator(".ask-text").textContent(), "Send this?\n\n**To:** sam@example.com");
    assert.equal(await ask.locator(".md, strong").count(), 0);
    await ask.getByRole("button", { name: "Not now", exact: true }).click();
    await ask.getByText("You chose: Not now").waitFor();
    assert.equal(await ask.getByRole("button").count(), 0);
    assert.deepEqual(failures, []);
  } finally { await context.close(); }
});

test("questions wait on Today until answered in the chat, and replies elsewhere are flagged", async () => {
  const { context, page, failures } = await open();
  try {
    await post(page, "/__test/reply", { ask: true, id: "today-ask", text: "Book the table for Friday?", options: ["Yes", "No"] });
    const card = page.locator("#waiting-list");
    await card.getByText("Book the table for Friday?").waitFor();
    assert.equal(await page.locator("#waiting-count").textContent(), "1");
    assert.match(await page.locator("#today-summary").textContent(), /One question is waiting/);
    await card.getByRole("button", { name: "Review: Book the table for Friday?" }).click();
    assert.equal(await page.locator("body").getAttribute("data-view"), "chat");
    const ask = page.locator("#log .msg.ask").last();
    assert.equal(await ask.getByRole("button", { name: "Yes", exact: true }).evaluate((b) => b === document.activeElement), true);
    await ask.getByRole("button", { name: "No", exact: true }).click();
    await go(page, "Today");
    await card.getByText("Nothing is waiting for you.").waitFor();
    assert.equal(await page.locator("#waiting-count").isVisible(), false);
    const chat = page.locator('#sidebar .nav[data-view="chat"]');
    await post(page, "/__test/reply", { text: "Your table is booked." });
    await page.waitForFunction(() => document.querySelector('#sidebar .nav[data-view="chat"]').dataset.state === "unread");
    await go(page, "Chat");
    assert.equal(await chat.getAttribute("data-state"), "");
    assert.deepEqual(failures, []);
  } finally { await context.close(); }
});

test("a turn's steps build up in one card, and its footer stays when it finishes", async () => {
  const { context, page, failures } = await open();
  try {
    await go(page, "Chat");
    await post(page, "/__test/activity", { kind: "started" });
    await post(page, "/__test/activity", { kind: "step", label: "Searching mail" });
    await post(page, "/__test/activity", { kind: "step", label: "Reading the thread" });
    await post(page, "/__test/activity", { kind: "thinking", text: "Sam needs it tonight. Then the rest." });
    const turn = page.locator("#log .msg.typing");
    await turn.locator(".step.now", { hasText: "Reading the thread…" }).waitFor();
    assert.equal(await turn.locator(".step:not(.now)").textContent(), "Searching mail");
    assert.equal(await turn.locator(".thought").textContent(), "Sam needs it tonight.");
    assert.equal(await page.locator('#sidebar .nav[data-view="chat"]').getAttribute("data-state"), "busy");
    await post(page, "/__test/activity", { kind: "finished", seconds: 1.5, tokens: 1200, route: "local" });
    await page.locator("#log .footer", { hasText: "1.5s · 1.2k tokens · local" }).waitFor();
    assert.equal(await turn.count(), 0);
    await go(page, "Today");
    await page.locator("#activity-list").getByText("Reading the thread").waitFor();
    assert.deepEqual(failures, []);
  } finally { await context.close(); }
});

test("Today lists open to-dos soonest first, and ticking one marks it done", async () => {
  const { context, page, failures } = await open();
  try {
    const list = page.locator("#todo-list");
    await list.getByText("Book the boiler service").waitFor();
    assert.deepEqual(await list.locator(".title").allTextContents(), [
      "Book the boiler service", "Send Sam the review deck", "Draft the budget notes", "Pick up the dry cleaning",
    ]);
    assert.equal(await list.locator(".row", { hasText: "Book the boiler service" }).locator(".badge").textContent(), "overdue");
    assert.equal(await list.locator(".row", { hasText: "Send Sam the review deck" }).locator(".badge").textContent(), "today");
    await list.getByLabel("Done: Pick up the dry cleaning").check();
    await page.waitForFunction(() => document.querySelector("#todo-count").textContent === "3 open");
    assert.equal(await list.getByText("Pick up the dry cleaning").count(), 0);
    await page.getByRole("button", { name: "Open Home", exact: true }).click();
    assert.equal(await page.locator("#page-title").textContent(), "Home");
    assert.deepEqual(failures, []);
  } finally { await context.close(); }
});

// Last: it empties the fixture's chat.
test("New chat and a typed /new start the chat afresh on every open page", async () => {
  const { context, page, failures } = await open();
  const other = await context.newPage();
  try {
    await other.goto(address);
    await other.waitForSelector("#status.live");
    const notice = "New chat. Earlier messages are kept, but I no longer see them.";
    for (const p of [page, other]) {
      await go(p, "Chat");
      await p.getByText("Can you put the Lisbon bookings on a page?").waitFor();
    }
    await page.getByRole("button", { name: "New chat", exact: true }).click();
    for (const p of [page, other]) {
      await p.locator("#log .msg.bot", { hasText: notice }).waitFor();
      assert.equal(await p.locator("#log .msg").count(), 1); // the earlier messages are gone
    }
    await page.reload();
    await page.waitForSelector("#status.live");
    await page.getByText("Ask Attaché anything. Replies appear here.").waitFor();
    await page.locator("#text").fill("/new");
    await page.locator("#text").press("Enter");
    await page.locator("#log .msg.bot", { hasText: notice }).waitFor();
    assert.equal(await page.locator("#log .msg.you").count(), 0); // a command, not a message
    assert.deepEqual(failures, []);
  } finally { await context.close(); }
});
