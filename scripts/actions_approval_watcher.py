"""GitHub Actions-friendly Muse approval-card watcher.

Works only inside the authenticated Muse UI. By default observes; apply mode
only clicks recognizable read-only website-access approvals for an exact host
on the explicitly configured allowlist. Never clicks connector write,
external-data-sharing, purchase, or ambiguous approval prompts.
"""
import argparse
import json
import os
import time
from pathlib import Path

from playwright.sync_api import Error, TimeoutError, sync_playwright

from approval_policy import (
    ALLOWED_TEXT_RE, ALWAYS_TEXT_RE, REJECT_TEXT_RE, decide, safe_policy,
)
from check_muse_session import auth_status, load_state

CARD = '[data-testid="hatch-inline-approval-card"]:not([data-hatch-background-approval-surface="true"])'
BACKGROUND_CARD = '[data-testid="hatch-inline-approval-card"][data-hatch-background-approval-surface="true"]'
SUB_ITEM = '[role="button"][aria-expanded]'
PRIMARY = '[data-hatch-approval-primary-action="true"]'
MENU = '[aria-haspopup="menu"]'


def label(node):
    try:
        return (node.inner_text(timeout=650) or "").strip()
    except Error:
        return ""


def visible(node):
    try:
        return node.is_visible(timeout=650)
    except Error:
        return False


def get_buttons(scope):
    """Require an Allow + Deny pair inside the same approval scope."""
    allow = []
    deny = []
    for button in scope.locator('button, [role="button"]').all():
        if not visible(button):
            continue
        name = label(button)
        if ALLOWED_TEXT_RE.fullmatch(name):
            allow.append(button)
        if REJECT_TEXT_RE.fullmatch(name):
            deny.append(button)
    return (allow[0], deny[0]) if len(allow) == 1 and len(deny) >= 1 else (None, None)


def approval_items(page):
    items = []
    for card in page.locator(CARD).all():
        if not visible(card):
            continue
        subs = [
            sub for sub in card.locator(SUB_ITEM).all()
            if sub.locator(PRIMARY).count() and visible(sub)
        ]
        if subs:
            for sub in subs:
                items.append((sub, label(sub)))
        else:
            items.append((card, label(card)))
    return items


def try_open_review_panel(page):
    """Only opens Muse's review tab; it never authorizes a tool call."""
    try:
        banner = page.locator(BACKGROUND_CARD)
        for i in range(min(banner.count(), 2)):
            buttons = banner.nth(i).locator("button")
            if buttons.count() and visible(buttons.first):
                buttons.first.click(timeout=1500)
                return True
        for tab in page.locator("button[aria-pressed]").all():
            title = (tab.get_attribute("aria-label") or "").strip().lower()
            if title in ("approvals", "승인", "검토 필요", "핵심 승인", "批准", "核可工具"):
                if tab.get_attribute("aria-pressed") != "true":
                    tab.click(timeout=1500)
                    return True
    except Error:
        pass
    return False


def choose_allow(page, scope, allow_button, prefer_always):
    """Offer scope-preserving Always allow for this site, else Allow once."""
    if prefer_always:
        for menu in scope.locator(MENU).all():
            if not visible(menu):
                continue
            try:
                menu.click(timeout=1500)
                page.wait_for_timeout(250)
                for choice in page.locator('[role="menuitem"]').all():
                    if not visible(choice):
                        continue
                    title = label(choice)
                    if ALWAYS_TEXT_RE.fullmatch(title) and (
                        "site" in title.lower() or "사이트" in title
                    ):
                        choice.click(timeout=1500)
                        return "always_site"
                page.keyboard.press("Escape")
            except Error:
                try:
                    page.keyboard.press("Escape")
                except Error:
                    pass
    allow_button.click(timeout=1500)
    return "once"


def process_cards(page, config, mode, attempted):
    scanned = approved = held = 0
    for scope, text in approval_items(page):
        if scanned >= 30:
            break
        scanned += 1
        decision, host, reason = decide(text, config["allowed_read_hosts"])
        # A positive-looking text without an Allow+Deny pair is never actionable.
        allow_btn, deny_btn = get_buttons(scope)
        if allow_btn is None or deny_btn is None:
            decision, reason = "hold", "missing_unambiguous_controls"
        # Hashes are deliberately not exported and nothing from card text is logged.
        # Each run stores a local record of buttons attempted, preventing duplicate
        # clicks if the UI fails to remove a card.
        token = (
            scope.get_attribute("data-hatch-approval-id") or
            scope.get_attribute("data-maa-approval-id") or
            text
        )
        if token in attempted:
            continue
        if decision != "allow_read" or not config["enabled"] or mode != "apply":
            held += 1
            print(f"MUSE_CARD=HELD reason={reason} host={host or '-'}")
            attempted.add(token)
            continue
        if approved >= config["max_approvals_per_run"]:
            break
        attempted.add(token)
        try:
            kind = choose_allow(page, scope, allow_btn, config.get("prefer_always_for_site", False))
            approved += 1
            print(f"MUSE_CARD=CLICKED host={host} kind={kind}")
            page.wait_for_timeout(800)
        except Error as exc:
            print(f"MUSE_CARD=CLICK_FAILED type={type(exc).__name__}")
    return scanned, approved, held


def run(mode, config):
    state = load_state()
    print("MUSE_WATCHER=START mode=" + mode)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(storage_state=state, locale="ko-KR")
            page = context.new_page()
            page.goto("https://muse.ai/", wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(5000)
            if auth_status(page) != "AUTHENTICATED_UI_PRESENT":
                print("MUSE_WATCHER=AUTH_REQUIRED")
                return 2
            end = time.monotonic() + config["scan_seconds"]
            attempted = set()
            total_scanned = total_approved = total_held = 0
            while time.monotonic() < end:
                if not approval_items(page):
                    try_open_review_panel(page)
                scanned, approved, held = process_cards(page, config, mode, attempted)
                total_scanned += scanned
                total_approved += approved
                total_held += held
                if total_approved >= config["max_approvals_per_run"]:
                    break
                page.wait_for_timeout(1000)
            print(f"MUSE_WATCHER=COMPLETE observed={total_scanned} clicked={total_approved} held={total_held}")
            return 0
        finally:
            browser.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("observe", "apply"), default="observe")
    parser.add_argument("--config", default="approval_policy.json")
    args = parser.parse_args()
    config = safe_policy(json.loads(Path(args.config).read_text(encoding="utf-8")))
    return run(args.mode, config)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"MUSE_WATCHER=ERROR type={type(exc).__name__}")
        raise SystemExit(2)
