"""Non-mutating Muse approval UI diagnostics.

Only emits numeric counts and known, non-sensitive UI structure signals.
Does NOT click any approval action, save screenshots, or log user content.
"""
import hashlib
import re
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright, Error
from check_muse_session import auth_status, load_state

CARD = '[data-testid="hatch-inline-approval-card"]'
SCOPE = CARD + ':not([data-hatch-background-approval-surface="true"])'
REVIEW = re.compile(
    r"(검토\s*필요|승인\s*요청|승인\s*대기|Needs?\s+review|Review\s+needed|"
    r"Approvals?|Pending\s+review|Review\s+requests?)", re.I
)

def count_visible(nodes, limit=120):
    total = nodes.count()
    visible = 0
    for i in range(min(total, limit)):
        try:
            if nodes.nth(i).is_visible(timeout=500):
                visible += 1
        except Error:
            pass
    return total, visible


def stats(page, stage):
    selectors = {
        "cards_all": CARD,
        "cards_nonbackground": SCOPE,
        "cards_background": CARD + '[data-hatch-background-approval-surface="true"]',
        "cards_panel": CARD + '[data-hatch-approval-surface="panel"]',
        "primary_action": '[data-hatch-approval-primary-action="true"]',
        "menu_triggers": '[aria-haspopup="menu"]',
        "role_tab": '[role="tab"]',
        "pressed_buttons": 'button[aria-pressed]',
        "expanded_buttons": 'button[aria-expanded]',
        "review_buttons": 'button, [role="button"]',
    }
    fields = {}
    for name, selector in selectors.items():
        if name == "review_buttons":
            continue
        total, shown = count_visible(page.locator(selector))
        fields[name] = f"{total}:{shown}"
    nav = page.get_by_role("button", name=REVIEW)
    nt, nv = count_visible(nav)
    fields["review_named_buttons"] = f"{nt}:{nv}"
    txt = page.get_by_text(REVIEW)
    tt, tv = count_visible(txt)
    fields["review_matching_text"] = f"{tt}:{tv}"
    frames = len(page.frames)
    fields["frames"] = str(frames)
    fields["signed_in"] = str(auth_status(page) == "AUTHENTICATED_UI_PRESENT")
    print(
        "MUSE_UI_DIAG stage=" + stage + " " +
        " ".join(f"{k}={v}" for k, v in fields.items()), flush=True
    )


def click_review_navigation(page):
    choices = (
        page.get_by_role("button", name=REVIEW),
        page.get_by_role("tab", name=REVIEW),
        page.locator('button[aria-pressed]').filter(has_text=REVIEW),
    )
    for group in choices:
        for i in range(min(group.count(), 15)):
            node = group.nth(i)
            try:
                if not node.is_visible(timeout=600):
                    continue
                node.click(timeout=1800)
                return "review_nav_clicked"
            except Error:
                continue
    return "review_nav_not_found"



def panel_signatures(page):
    signatures = set()
    cards = page.locator(SCOPE)
    for i in range(min(cards.count(), 15)):
        card = cards.nth(i)
        if not card.is_visible():
            continue
        try:
            content = card.inner_text(timeout=700)
            # Only use signatures in memory; never log card contents or hashes.
            signatures.add(hashlib.sha256(content.encode("utf-8")).digest())
        except Error:
            pass
    return signatures


def inspect_review_buttons(page):
    buttons = page.get_by_role("button", name=REVIEW)
    max_buttons = min(buttons.count(), 18)
    fingerprints = set()
    transitions = 0
    details = []
    fingerprints.update(panel_signatures(page))
    for i in range(max_buttons):
        buttons = page.get_by_role("button", name=REVIEW)
        if i >= buttons.count():
            break
        node = buttons.nth(i)
        if not node.is_visible():
            continue
        try:
            before = panel_signatures(page)
            expanded_before = node.get_attribute("aria-expanded") or "-"
            node.click(timeout=2000)
            page.wait_for_timeout(350)
            after = panel_signatures(page)
            fingerprints.update(after)
            if after != before:
                transitions += 1
            primary_visible = count_visible(page.locator(PRIMARY_ACTION))[1]
            details.append(
                f"{i}:{expanded_before}:{int(after != before)}:"
                f"{primary_visible}"
            )
        except Error:
            details.append(f"{i}:error")
    print(
        f"MUSE_UI_DIAG review_navigation_count={max_buttons} "
        f"card_transitions={transitions} unique_card_views={len(fingerprints)} "
        f"detail={','.join(details)}",
        flush=True,
    )

PRIMARY_ACTION = '[data-hatch-approval-primary-action="true"]'



def inspect_frame_card_counts(page):
    pieces = []
    for index, frame in enumerate(page.frames[:15]):
        try:
            all_count = frame.locator(CARD).count()
            visible_count = count_visible(frame.locator(CARD))[1]
            primary = frame.locator(PRIMARY_ACTION).count()
            pieces.append(f"{index}:{all_count}:{visible_count}:{primary}")
        except Error:
            pieces.append(f"{index}:error")
    print("MUSE_UI_DIAG frame_counts=" + ",".join(pieces), flush=True)


def inspect_review_button_structure(page):
    group = page.get_by_role("button", name=REVIEW)
    details = []
    for i in range(min(group.count(), 14)):
        node = group.nth(i)
        if not node.is_visible():
            continue
        try:
            expanded = node.get_attribute("aria-expanded")
            pressed = node.get_attribute("aria-pressed")
            structure = node.evaluate("""el => {
              const host = el.closest('[data-testid]');
              const role = el.closest('[role]');
              return {
                tag: el.tagName.toLowerCase(),
                withinCard: !!el.closest('[data-testid="hatch-inline-approval-card"]'),
                containerTestId: host?.getAttribute('data-testid') || '-',
                containerRole: role?.getAttribute('role') || '-'
              };
            }""")
            # No user data, prompts, URLs, IDs or request bodies.
            details.append(
                f"{i}:{expanded or '-'}:{pressed or '-'}:"
                f"{structure['tag']}:{int(structure['withinCard'])}:"
                f"{structure['containerTestId']}:{structure['containerRole']}"
            )
        except Error:
            details.append(f"{i}:error")
    print("MUSE_UI_DIAG review_button_structure=" + ",".join(details), flush=True)



def auth_probe(page, stage, response=None):
    """Only fixed classifications and counts; never print URLs or page content."""
    host = urlsplit(page.url).hostname or ""
    host_class = "muse" if host == "muse.ai" else (
        "muse_subdomain" if host.endswith(".muse.ai") else "other")
    headings = sum(page.get_by_role("heading", name=title, exact=True).count()
                   for title in ("로그인 또는 계정 만들기", "Log in or sign up", "Sign in"))
    signals = {
        "stage": stage,
        "status": auth_status(page),
        "host": host_class,
        "document_http": response.status if response else "unknown",
        "avatar": page.locator('[data-hatch-avatar-interaction="true"]').count(),
        "threads": page.locator('[data-testid="hatch-thread-row"]').count(),
        "login_headings": headings,
        "approval_cards": page.locator(CARD).count(),
    }
    print("MUSE_AUTH_PROBE " +
          " ".join(f"{key}={value}" for key, value in signals.items()),
          flush=True)


def main():
    state = load_state()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(storage_state=state, locale="ko-KR")
            page = context.new_page()
            response = page.goto("https://muse.ai/", wait_until="domcontentloaded", timeout=40000)
            page.wait_for_timeout(5000)
            auth_probe(page, "5s", response)
            if auth_status(page) != "AUTHENTICATED_UI_PRESENT":
                page.wait_for_timeout(6000)
                auth_probe(page, "11s", response)
            if auth_status(page) != "AUTHENTICATED_UI_PRESENT":
                page.wait_for_timeout(6000)
                auth_probe(page, "17s", response)
            stats(page, "landing")
            if auth_status(page) != "AUTHENTICATED_UI_PRESENT":
                return 2
            result = click_review_navigation(page)
            print("MUSE_UI_DIAG navigation=" + result, flush=True)
            page.wait_for_timeout(1600)
            stats(page, "after_review_navigation")
            inspect_review_buttons(page)
            stats(page, "after_review_sequence")
            inspect_frame_card_counts(page)
            inspect_review_button_structure(page)
            return 0
        finally:
            browser.close()

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("MUSE_UI_DIAG failed_type=" + type(exc).__name__, flush=True)
        raise SystemExit(2)
