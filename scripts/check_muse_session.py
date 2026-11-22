"""Non-mutating GitHub Actions smoke check for user-provided Muse authentication state.
Never emits cookies, tokens, page content, screenshots, or complete page URLs.
"""
import base64
import gzip
import hashlib
import json
import os
import re
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, Error

def read_encoded_secret():
    single = os.environ.get("MUSE_AUTH_B64", "").strip()
    parts = [os.environ.get(f"MUSE_AUTH_B64_{i:02d}", "").strip()
             for i in range(1, 13)]
    if single and any(parts):
        raise ValueError("Remove obsolete MUSE_AUTH_B64 when using split secrets")
    if single:
        return single
    if not parts[0]:
        raise ValueError("No Muse login secrets configured")
    header = parts[0].split(":", 3)
    if len(header) != 4 or header[0] != "muse1":
        raise ValueError("Split Muse secret header is invalid")
    try:
        count = int(header[1])
    except ValueError:
        raise ValueError("Split Muse secret count is invalid") from None
    if not (2 <= count <= len(parts)):
        raise ValueError("Split Muse secret count is out of range")
    if not all(parts[:count]) or any(parts[count:]):
        raise ValueError("One or more Muse secret parts are missing or unexpected")
    encoded = header[3] + "".join(parts[1:count])
    if hashlib.sha256(encoded.encode("ascii")).hexdigest()[:16] != header[2]:
        raise ValueError("Muse secret part integrity check failed")
    return encoded


def load_state():
    encoded = read_encoded_secret()
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
    # Muse occasionally changes its login layout: the top-level sign-in
    # prompt can be ordinary text or a button instead of an H1/H2. Never
    # misclassify this as signed-in: we only test it after app UI signals.
    try:
        screen_text = page.locator("body").inner_text(timeout=700)
        if re.search(r"(?<![a-z])(?:sign\s*in|log\s*in|로그인)(?![a-z])",
                     screen_text, re.I):
            return "LOGIN_REQUIRED"
    except Error:
        pass
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
