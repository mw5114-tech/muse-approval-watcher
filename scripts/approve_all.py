"""Muse approval queue handler for GitHub Actions.

Explicit account-owner opt-in mode: click every *recognized* Muse approval card
with a visible Allow button and a nearby Deny button, across chats and
scheduled tasks. Scope and risk are deliberately NOT filtered. This can
authorize uploads, external sharing and high-impact actions. Use with care.
Do not log raw cards, user content, cookies, or URLs.
"""
import argparse
import json
import re
import time
from pathlib import Path

from playwright.sync_api import Error, TimeoutError, sync_playwright

from check_muse_session import auth_status, load_state

CARD = '[data-testid="hatch-inline-approval-card"]:not([data-hatch-background-approval-surface="true"])'
BACKGROUND = '[data-testid="hatch-inline-approval-card"][data-hatch-background-approval-surface="true"]'
PRIMARY = '[data-hatch-approval-primary-action="true"]'
SUB = '[role="button"][aria-expanded]'
MENU = '[aria-haspopup="menu"]'
CONTROLS = 'button, [role="button"]'
ALLOW = re.compile(r"^(?:한\s*번만\s*허용|이번만\s*허용|허용|계속\s*허용|항상\s*허용|Allow(?:\s+(?:once|this time|always))?)$", re.I)
ALWAYS = re.compile(r"(?:계속\s*허용|항상\s*허용|영구\s*허용|Always\s+allow|Allow\s+always)", re.I)
DENY = re.compile(r"^(?:거부|차단|허용\s*안\s*함|Deny|Reject|Decline|Block)$", re.I)
REVIEW_TAB = {"approvals", "검토 필요", "승인", "批准", "核可工具"}
MAX_ITEMS_PER_PASS = 75


def visible(node):
    try:
        return node.is_visible(timeout=600)
    except Error:
        return False


def label(node):
    try:
        return (node.inner_text(timeout=700) or node.get_attribute("aria-label") or "").strip()
    except Error:
        return ""


def control_pair(scope):
    """Do not click general page buttons. Require the known approval surface."""
    allow = []
    deny = []
    for button in scope.locator(CONTROLS).all():
        if not visible(button):
            continue
        name = label(button)
        if DENY.fullmatch(name):
            deny.append(button)
        elif ALLOW.fullmatch(name) or button.get_attribute("data-hatch-approval-primary-action") == "true":
            allow.append(button)
    if not deny or not allow:
        return None
    # Prefer an ordinary one-time Allow as fallback; the persistent grant is
    # chosen separately by choose_allow when available.
    normal = [button for button in allow if not ALWAYS.search(label(button))]
    return normal[0] if normal else allow[0]


def approval_items(page):
    items = []
    for card in page.locator(CARD).all():
        if not visible(card):
            continue
        subs = []
        for sub in card.locator(SUB).all():
            if visible(sub) and sub.locator(PRIMARY).count():
                subs.append(sub)
        if subs:
            items.extend(subs)
        else:
            items.append(card)
    return items[:MAX_ITEMS_PER_PASS]


def open_review_panel(page):
    """Open the built-in approvals panel, not a permission-grant button."""
    try:
        banner = page.locator(BACKGROUND)
        for item in banner.all()[:3]:
            for button in item.locator("button").all():
                if visible(button):
                    button.click(timeout=1300)
                    return True
        tab_found = False
        for tab in page.locator("button[aria-pressed]").all():
            name = (tab.get_attribute("aria-label") or "").strip().lower()
            if name in REVIEW_TAB:
                tab_found = True
                if tab.get_attribute("aria-pressed") != "true":
                    tab.click(timeout=1300)
                    return True
                return False
        # Approvals sometimes live behind the account avatar.
        # Never toggle the avatar if the approval tab is already available.
        if not tab_found and not page.locator(CARD).count():
            avatar = page.locator('[data-hatch-avatar-interaction="true"]')
            if avatar.count() and visible(avatar.first):
                avatar.first.click(timeout=1300)
                return True
    except Error:
        pass
    return False


def expand_pending_preview(page):
    """Expand one collapsed approval row from Muse's review panel."""
    try:
        panels = page.locator(CARD + '[data-hatch-approval-surface="panel"]')
        if not panels.count():
            return False
        section = panels.first.locator("xpath=ancestor::section[1]")
        for preview in section.locator('button[aria-expanded="false"]').all():
            if not visible(preview):
                continue
            if preview.locator('xpath=ancestor::*[@data-testid="hatch-inline-approval-card"]').count():
                continue
            preview.click(timeout=1300)
            return True
    except Error:
        pass
    return False


def stable_id(scope):
    """Assign an ephemeral, non-secret ID so duplicate-looking cards aren't skipped."""
    try:
        server_id = (
            scope.get_attribute("data-hatch-approval-id")
            or scope.get_attribute("data-maa-approval-id")
        )
        if server_id:
            return "approval:" + server_id
        return scope.evaluate("""el => {
            if (!el.dataset.museWatcherId) {
              window.__museApprovalSequence = (window.__museApprovalSequence || 0) + 1;
              el.dataset.museWatcherId = String(window.__museApprovalSequence);
            }
            return el.dataset.museWatcherId;
        }""")
    except Error:
        return None


def choose_allow(page, scope, fallback, prefer_always):
    if prefer_always:
        # Some Muse cards render "Always allow" directly, others hide it in a menu.
        for button in scope.locator(CONTROLS).all():
            if visible(button) and ALWAYS.search(label(button)) and button != fallback:
                button.click(timeout=2000)
                return "always"
        for menu in scope.locator(MENU).all():
            if not visible(menu):
                continue
            try:
                menu.click(timeout=1800)
                page.wait_for_timeout(200)
                for option in page.locator('[role="menuitem"]').all():
                    if visible(option) and ALWAYS.search(label(option)):
                        option.click(timeout=1800)
                        return "always"
            except Error:
                pass
            try:
                page.keyboard.press("Escape")
            except Error:
                pass
    fallback.click(timeout=2000)
    return "once"


def scan_once(page, mode, seen, prefer_always, max_clicks):
    scanned = clicked = blocked = 0
    for scope in approval_items(page):
        if clicked >= max_clicks:
            break
        key = stable_id(scope)
        if key is None or key in seen:
            continue
        scanned += 1
        allow = control_pair(scope)
        if allow is None:
            blocked += 1
            seen.add(key)
            print("MUSE_APPROVAL=UNRECOGNIZED", flush=True)
            continue
        if mode == "observe":
            seen.add(key)
            print("MUSE_APPROVAL=DETECTED", flush=True)
            continue
        seen.add(key)   # Never double-click the same DOM approval within a run.
        try:
            kind = choose_allow(page, scope, allow, prefer_always)
            clicked += 1
            # Muse reacts asynchronously. Wait briefly for the approval card
            # to disappear before classifying this as UI-confirmed.
            # It is still NOT proof the requested external action succeeded.
            try:
                scope.wait_for(state="hidden", timeout=1700)
                result = "DISMISSED"
            except (TimeoutError, Error):
                result = "UNCONFIRMED"
            print(f"MUSE_APPROVAL=CLICKED kind={kind} ui={result}", flush=True)
        except Error as error:
            print(f"MUSE_APPROVAL=CLICK_FAILED type={type(error).__name__}", flush=True)
    return scanned, clicked, blocked


def validate_config(config):
    if not isinstance(config.get("enabled"), bool):
        raise ValueError("enabled must be boolean")
    if not isinstance(config.get("prefer_always"), bool):
        raise ValueError("prefer_always must be boolean")
    if not isinstance(config.get("approve_all"), bool):
        raise ValueError("approve_all must be boolean")
    count = config.get("max_approvals_per_run")
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 100:
        raise ValueError("max_approvals_per_run must be 1..100")
    seconds = config.get("scan_seconds")
    if not isinstance(seconds, int) or isinstance(seconds, bool) or not 5 <= seconds <= 270:
        raise ValueError("scan_seconds must be 5..270")
    return config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("observe", "apply"), default="observe")
    parser.add_argument("--config", default="approval_policy.json")
    args = parser.parse_args()
    config = validate_config(json.loads(Path(args.config).read_text(encoding="utf-8")))
    mode = args.mode if config["enabled"] and (args.mode == "observe" or config["approve_all"]) else "observe"
    state = load_state()
    print(f"MUSE_WATCHER=START mode={mode}", flush=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(storage_state=state, locale="ko-KR")
            page = context.new_page()
            # A transient navigation failure should not permanently skip a run.
            for attempt in range(2):
                try:
                    page.goto("https://muse.ai/", wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(3500)
                    if auth_status(page) == "AUTHENTICATED_UI_PRESENT":
                        break
                except Error:
                    if attempt:
                        print("MUSE_WATCHER=NAVIGATION_FAILED", flush=True)
                        return 2
                if attempt == 0:
                    page.wait_for_timeout(2000)
            else:
                print("MUSE_WATCHER=AUTH_REQUIRED_OR_SITE_UNAVAILABLE", flush=True)
                return 2
            end = time.monotonic() + config["scan_seconds"]
            seen = set()
            totals = [0, 0, 0]
            last_panel_open = 0
            last_heartbeat = time.monotonic()
            while time.monotonic() < end:
                now = time.monotonic()
                if now - last_panel_open >= 10:
                    # Open the review panel even when the active chat has
                    # approval cards; other scheduled tasks may be waiting.
                    open_review_panel(page)
                    expand_pending_preview(page)
                    last_panel_open = now
                if now - last_heartbeat >= 45:
                    if auth_status(page) != "AUTHENTICATED_UI_PRESENT":
                        print("MUSE_WATCHER=AUTH_LOST_DURING_RUN", flush=True)
                        return 2
                    print(
                        f"MUSE_HEARTBEAT scanned={totals[0]} clicked={totals[1]} "
                        f"unrecognized={totals[2]}",
                        flush=True,
                    )
                    last_heartbeat = now
                scanned, clicked, blocked = scan_once(
                    page, mode, seen, config["prefer_always"],
                    config["max_approvals_per_run"] - totals[1],
                )
                totals[0] += scanned
                totals[1] += clicked
                totals[2] += blocked
                if totals[1] >= config["max_approvals_per_run"]:
                    print("MUSE_WATCHER=LIMIT_REACHED", flush=True)
                    break
                page.wait_for_timeout(1000)
            print(f"MUSE_WATCHER=COMPLETE observed={totals[0]} clicked={totals[1]} unrecognized={totals[2]}", flush=True)
            return 0
        finally:
            browser.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"MUSE_WATCHER=ERROR type={type(error).__name__}", flush=True)
        raise SystemExit(2)
