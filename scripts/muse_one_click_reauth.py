"""Guarded one-click sign-in for an already remembered Muse account.

This does NOT type credentials, bypass MFA, or save/emit session data.
Never click a generic sign-in button on an untrusted or ambiguous host.
"""
import os
import re
from urllib.parse import urlsplit

from playwright.sync_api import Error

from check_muse_session import auth_status

EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
MUSE_CHOOSER = re.compile(
    r"(?:Muse\s*에\s*로그인|(?:Sign|Log)\s*in\s*to\s*Muse|Continue\s*to\s*Muse)",
    re.I,
)
OTHER_ACCOUNT = re.compile(
    r"(?:다른\s*계정\s*사용|Use\s+another\s+account|Other\s+account)",
    re.I,
)
LOGIN = re.compile(r"^(?:로그인|Sign\s*in|Log\s*in)$", re.I)


def allowed_host(url, extra_hosts=""):
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host:
        return False
    if host == "muse.ai" or host.endswith(".muse.ai"):
        return True
    # Optional explicit host allowlist for a separately hosted official
    # identity-provider chooser. Never wildcard an unknown provider.
    extras = {x.strip().lower() for x in extra_hosts.split(",") if x.strip()}
    return host in extras


def confirmed_account_chooser(frame, *, expected_email="", extra_hosts=""):
    """Return the unique button only if this is a recognized account chooser."""
    if not allowed_host(frame.url, extra_hosts):
        return None
    try:
        body = frame.locator("body").inner_text(timeout=1200)
        if not MUSE_CHOOSER.search(body) or not OTHER_ACCOUNT.search(body):
            return None
        accounts = {e.casefold() for e in EMAIL.findall(body)}
        if len(accounts) != 1:
            return None
        if expected_email and expected_email.strip().casefold() not in accounts:
            return None
        if frame.locator('input[type="password"], input[autocomplete="one-time-code"]').count():
            return None
        buttons = frame.get_by_role("button", name=LOGIN)
        if buttons.count() != 1 or not buttons.first.is_visible():
            return None
        return buttons.first
    except Error:
        return None


def account_chooser_targets(page):
    """Find both in-page and popup/iframe account chooser candidates."""
    pages = [page] + [p for p in page.context.pages if p is not page][:4]
    for p in pages:
        for f in p.frames[:15]:
            yield f


def _wait_for_auth(page, *, checks=15, interval_ms=1000):
    for i in range(checks):
        try:
            if auth_status(page) == "AUTHENTICATED_UI_PRESENT":
                return True
        except Error:
            pass
        if i < checks - 1:
            page.wait_for_timeout(interval_ms)
    return False


def _try_chooser(page, *, expected_email, extra_hosts):
    choices = []
    for frame in account_chooser_targets(page):
        btn = confirmed_account_chooser(
            frame, expected_email=expected_email, extra_hosts=extra_hosts,
        )
        if btn is not None:
            choices.append(btn)
    # Even multiple valid-looking chooser surfaces are ambiguous.
    if len(choices) != 1:
        return "AMBIGUOUS" if choices else "NOT_PRESENT"
    try:
        choices[0].click(timeout=3500)
    except Error:
        return "CLICK_FAILED"
    return "AUTHENTICATED" if _wait_for_auth(page) else "UNVERIFIED_AFTER_CLICK"


def try_one_click_reauth(page, *, expected_email=None, extra_hosts=None):
    """Attempt at most one remembered-account click after a logged-out result.

    Return a bounded classification without exposing account identifiers.
    """
    if os.environ.get("MUSE_ONE_CLICK_LOGIN_ENABLED", "true").lower() in {
        "0", "false", "no", "off"
    }:
        return "DISABLED"
    expected = (expected_email if expected_email is not None else
                os.environ.get("MUSE_EXPECTED_LOGIN_EMAIL", "")).strip()
    allowed_extra = (extra_hosts if extra_hosts is not None else
                     os.environ.get("MUSE_LOGIN_ALLOWED_HOSTS", ""))

    if auth_status(page) != "LOGIN_REQUIRED":
        return "NOT_LOGGED_OUT"

    outcome = _try_chooser(page, expected_email=expected,
                           extra_hosts=allowed_extra)
    if outcome != "NOT_PRESENT":
        return outcome

    # Optional first click opens the remembered account chooser from Muse's
    # own signed-out homepage. This is NOT a credential submission.
    if not allowed_host(page.url) or (
        page.locator('[data-testid="hatch-inline-approval-card"]').count()
    ):
        return "NOT_PRESENT"
    try:
        body = page.locator("body").inner_text(timeout=1000)
        entry = page.get_by_role("button", name=LOGIN)
        if (not re.search(r"(?:로그인|Sign\s*in|Log\s*in)", body, re.I)
                or entry.count() != 1 or not entry.first.is_visible()
                or page.locator('input[type="password"]').count()):
            return "NOT_PRESENT"
        entry.first.click(timeout=3500)
        page.wait_for_timeout(1500)
        if auth_status(page) == "AUTHENTICATED_UI_PRESENT":
            return "AUTHENTICATED"
        return _try_chooser(page, expected_email=expected,
                            extra_hosts=allowed_extra)
    except Error:
        return "ENTRY_FAILED"
