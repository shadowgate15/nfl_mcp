"""One-time, user-run script to stage ESPN Fantasy session cookies into `.env`.

Private ESPN Fantasy leagues require the `espn_s2` and `SWID` session cookies, which
only exist after a real login. This script launches a headed (non-headless) browser
at ESPN's login page, lets you log in by hand — including whatever 2FA/SSO/CAPTCHA
challenge ESPN's own page throws up — and then writes the two cookies into a
repo-root `.env` as `ESPN_S2`/`ESPN_SWID`, merging into any existing `.env` content.

Usage:
    uv sync --group espn
    uv run python scripts/espn_cookie_pull.py

The runtime tools read these values via plain `os.getenv("ESPN_S2")` /
`os.getenv("ESPN_SWID")` — see docs/adr/0002-espn-cookie-pull-script.md.
"""

from __future__ import annotations

import re
import sys
import threading
import time
from pathlib import Path

LOGIN_URL = "https://www.espn.com/login"
TIMEOUT_SECONDS = 300
POLL_INTERVAL_SECONDS = 2
REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"
COOKIE_ENV_NAMES = {"espn_s2": "ESPN_S2", "SWID": "ESPN_SWID"}


def _wait_for_enter(enter_pressed: threading.Event) -> None:
    input("\nPress Enter here once you've finished logging in in the browser window...\n")
    enter_pressed.set()


def _extract_cookies(context) -> dict[str, str]:
    found = {}
    for cookie in context.cookies():
        env_name = COOKIE_ENV_NAMES.get(cookie["name"])
        if env_name:
            found[env_name] = cookie["value"]
    return found


def _merge_env(existing_text: str, values: dict[str, str]) -> str:
    lines = existing_text.splitlines() if existing_text else []
    remaining = dict(values)

    merged_lines = []
    for line in lines:
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=", line)
        key = match.group(1) if match else None
        if key in remaining:
            merged_lines.append(f"{key}={remaining.pop(key)}")
        else:
            merged_lines.append(line)

    for key, value in remaining.items():
        merged_lines.append(f"{key}={value}")

    return "\n".join(merged_lines) + "\n"


def _write_env(values: dict[str, str]) -> None:
    existing_text = ENV_PATH.read_text() if ENV_PATH.exists() else ""
    ENV_PATH.write_text(_merge_env(existing_text, values))


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright isn't installed. Run: uv sync --group espn", file=sys.stderr)
        return 1

    enter_pressed = threading.Event()
    enter_thread = threading.Thread(target=_wait_for_enter, args=(enter_pressed,), daemon=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(LOGIN_URL)

        print(f"Log in to ESPN in the opened browser window (timeout: {TIMEOUT_SECONDS}s).")
        enter_thread.start()

        cookies: dict[str, str] = {}
        deadline = time.monotonic() + TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            cookies = _extract_cookies(context)
            if enter_pressed.is_set() or len(cookies) == len(COOKIE_ENV_NAMES):
                cookies = _extract_cookies(context)
                break
            time.sleep(POLL_INTERVAL_SECONDS)

        browser.close()

    missing = set(COOKIE_ENV_NAMES.values()) - set(cookies)
    if missing:
        print(
            f"Timed out after {TIMEOUT_SECONDS}s without capturing: {', '.join(sorted(missing))}",
            file=sys.stderr,
        )
        return 1

    _write_env(cookies)
    print(f"Wrote {', '.join(sorted(cookies))} to {ENV_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
