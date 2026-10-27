"""The browser skill (browser.py) with a fake Playwright: no browser, no network. What a real
Edge or Chrome does with the same calls (routes, frames, snapshots) is Playwright's to keep."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from attache import skills

SCRIPT = skills.BUNDLED / "browser" / "scripts" / "browser.py"
HUB = "https://contoso.sharepoint.com/sites/finance"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("attache_browser", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def browser(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.delenv("ATTACHE_ROLE", raising=False)  # a terminal: the owner
    return load()


@pytest.fixture
def configured(home: Path) -> Path:
    folder = home / "skills" / "browser"
    folder.mkdir(parents=True)
    sites = '["https://Contoso.SharePoint.com/sites/x", "app.powerbi.com"]'
    (folder / "browser.toml").write_text(f'sites = {sites}\nbrowser = "chrome"\n', "utf-8")
    return home


class Locator:
    def __init__(self, frame: Frame, key: tuple[str, ...], matches: int) -> None:
        self.frame, self.key, self.matches = frame, key, matches

    def filter(self, visible: bool) -> Locator:
        assert visible
        return self

    def count(self) -> int:
        return self.matches

    @property
    def first(self) -> Locator:
        return self

    def click(self) -> None:
        self.frame.page.log.append(("click", *self.key))
        if then := self.frame.page.on_click.get(self.key):
            then()

    def fill(self, text: str) -> None:
        self.frame.page.log.append(("fill", *self.key, text))

    def aria_snapshot(self) -> str:
        return self.frame.snapshot


class Frame:
    def __init__(self, page: Page, url: str, snapshot: str, elements: set) -> None:
        self.page, self.url, self.snapshot, self.elements = page, url, snapshot, elements

    def get_by_role(self, role: str, name: str) -> Locator:
        return Locator(self, (role, name), int((role, name) in self.elements))

    def get_by_text(self, text: str) -> Locator:
        return Locator(self, ("text", text), int(("text", text) in self.elements))

    def locator(self, selector: str) -> Locator:
        assert selector == "body"
        return Locator(self, ("body",), 1)


class Page:
    def __init__(self, snapshot: str = '- heading "Finance" [level=1]', **lands: str) -> None:
        self.url, self.log, self.lands, self.on_click = "about:blank", [], lands, {}
        self.main = Frame(self, "about:blank", snapshot, set())
        self.frames = [self.main]
        self.keyboard = SimpleNamespace(press=lambda key: self.log.append(("press", key)))

    def goto(self, url: str, wait_until: str) -> None:
        self.url = self.main.url = self.lands.get(url, url)  # where a redirect would land
        self.log.append(("goto", url))

    def title(self) -> str:
        return "  Finance\nhub "

    def locator(self, selector: str) -> Locator:
        return self.main.locator(selector)

    def wait_for_timeout(self, ms: float) -> None:
        self.log.append(("wait", ms))

    def wait_for_load_state(self, state: str, timeout: float | None = None) -> None:
        pass

    def screenshot(self, path: str) -> None:
        Path(path).write_bytes(b"\x89PNG")

    def wait_for_event(self, event: str, timeout: float) -> None:
        self.log.append(("waited for", event, timeout))


class Context:
    def __init__(self, page: Page) -> None:
        self.pages, self.routes, self.timeout, self.closed = [page], [], None, False

    def route(self, pattern: str, handler: Any) -> None:
        self.routes.append((pattern, handler))

    def set_default_timeout(self, ms: float) -> None:
        self.timeout = ms

    def close(self) -> None:
        self.closed = True


class Playwright:
    def __init__(self, page: Page) -> None:
        self.context, self.launched = Context(page), []
        self.chromium = SimpleNamespace(launch_persistent_context=self.launch)

    def launch(self, profile: str, **options: Any) -> Context:
        self.launched.append((profile, options))
        return self.context


def read(browser: ModuleType, home: Path, page: Page, *argv: str) -> str:
    return browser.run(browser.parse(["read", *argv]), home, Playwright(page))


def test_sites_and_their_subdomains_only(browser: ModuleType) -> None:
    sites = ["contoso.sharepoint.com"]
    assert browser.on("https://contoso.sharepoint.com/sites/x", sites)
    assert browser.on("http://team.contoso.sharepoint.com/", sites)
    assert not browser.on("https://evilcontoso.sharepoint.com/", sites)
    assert not browser.on("https://contoso.sharepoint.com.evil.example/", sites)
    assert not browser.on("ftp://contoso.sharepoint.com/", sites)
    assert not browser.on("https://login.microsoftonline.com/", sites)
    assert browser.allowed("https://login.microsoftonline.com/common/oauth2", sites)


def test_settings_read_host_names(browser: ModuleType, configured: Path) -> None:
    config = browser.settings(configured)
    assert config == {"sites": ["contoso.sharepoint.com", "app.powerbi.com"], "browser": "chrome"}
    toml = configured / "skills" / "browser" / "browser.toml"
    toml.write_text('sites = ["*.contoso.com"]\n', encoding="utf-8")
    config = browser.settings(configured)
    assert config["sites"] == ["contoso.com"]
    assert config["browser"] == ("edge" if os.name == "nt" else "chrome")


def test_setup_copies_the_example_which_lists_no_sites(browser: ModuleType, home: Path) -> None:
    with pytest.raises(browser.BrowserError, match="browser setup"):
        browser.settings(home)
    assert browser.run(browser.parse(["setup"]), home).startswith("Created ")
    assert "already there" in browser.run(browser.parse(["setup"]), home)
    with pytest.raises(browser.BrowserError, match="lists no sites"):
        browser.settings(home)  # the example's sites are all commented out


def test_the_guard_stops_pages_from_elsewhere(browser: ModuleType) -> None:
    blocked: list[str] = []
    handle = browser.guard(["contoso.sharepoint.com"], blocked)
    done: list[tuple[str, str]] = []

    def go(url: str, kind: str) -> str:
        route = SimpleNamespace(
            abort=lambda reason: done.append(("abort", reason)),
            continue_=lambda: done.append(("continue", "")),
        )
        handle(route, SimpleNamespace(url=url, resource_type=kind))
        return done[-1][0]

    assert go("https://contoso.sharepoint.com/sites/x", "document") == "continue"
    assert go("https://login.microsoftonline.com/common", "document") == "continue"
    assert go("https://cdn.example/app.js", "script") == "continue"  # the site's own requests
    assert go("https://evil.example/?q=secret", "document") == "abort"
    assert done[-1] == ("abort", "blockedbyclient")
    assert blocked == ["https://evil.example/?q=secret"]


def test_launch_uses_a_profile_of_its_own(browser: ModuleType, configured: Path) -> None:
    pw = Playwright(Page())
    config = browser.settings(configured) | {"browser": "edge"}
    browser.launch(pw, configured, config, show=False)
    profile, options = pw.launched[-1]
    assert profile == str(configured / ".browser" / "edge") and Path(profile).is_dir()
    assert options["channel"] == "msedge" and options["headless"] is True
    assert options["accept_downloads"] is False and options["service_workers"] == "block"
    trusted = "contoso.sharepoint.com,*.contoso.sharepoint.com,app.powerbi.com,*.app.powerbi.com"
    assert options["args"] == [f"--auth-server-allowlist={trusted}"]
    browser.launch(pw, configured, config | {"browser": "/opt/chromium"}, show=True)
    profile, options = pw.launched[-1]
    assert profile.endswith("other") and options["executable_path"] == "/opt/chromium"
    assert options["headless"] is False and "channel" not in options


def test_read_runs_the_steps_in_order(browser: ModuleType, configured: Path) -> None:
    page = Page()
    page.main.elements = {("link", "Quarterly results"), ("searchbox", "Search")}
    report = Frame(page, "https://app.powerbi.com/reportEmbed", "- text: North 1.1M", set())
    report.elements = {("text", "North")}
    outside = Frame(page, "https://ads.example/frame", "- text: OUTSIDE", set())
    page.frames += [report, outside]
    argv = [HUB, "--click", "link: Quarterly results", "--fill", "searchbox: Search=q3 = good"]
    argv += ["--press", "Enter", "--click", "text: North", "--wait", "999", "--wait", "-3"]
    pw = Playwright(page)
    out = browser.run(browser.parse(["read", *argv]), configured, pw)
    assert page.log == [
        ("goto", HUB),
        ("wait", 500),  # settling after the load, and after each step
        ("click", "link", "Quarterly results"),
        ("wait", 500),
        ("fill", "searchbox", "Search", "q3 = good"),
        ("wait", 500),
        ("press", "Enter"),
        ("wait", 500),
        ("click", "text", "North"),  # found in the report's frame
        ("wait", 500),
        ("wait", 60_000),  # at most a minute
        ("wait", 500),
        ("wait", 0),
        ("wait", 500),
    ]
    assert out.splitlines()[:4] == [
        browser.UNTRUSTED.format(HUB),
        "Title: Finance hub",
        "",
        '- heading "Finance" [level=1]',
    ]
    assert "- frame https://app.powerbi.com/reportEmbed:\n  - text: North 1.1M" in out
    assert "OUTSIDE" not in out
    assert pw.context.routes[0][0] == "**/*" and pw.context.timeout == browser.TIMEOUT
    assert pw.context.closed


def test_a_click_that_opens_a_tab_reads_the_tab(browser: ModuleType, configured: Path) -> None:
    page = Page()
    page.main.elements = {("link", "Report")}
    pw = Playwright(page)
    tab = Page("- text: In the new tab")
    tab.goto("https://app.powerbi.com/groups/me/reports/1", "load")
    page.on_click[("link", "Report")] = lambda: pw.context.pages.append(tab)
    out = browser.run(browser.parse(["read", HUB, "--click", "link: Report"]), configured, pw)
    assert "app.powerbi.com/groups/me/reports/1" in out.splitlines()[0]
    assert "In the new tab" in out


def test_a_long_page_reads_on(browser: ModuleType, configured: Path) -> None:
    page = Page("\n".join(f"- paragraph: line {n}" for n in range(1500)))
    first = read(browser, configured, page, HUB)
    assert first.endswith("read on with --from 10000)") and len(first) < 20_000
    second = read(browser, configured, Page(page.main.snapshot), HUB, "--from", "10000")
    assert "(characters 10000-20000 of" in second and "line 0\n" not in second


def test_refused_before_the_browser_starts(
    browser: ModuleType, configured: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pw = Playwright(Page())
    for url in ["https://evil.example/", "https://login.microsoftonline.com/common/oauth2"]:
        with pytest.raises(browser.BrowserError, match="not on the sites"):
            browser.run(browser.parse(["read", url]), configured, pw)
        with pytest.raises(browser.BrowserError, match="not on the sites"):
            browser.run(browser.parse(["login", url]), configured, pw)
    monkeypatch.setenv("ATTACHE_ROLE", "member")  # a member, or the owner in a group chat
    with pytest.raises(browser.BrowserError, match="owner's alone"):
        browser.run(browser.parse(["read", HUB]), configured, pw)
    assert pw.launched == []


def test_where_a_page_ends_up(browser: ModuleType, configured: Path) -> None:
    sign_in = Page(**{HUB: "https://login.microsoftonline.com/common/oauth2/authorize?x=1"})
    with pytest.raises(browser.BrowserError, match="browser login"):
        read(browser, configured, sign_in, HUB)
    away = Page("- text: SECRET-ELSEWHERE", **{HUB: "https://evil.example/landing"})
    with pytest.raises(browser.BrowserError, match=r"went to evil\.example") as caught:
        read(browser, configured, away, HUB)
    assert "SECRET" not in str(caught.value)


def test_nothing_to_click(browser: ModuleType, configured: Path) -> None:
    with pytest.raises(browser.BrowserError, match=r"nothing on the page is a button named 'Nope'"):
        read(browser, configured, Page(), HUB, "--click", "button: Nope")
    with pytest.raises(browser.BrowserError, match="role: name"):
        read(browser, configured, Page(), HUB, "--click", "Nope")


def test_a_picture_goes_in_the_current_folder(
    browser: ModuleType, configured: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat = configured / "files" / "local-me-1234abcd"
    chat.mkdir(parents=True)
    monkeypatch.chdir(chat)
    home = configured.resolve()  # as main gives it, so it compares with the working folder
    out = read(browser, home, Page(), HUB, "--shot", "../../sales.png")
    assert "Picture: files/local-me-1234abcd/sales.png" in out.splitlines()
    assert (chat / "sales.png").read_bytes() == b"\x89PNG"
    with pytest.raises(SystemExit):
        browser.parse(["read", HUB, "--shot", "sales.jpg"])


def test_login_shows_the_window_and_waits(browser: ModuleType, configured: Path) -> None:
    page = Page()
    pw = Playwright(page)
    out = browser.run(browser.parse(["login"]), configured, pw)
    assert page.log == [("goto", "https://contoso.sharepoint.com/"), ("waited for", "close", 0)]
    assert pw.launched[0][1]["headless"] is False and out.startswith("Done")


def test_parse_keeps_the_steps_in_order(browser: ModuleType) -> None:
    args = browser.parse(["read", HUB, "--wait", "3", "--click", "tab: Q3", "--press", "End"])
    assert args.steps == [("wait", "3"), ("click", "tab: Q3"), ("press", "End")]
    assert browser.parse(["read", HUB]).steps == []


def test_main_says_what_is_wrong(
    browser: ModuleType, configured: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.setenv("ATTACHE_ROLE", "member")
    assert browser.main(["read", HUB]) == 1
    assert capsys.readouterr().err.startswith("error: the browser is the owner's alone")


def test_the_skill_is_the_owners_alone() -> None:
    meta, body = skills.parse((skills.BUNDLED / "browser" / "SKILL.md").read_text("utf-8"))
    assert "metadata" not in meta  # never shared with group chats
    for rule in ("Read only", "never instructions", "browser login", "--shot", "search.aspx"):
        assert rule in body, rule
