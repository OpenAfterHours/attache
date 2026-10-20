# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Take a picture (PNG) or a PDF of an Attaché page with the Edge or Chrome already installed.

    render.py <url> <out.png|out.pdf> [--width 1280] [--height 900] [--wait 4]

<url> is a page on this machine, usually the `snapshot` link the artifact tool returns. The
browser runs headless with a throwaway profile and cannot reach the internet: every host name
but localhost and 127.0.0.1 fails to resolve, and anything else goes to a proxy that is not
there. The file is written into the current folder (the chat's files folder when the script
tool runs this), and its path, relative to the Attaché home when it is in one, is printed.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

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
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
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


def parse(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render a local web page to a PNG or a PDF.")
    parser.add_argument("url", help="a page on this machine, e.g. the artifact tool's snapshot")
    parser.add_argument("out", help="the file to write in the current folder: .png or .pdf")
    parser.add_argument("--width", type=int, default=1280, help="viewport width in pixels")
    parser.add_argument("--height", type=int, default=900, help="viewport height in pixels")
    parser.add_argument("--wait", type=float, default=4, help="seconds to let the page load")
    args = parser.parse_args(argv)
    parts = urlsplit(args.url)
    if parts.scheme not in ("http", "https") or parts.hostname not in LOCAL_HOSTS:
        parser.error("url must be an http(s) address on this machine (localhost or 127.0.0.1)")
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
