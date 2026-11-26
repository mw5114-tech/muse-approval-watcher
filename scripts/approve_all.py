"""Muse approval queue handler for GitHub Actions.

Explicit account-owner opt-in mode: click every *recognized* Muse approval card
with a visible Allow button and a nearby Deny button, across chats and
scheduled tasks. Scope and risk are deliberately NOT filtered. This can
authorize uploads, external sharing and high-impact actions. Use with care.
Do not log raw cards, user content, cookies, or URLs.
"""
import argparse
import hashlib
import json
import os
import re
import time
from pathlib import Path

from playwright.sync_api import Error, TimeoutError, sync_playwright

from check_muse_session import auth_status, load_state
from muse_one_click_reauth import try_one_click_reauth

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
REVIEW_LABEL = re.compile(
    r"(검토\s*필요|승인\s*요청|승인\s*대기|Needs?\s+review|Review\s+needed|"
    r"Approvals?|Pending\s+review|Review\s+requests?)", re.I
)
STATUS_PANEL = '[data-testid="hatch-status-panel-sliding-surface"]'
PANEL_CARD = '[data-testid="hatch-inline-approval-card"][data-hatch-approval-surface="panel"]'

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


def approval_items(page, *, panel_only=False):
    items = []
    target = PANEL_CARD if panel_only else CARD
    for card in page.locator(target).all():
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



def review_rows(page):
    """Only review-needed accordion buttons in Muse's actual status panel."""
    return page.locator(STATUS_PANEL).get_by_role("button", name=REVIEW_LABEL).and_(page.locator("[aria-expanded]"))


def open_review_row(page, index):
    """Select one distinct pending-review row; never clicks Allow or Deny."""
    rows = review_rows(page)
    count = rows.count()
    if count == 0:
        return 0, False
    index = index % count
    selected = rows.nth(index)
    try:
        if not visible(selected):
            return count, False
        if selected.get_attribute("aria-expanded") != "true":
            selected.click(timeout=1800)
            page.wait_for_timeout(650)
        # Only process a card if the intended row actually became expanded.
        return count, review_rows(page).nth(index).get_attribute("aria-expanded") == "true"
    except Error:
        return count, False


REVIEW_NAV = re.compile(r"^(?:검토|검토 필요|승인|승인 요청|권한|Review|Approvals?|Permissions?)$", re.I)
PENDING_BANNER = re.compile(r"(?:검토가 필요한 작업\\s*[1-9][0-9]*개|[1-9][0-9]*\\s*(?:tasks?|items?)\\s*(?:need|require)\\s*review)", re.I)
EMPTY_REVIEW = re.compile(r"^(?:검토가 필요한 작업이 없습니다|검토할 작업이 없습니다|검토할 항목이 없습니다|검토 필요 없음|승인 요청이 없습니다|No (?:pending approvals|items to review|tasks to review|review requests))\\.?$", re.I)


def visible_any(nodes):
    return any(visible(n) for n in nodes.all()[:75])


def review_tabs(page):
    panel = page.locator(STATUS_PANEL)
    return panel.locator('button[aria-pressed], [role="tab"], button[aria-selected]')


def review_tab_match(tab):
    # Use exact known accessibility names or an explicit shield icon on a tab.
    names = [label(tab), tab.get_attribute("aria-label") or "", tab.get_attribute("title") or ""]
    if any(REVIEW_NAV.fullmatch(name.strip()) for name in names):
        return True
    return bool(tab.locator('svg.lucide-shield-check, svg[data-icon="shield-check"], svg[data-testid="shield-check"]').count())


def review_tab_selected(tab):
    return tab.get_attribute("aria-pressed") == "true" or tab.get_attribute("aria-selected") == "true"


def review_state(page):
    """A zero-card count is not evidence that the review queue is empty."""
    panel = page.locator(STATUS_PANEL)
    if not visible_any(panel):
        return "UNVERIFIED"
    if visible_any(panel.locator(CARD)) or visible_any(page.locator(PANEL_CARD)) or visible_any(review_rows(page)):
        return "PENDING"
    if visible_any(page.get_by_text(PENDING_BANNER)):
        return "PENDING_NOT_RENDERED"
    if visible_any(panel.get_by_text(EMPTY_REVIEW)):
        return "EMPTY_CONFIRMED"
    return "UNVERIFIED"


def open_review_panel(page):
    """Use either the chat Review button or the actual account shield tab."""
    try:
        if review_state(page) in {"PENDING", "EMPTY_CONFIRMED"}:
            return True
        # The composer banner is explicit navigation, not a permission control.
        for role in ("button", "link"):
            for button in page.get_by_role(role, name=re.compile(r"^(?:검토|Review)$", re.I)).all()[:12]:
                if not visible(button):
                    continue
                if button.locator('xpath=ancestor::*[@data-testid="hatch-inline-approval-card" or @data-testid="hatch-status-panel-sliding-surface"]').count():
                    continue
                button.click(timeout=1800)
                page.wait_for_timeout(650)
                return True
        panel = page.locator(STATUS_PANEL)
        if not visible_any(panel):
            avatar = page.locator('[data-hatch-avatar-interaction="true"]')
            for node in avatar.all()[:5]:
                if visible(node):
                    node.click(timeout=1800)
                    page.wait_for_timeout(650)
                    break
        for tab in review_tabs(page).all()[:12]:
            if visible(tab) and review_tab_match(tab):
                if not review_tab_selected(tab):
                    tab.click(timeout=1800)
                    page.wait_for_timeout(650)
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


def card_fingerprint(scope):
    """In-memory only. Do not log request text or its hash."""
    try:
        text = scope.inner_text(timeout=700)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
    except Error:
        return None


def wait_card_gone(page, original_handle, timeout_ms=2200):
    """Observe the PRE-CLICK element, not a .nth() locator that may reindex.

    A card disappearing is a UI acknowledgement, NOT proof that an external
    tool call or other side effect succeeded.
    """
    if original_handle is None:
        return False
    try:
        page.wait_for_function(
            """el => !el.isConnected || el.hidden ||
              el.getClientRects().length === 0 ||
              getComputedStyle(el).visibility === 'hidden'""",
            arg=original_handle,
            timeout=timeout_ms,
        )
        return True
    except (TimeoutError, Error):
        return False


def scan_once(page, mode, seen, prefer_always, max_clicks, *,
              retry_state=None, max_attempts=1,
              retry_delay_seconds=6.0, confirmation_timeout_ms=2200,
              panel_only=False, identity_prefix=""):
    """Process the queue by re-fetching DOM after EACH click.

    With retry_state set, re-click only the SAME still-visible, unchanged
    approval surface. Each card is attempted at most max_attempts times.
    A retry is never taken as evidence that the prior request failed; it is
    therefore bounded and logged as UI-unconfirmed.
    """
    scanned = clicked = blocked = 0
    inspected_this_pass = set()
    if retry_state is None:
        retry_state = {}
        max_attempts = 1

    while scanned < MAX_ITEMS_PER_PASS and clicked < max_clicks:
        selected = None
        # Fresh list on every iteration: Playwright nth() locators shift when
        # a preceding approval disappears after an action.
        for scope in approval_items(page, panel_only=panel_only):
            card_id = stable_id(scope)
            # A real approval ID is globally unique across review rows.
            # The row prefix is only a fallback for DOM-only temporary IDs.
            key = (
                card_id if card_id and card_id.startswith("approval:")
                else identity_prefix + card_id if card_id is not None else None
            )
            if key is None or key in seen or key in inspected_this_pass:
                continue
            record = retry_state.get(key)
            if record and time.monotonic() < record["next_retry_at"]:
                continue
            selected = (scope, key, record)
            break

        if selected is None:
            break
        scope, key, record = selected
        inspected_this_pass.add(key)
        scanned += 1

        if record and record["fingerprint"] != card_fingerprint(scope):
            seen.add(key)
            retry_state.pop(key, None)
            blocked += 1
            print("MUSE_APPROVAL=CHANGED_REVIEW_NEEDED", flush=True)
            continue

        allow = control_pair(scope)
        if allow is None or not visible(scope) or (not prefer_always and (not ALLOW.fullmatch(label(allow)) or ALWAYS.search(label(allow)))):
            # It may be transitioning after a previous click. Avoid a blind
            # repeat on an ambiguous or disabled approval surface.
            seen.add(key)
            retry_state.pop(key, None)
            blocked += 1
            print("MUSE_APPROVAL=UNRECOGNIZED_OR_TRANSITIONING", flush=True)
            continue
        try:
            if not allow.is_enabled(timeout=700):
                seen.add(key)
                retry_state.pop(key, None)
                blocked += 1
                print("MUSE_APPROVAL=DISABLED_REVIEW_NEEDED", flush=True)
                continue
        except Error:
            seen.add(key)
            retry_state.pop(key, None)
            blocked += 1
            continue

        if mode == "observe":
            seen.add(key)
            print("MUSE_APPROVAL=DETECTED", flush=True)
            continue

        if record is None:
            record = {
                "attempts": 0,
                "next_retry_at": 0.0,
                "fingerprint": card_fingerprint(scope),
            }
            retry_state[key] = record

        record["attempts"] += 1
        try:
            # Capture the exact DOM node BEFORE clicking; other cards may shift.
            original_handle = scope.element_handle(timeout=1000)
            if original_handle is None:
                raise Error("Approval element not available")
            kind = choose_allow(page, scope, allow, prefer_always)
            clicked += 1
            dismissed = wait_card_gone(page, original_handle, timeout_ms=confirmation_timeout_ms)
            if dismissed:
                seen.add(key)
                retry_state.pop(key, None)
                print(
                    f"MUSE_APPROVAL=CLICKED attempt={record['attempts']} "
                    f"kind={kind} ui=DISMISSED",
                    flush=True,
                )
                continue

            if record["attempts"] >= max_attempts:
                seen.add(key)
                retry_state.pop(key, None)
                print(
                    f"MUSE_APPROVAL=RETRY_EXHAUSTED attempts={record['attempts']} "
                    "ui=UNCONFIRMED",
                    flush=True,
                )
            else:
                record["next_retry_at"] = time.monotonic() + retry_delay_seconds
                print(
                    f"MUSE_APPROVAL=RETRY_SCHEDULED attempt={record['attempts']}"
                    f"/{max_attempts} ui=UNCONFIRMED",
                    flush=True,
                )
        except Error as error:
            # A Playwright click error can happen AFTER dispatching the event.
            # Never immediately click again; let a later scan verify the DOM.
            if record["attempts"] >= max_attempts:
                seen.add(key)
                retry_state.pop(key, None)
            else:
                record["next_retry_at"] = time.monotonic() + retry_delay_seconds
            print(
                f"MUSE_APPROVAL=CLICK_FAILED attempt={record['attempts']} "
                f"type={type(error).__name__}",
                flush=True,
            )
    return scanned, clicked, blocked


def validate_config(config):
    if not isinstance(config.get("enabled"), bool):
        raise ValueError("enabled must be boolean")
    if not isinstance(config.get("prefer_always"), bool):
        raise ValueError("prefer_always must be boolean")
    if not isinstance(config.get("approve_all"), bool):
        raise ValueError("approve_all must be boolean")
    attempts = config.get("max_attempts_per_card", 3)
    if not isinstance(attempts, int) or isinstance(attempts, bool) or not 1 <= attempts <= 3:
        raise ValueError("max_attempts_per_card must be 1..3")
    retry_delay = config.get("retry_delay_seconds", 6)
    if not isinstance(retry_delay, (int, float)) or isinstance(retry_delay, bool) or not 4 <= retry_delay <= 30:
        raise ValueError("retry_delay_seconds must be 4..30")
    count = config.get("max_approvals_per_run")
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 100:
        raise ValueError("max_approvals_per_run must be 1..100")
    seconds = config.get("scan_seconds")
    if not isinstance(seconds, int) or isinstance(seconds, bool) or not 5 <= seconds <= 270:
        raise ValueError("scan_seconds must be 5..270")
    return config



def wait_for_authenticated_ui(page, *, checks=18, interval_ms=1000,
                              status_fn=auth_status):
    """Wait for a real signed-in UI, not just DOMContentLoaded.

    Fixed-interval polling avoids falsely declaring a valid but slowly
    hydrated page logged out after an arbitrary short sleep.
    """
    if checks < 1:
        raise ValueError("checks must be positive")
    status = "INCONCLUSIVE"
    login_streak = 0
    for index in range(checks):
        status = status_fn(page)
        if status == "AUTHENTICATED_UI_PRESENT":
            return status
        # Five consecutive explicit login prompts are sufficient evidence
        # that waiting 18 seconds on this same signed-out DOM is futile.
        # One transient login-looking frame never stops a possible hydrate.
        login_streak = login_streak + 1 if status == "LOGIN_REQUIRED" else 0
        if login_streak >= 5:
            return "LOGIN_REQUIRED"
        if index + 1 < checks:
            page.wait_for_timeout(interval_ms)
    return status


def establish_authenticated_session(page, *, attempts=3,
                                    checks_per_attempt=18,
                                    interval_ms=1000):
    """Retry rendering on the same stored session; never auto-login.

    Logs only bounded status categories and the HTTP status code for the
    root document. Do not emit redirected URLs, response bodies, or cookies.
    """
    statuses = []
    for attempt in range(attempts):
        http_status = "unknown"
        try:
            # Reload is useful for a transiently blank or unhydrated SPA.
            response = (
                page.goto("https://muse.ai/", wait_until="domcontentloaded",
                          timeout=30000)
                if attempt == 0
                else page.reload(wait_until="domcontentloaded", timeout=30000)
            )
            if response is not None:
                http_status = response.status
            status = wait_for_authenticated_ui(
                page, checks=checks_per_attempt, interval_ms=interval_ms,
            )
        except Error:
            status = "NAVIGATION_ERROR"
        statuses.append(status)
        print(
            f"MUSE_STARTUP_CHECK attempt={attempt + 1}/{attempts} "
            f"status={status} document_http={http_status}", flush=True
        )
        if status == "AUTHENTICATED_UI_PRESENT":
            return True
        # Verify a real sign-in prompt across one fresh page load.
        # A persistent login checkpoint cannot be resolved by further
        # reloads using the same invalid stored session.
        if len(statuses) >= 2 and statuses[-2:] == ["LOGIN_REQUIRED"] * 2:
            break
        if attempt + 1 < attempts:
            page.wait_for_timeout(2000 * (attempt + 1))
    if statuses and all(s == "LOGIN_REQUIRED" for s in statuses):
        # Do not loop passwords or silently trust a sign-in button.
        # One guarded click is permitted only on a unique remembered-Muse
        # account chooser hosted by a trusted domain.
        outcome = try_one_click_reauth(page)
        print(f"MUSE_REAUTH result={outcome}", flush=True)
        if outcome == "AUTHENTICATED":
            return True
        failure = "LOGIN_REQUIRED"
    elif statuses and all(s == "NAVIGATION_ERROR" for s in statuses):
        failure = "NAVIGATION_FAILED"
    else:
        failure = "AUTH_UNVERIFIED_OR_UI_UNAVAILABLE"
    print(f"MUSE_WATCHER=STARTUP_FAILED reason={failure}", flush=True)
    if failure == "LOGIN_REQUIRED":
        print("MUSE_WATCHER=ACTION_REQUIRED refresh_muse_session_secret", flush=True)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        guidance = (
            "The signed-in Muse UI was not available. Re-sign in on a trusted "
            "computer and refresh MUSE_AUTH_B64 through the repository's "
            "existing local session exporter. No approval actions were sent."
            if failure == "LOGIN_REQUIRED"
            else "Muse UI could not be verified. Check site availability and "
                 "authentication; no approval actions were sent."
        )
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(
                "## Muse watcher startup blocked\n\n"
                f"- Classification: \x60{failure}\x60\n"
                f"- Next step: {guidance}\n"
            )
    return False


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
            context = browser.new_context(storage_state=state, locale="ko-KR", viewport={"width": 1680, "height": 900})
            page = context.new_page()
            # Page readiness is independent of HTTP 200 / DOMContentLoaded.
            # Never approve when the account UI remains unverified.
            if not establish_authenticated_session(page):
                return 2
            end = time.monotonic() + config["scan_seconds"]
            seen = set()
            retry_state = {}
            totals = [0, 0, 0]
            last_panel_open = 0
            review_cursor = 0
            visited_review_rows = set()
            last_heartbeat = time.monotonic()
            while time.monotonic() < end:
                now = time.monotonic()
                row_count = review_rows(page).count()
                selected_review_row = None
                if row_count:
                    # Muse renders one panel approval at a time, even with
                    # multiple review-needed task accordion entries. Traverse
                    # them explicitly instead of staying on the first.
                    index = review_cursor % row_count
                    _, row_open = open_review_row(page, index)
                    review_cursor = (index + 1) % row_count
                    if row_open:
                        selected_review_row = index
                        visited_review_rows.add(index)
                elif now - last_panel_open >= 10:
                    open_review_panel(page)
                    expand_pending_preview(page)
                    last_panel_open = now
                if now - last_heartbeat >= 45:
                    if auth_status(page) != "AUTHENTICATED_UI_PRESENT":
                        print("MUSE_WATCHER=AUTH_LOST_DURING_RUN", flush=True)
                        return 2
                    print(
                        f"MUSE_HEARTBEAT scanned={totals[0]} clicked={totals[1]} "
                        f"unrecognized={totals[2]} review_state={review_state(page)}",
                        flush=True,
                    )
                    last_heartbeat = now
                scanned, clicked, blocked = scan_once(
                    page, mode, seen, config["prefer_always"],
                    config["max_approvals_per_run"] - totals[1],
                    retry_state=retry_state,
                    max_attempts=config.get("max_attempts_per_card", 3),
                    retry_delay_seconds=config.get("retry_delay_seconds", 6),
                    confirmation_timeout_ms=2200,
                    panel_only=selected_review_row is not None,
                    identity_prefix=(
                        f"review-row:{selected_review_row}:"
                        if selected_review_row is not None else ""
                    ),
                )
                totals[0] += scanned
                totals[1] += clicked
                totals[2] += blocked
                if totals[1] >= config["max_approvals_per_run"]:
                    print("MUSE_WATCHER=LIMIT_REACHED", flush=True)
                    break
                page.wait_for_timeout(1000)
            print(
                f"MUSE_WATCHER=COMPLETE observed={totals[0]} clicked={totals[1]} "
                f"unrecognized={totals[2]} review_rows_visited={len(visited_review_rows)}",
                flush=True,
            )
            final_review_state = review_state(page)
            print(f"MUSE_REVIEW_STATE={final_review_state}", flush=True)
            if final_review_state == "UNVERIFIED" or (mode == "apply" and final_review_state != "EMPTY_CONFIRMED"):
                print("MUSE_WATCHER=REVIEW_NOT_VERIFIED", flush=True)
                return 3
            return 0
        finally:
            browser.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"MUSE_WATCHER=ERROR type={type(error).__name__}", flush=True)
        raise SystemExit(2)
