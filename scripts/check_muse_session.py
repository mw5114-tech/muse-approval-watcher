"""Non-mutating GitHub Actions smoke check for user-provided Muse authentication state.
Never emits cookies, tokens, page content, screenshots, or complete page URLs.
"""
import base64
import gzip
import json
import os
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

def load_state():
    encoded = os.environ.get("MUSE_AUTH_B64", "")
    if not encoded:
        raise ValueError("MUSE_AUTH_B64 is not set")
    raw = gzip.decompress(base64.b64decode(encoded, validate=True))
    if len(raw) > 2_000_000:
        raise ValueError("Authentication state is unexpectedly large")
    state = json.loads(raw.decode("utf-8"))
    if (not isinstance(state, dict)
            or not isinstance(state.get("cookies"), list)
            or not isinstance(state.get("origins"), list)):
        raise ValueError("Invalid Playwright storage state")
    return state

def auth_status(page):
    if urlsplit(page.url).hostname != "muse.ai":
        return "LOGIN_REQUIRED"
    logged_in_selectors = [
        '[data-hatch-avatar-interaction="true"]',
        '[data-testid="hatch-thread-row"]',
    ]
    if any(page.locator(selector).count() > 0 for selector in logged_in_selectors):
        return "AUTHENTICATED_UI_PRESENT"
    logged_out_titles = ["로그인 또는 계정 만들기", "Log in or sign up", "Sign in"]
    if any(page.get_by_role("heading", name=title, exact=True).count()
           for title in logged_out_titles):
        return "LOGIN_REQUIRED"
    return "INCONCLUSIVE"

def main():
    try:
        state = load_state()
    except (ValueError, OSError, json.JSONDecodeError, gzip.BadGzipFile) as exc:
        print(f"MUSE_AUTH_CHECK=INVALID_SECRET ({type(exc).__name__})")
        return 2
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(storage_state=state, locale="ko-KR")
            page = context.new_page()
            page.goto("https://muse.ai/", wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(8000)
            status = auth_status(page)
            print(f"MUSE_AUTH_CHECK={status}")
            return 0 if status == "AUTHENTICATED_UI_PRESENT" else 2
        finally:
            browser.close()

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"MUSE_AUTH_CHECK=ERROR ({type(exc).__name__})")
        raise SystemExit(2)
