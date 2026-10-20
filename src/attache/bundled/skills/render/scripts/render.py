# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Take a picture (PNG) or a PDF of an Attaché page with the Edge or Chrome already installed.

    render.py <snapshot> <out.png|out.pdf> [--width 1280] [--height 900] [--wait 4]

<snapshot> is the link the artifact tool returns, and nothing else is accepted: its origin must
be this Attaché's ($ATTACHE_WEB_URL, which `attache run` sets), its path /a/<page>, and it must
carry the page's t= and r= tokens. So the script cannot take pictures of other local servers
(an admin page, say) or local files, which members could then send into a group chat. Width
and height are at most 4000 pixels, and the wait at most 30 seconds.

The browser runs headless with a throwaway profile and cannot reach the internet: every host
name but localhost and 127.0.0.1 fails to resolve, and anything else goes to a proxy that is
not there. The file is written into the current folder (the chat's files folder when the
script tool runs this), and its path, relative to the Attaché home when it is in one, is
printed.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

NAMES = [
    "msedge",
    "microsoft-edge",
    "microsoft-edge-stable",
    "google-chrome",
    "google-chrome-stable",
    "chrome",
    "chromium",
    "chromium-browser",
]
PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/microsoft-edge",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/snap/bin/chromium",
]
PAGE = re.compile(r"/a/[a-z0-9-]+")  # an Attaché page, as web.py serves it
OFFLINE = [
    "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1",
    "--proxy-server=127.0.0.1:9",  # nothing listens: IP literals fail too (loopback is exempt)
]


def find_browser() -> str | None:
    """Edge or Chrome: first on PATH, then where the installers put them."""
    for name in NAMES:
        if found := shutil.which(name):
            return found
    candidates = list(PATHS)
    if local := os.environ.get("LOCALAPPDATA"):  # Chrome installed for one Windows user
        candidates.append(str(Path(local) / "Google" / "Chrome" / "Application" / "chrome.exe"))
    return next((path for path in candidates if Path(path).is_file()), None)


def at_most(limit: float, kind: Callable[[str], float]) -> Callable[[str], float]:
    """An argparse type: a number above 0 and at most `limit`."""

    def number(text: str) -> float:
        value = kind(text)
        if not 0 < value <= limit:
            raise argparse.ArgumentTypeError(f"{text} is not between 0 and {limit}")
        return value

    return number


def snapshot_problem(url: str) -> str | None:
    """Why `url` is not a snapshot link of this Attaché's pages, or None when it is one."""
    web = os.environ.get("ATTACHE_WEB_URL")
    if not web:
        return "ATTACHE_WEB_URL is not set: run this through Attaché's script tool"
    parts, home = urlsplit(url), urlsplit(web)
    if (parts.scheme, parts.netloc) != (home.scheme, home.netloc):
        return f"only pages of this Attaché ({web}) can be rendered"
    tokens = parse_qs(parts.query)
    both_tokens = all(len(tokens.get(key, [])) == 1 for key in ("t", "r"))
    if not PAGE.fullmatch(parts.path) or not both_tokens:
        return "give the `snapshot` link from the artifact tool: /a/<page>?t=...&r=..."
    return None


def parse(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render an Attaché page to a PNG or a PDF.")
    parser.add_argument("url", help="the artifact tool's `snapshot` link")
    parser.add_argument("out", help="the file to write in the current folder: .png or .pdf")
    parser.add_argument("--width", type=at_most(4000, int), default=1280, help="pixels, ≤ 4000")
    parser.add_argument("--height", type=at_most(4000, int), default=900, help="pixels, ≤ 4000")
    parser.add_argument("--wait", type=at_most(30, float), default=4, help="seconds, at most 30")
    args = parser.parse_args(argv)
    if problem := snapshot_problem(args.url):
        parser.error(problem)
    if Path(args.out).suffix.lower() not in (".png", ".pdf"):
        parser.error("out must end in .png or .pdf")
    args.out = Path.cwd() / Path(args.out).name  # always into the current folder
    return args


def check(url: str) -> str | None:
    """Ask for the page once, as the browser will; why it was refused, or None. A refused page
    would still render, as a picture of the error, so this stops that early."""
    request = urllib.request.Request(url, headers={"Sec-Fetch-Dest": "document"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # straight to localhost
    try:
        with opener.open(request, timeout=15):
            return None
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:200]
        return (
            f"the page answered {error.code} {detail} (snapshot links change when Attaché restarts)"
        )
    except OSError as error:
        return f"cannot reach the page: {error} (is Attaché running?)"


def command(browser: str, args: argparse.Namespace, profile: str) -> list[str]:
    """The browser's argument list. --timeout, not --virtual-time-budget: a page with a live
    event stream never goes idle, so a virtual-time budget would wait for ever."""
    if args.out.suffix.lower() == ".pdf":
        output = [f"--print-to-pdf={args.out}", "--no-pdf-header-footer"]
    else:
        output = [f"--screenshot={args.out}", "--hide-scrollbars"]
    return [
        browser,
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--mute-audio",
        f"--user-data-dir={profile}",
        *OFFLINE,
        f"--window-size={args.width},{args.height}",
        f"--timeout={int(args.wait * 1000)}",
        *output,
        args.url,
    ]


def shown(path: Path) -> str:
    """The path relative to the Attaché home (files/<chat>/page.png) when it is under one."""
    home = next((parent.parent for parent in path.parents if parent.name == "files"), None)
    return (path.relative_to(home) if home else path).as_posix()


def main(argv: list[str] | None = None) -> int:
    args = parse(argv)
    browser = find_browser()
    if browser is None:
        print("error: no Edge or Chrome found; install one to render pages", file=sys.stderr)
        return 2
    if problem := check(args.url):
        print(f"error: {problem}", file=sys.stderr)
        return 1
    args.out.unlink(missing_ok=True)
    # The browser may still hold files in the profile for a moment on Windows: never fail on it.
    with tempfile.TemporaryDirectory(
        prefix="attache-render-", ignore_cleanup_errors=True
    ) as profile:
        try:
            done = subprocess.run(
                command(browser, args, profile), capture_output=True, text=True, timeout=120
            )
        except subprocess.TimeoutExpired:
            print("error: the browser did not finish within 2 minutes", file=sys.stderr)
            return 1
    if not args.out.is_file():
        tail = (done.stderr or done.stdout).strip().splitlines()[-5:]
        print("error: the browser wrote nothing:", *tail, sep="\n", file=sys.stderr)
        return 1
    print(shown(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
