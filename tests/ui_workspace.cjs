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
  await page.waitForFunction(() => document.querySelector("#page-select").options.length === 3);
  return { context, page, failures };
}
async function workspace(page, name) {
  await page.getByRole("navigation", { name: "Workspace view" }).getByRole("button", { name, exact: true }).click();
}
async function layout(page, name) {
  await page.getByRole("navigation", { name: "Layout", exact: true }).getByRole("button", { name, exact: true }).click();
}

test("layouts, keyboard and pointer resizing persist without discarding chat or page drafts", async () => {
  const { context, page, failures } = await open();
  try {
    assert.equal(await page.locator("body").getAttribute("data-layout"), "chat");
    assert.equal(await page.locator("#pages").isVisible(), false);
    await page.locator("#text").fill("My unfinished message");
    await layout(page, "Split");
    const divider = page.getByRole("separator");
    await divider.focus();
    const before = Number(await divider.getAttribute("aria-valuenow"));
    await page.keyboard.press("ArrowRight");
    assert.equal(Number(await divider.getAttribute("aria-valuenow")), before + 2);
    const box = await divider.boundingBox();
    await page.mouse.move(box.x + box.width / 2, box.y + 20);
    await page.mouse.down();
    await page.mouse.move(720, box.y + 20);
    await page.mouse.up();
    const width = Number(await divider.getAttribute("aria-valuenow"));
    assert.ok(width >= 49 && width <= 51);
    await page.frameLocator('iframe[title="Home"]').getByLabel("Page draft").fill("Unsaved page input");
    await workspace(page, "Pages");
    await page.getByRole("button", { name: "Notes Open page" }).click();
    await page.frameLocator('iframe[title="Notes"]').getByLabel("Page draft").fill("Notes input");
    await workspace(page, "Settings");
    await workspace(page, "Tasks");
    assert.equal(await page.frameLocator('iframe[title="Home"]').getByLabel("Page draft").inputValue(), "Unsaved page input");
    await layout(page, "Workspace");
    await layout(page, "Chat");
    assert.equal(await page.locator("#text").inputValue(), "My unfinished message");
    await layout(page, "Split");
    await workspace(page, "Pages");
    await page.reload();
    await page.waitForSelector("#status.live").catch(async (error) => {
      throw new Error(`${error.message}; page errors: ${JSON.stringify(failures)}; body: ${(await page.locator("body").innerText()).slice(0, 400)}`);
    });
    assert.equal(await page.locator("body").getAttribute("data-layout"), "split");
    assert.equal(Number(await page.getByRole("separator").getAttribute("aria-valuenow")), width);
    assert.equal(await page.locator("#library-view").isVisible(), true);
    await divider.focus();
    await page.keyboard.press("End");
    const preferred = await divider.getAttribute("aria-valuenow");
    await page.setViewportSize({ width: 390, height: 844 });
    await layout(page, "Workspace");
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.waitForFunction((value) => document.querySelector("#divider").getAttribute("aria-valuenow") === value, preferred);
    assert.equal(await divider.getAttribute("aria-valuenow"), preferred);
    assert.deepEqual(failures, []);
  } finally { await context.close(); }
});

test("mobile keeps one pane and survives blocked or corrupt storage", async () => {
  for (const blocked of [false, true]) {
    const { context, page, failures } = await open({ viewport: { width: 390, height: 844 } }, blocked
      ? () => { if (window === window.top) Object.defineProperty(window, "localStorage", { get() { throw new Error("Blocked"); } }); }
      : () => { if (window === window.top) localStorage.setItem("attache-workspace-v1", "invalid JSON"); });
    try {
      assert.equal(await page.locator("#split-layout").isVisible(), false);
      await page.locator("#text").fill("Mobile draft");
      await layout(page, "Workspace");
      assert.equal(await page.locator("#pages").isVisible(), true);
      assert.equal(await page.locator("#chat").isVisible(), false);
      await workspace(page, "Activity");
      await page.locator("#activity-reminders .record").filter({ hasText: "Review this week's plan" }).waitFor();
      await layout(page, "Chat");
      assert.equal(await page.locator("#text").inputValue(), "Mobile draft");
      assert.deepEqual(failures, []);
    } finally { await context.close(); }
  }
});

test("settings preserves draft across views and editors, validates, reviews then saves", async () => {
  const { context, page, failures } = await open();
  try {
    await layout(page, "Split");
    await workspace(page, "Settings");
    const name = page.getByLabel("Assistant name", { exact: true });
    await name.fill("Workspace helper");
    await workspace(page, "Tasks");
    await workspace(page, "Settings");
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
    await layout(page, "Split");
    const draft = page.frameLocator('iframe[title="Home"]').getByLabel("Page draft");
    await draft.fill("Keep me");
    assert.equal(await page.locator('iframe[title="Home"]').getAttribute("sandbox"), "allow-scripts allow-forms");
    await page.request.post(new URL("/__test/republish", address).href);
    await workspace(page, "Pages");
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await workspace(page, "Tasks");
    await page.getByRole("button", { name: "Updated — reload", exact: true }).waitFor();
    assert.equal(await draft.inputValue(), "Keep me");
    page.once("dialog", (dialog) => dialog.accept());
    await page.getByRole("button", { name: "Updated — reload", exact: true }).click();
    await page.waitForFunction(() => document.querySelector("#reload-page").textContent === "Reload");
    await draft.waitFor();
    assert.equal(await draft.inputValue(), "");
    await workspace(page, "Analysis");
    await page.getByRole("button", { name: "Analysis sample Open page", exact: true }).waitFor();
    assert.equal(await page.getByRole("button", { name: "Notes Open page", exact: true }).isVisible(), false);
    assert.deepEqual(failures, []);
  } finally { await context.close(); }
});

test("expired owner authentication removes the workspace and ignores pending settings responses", async () => {
  const { context, page, failures } = await open();
  try {
    await layout(page, "Split");
    await context.route("**/api/settings", async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 100));
      await route.fulfill({ status: 401, body: "expired" });
    });
    await workspace(page, "Settings");
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

// Last: it empties the fixture's chat.
test("New chat and a typed /new start the chat afresh on every open page", async () => {
  const { context, page, failures } = await open();
  const other = await context.newPage();
  try {
    await other.goto(address);
    await other.waitForSelector("#status.live");
    const notice = "New chat. Earlier messages are kept, but I no longer see them.";
    for (const p of [page, other]) await p.getByText("Can we make this workspace fit how I work?").waitFor();
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
