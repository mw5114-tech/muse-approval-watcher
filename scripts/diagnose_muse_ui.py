"""Non-mutating Muse approval UI diagnostics.

Only emits numeric counts and known, non-sensitive UI structure signals.
Does NOT click any approval action, save screenshots, or log user content.
"""
import re
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


def main():
    state = load_state()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(storage_state=state, locale="ko-KR")
            page = context.new_page()
            page.goto("https://muse.ai/", wait_until="domcontentloaded", timeout=40000)
            page.wait_for_timeout(5000)
            stats(page, "landing")
            if auth_status(page) != "AUTHENTICATED_UI_PRESENT":
                return 2
            result = click_review_navigation(page)
            print("MUSE_UI_DIAG navigation=" + result, flush=True)
            page.wait_for_timeout(1600)
            stats(page, "after_review_navigation")
            return 0
        finally:
            browser.close()

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("MUSE_UI_DIAG failed_type=" + type(exc).__name__, flush=True)
        raise SystemExit(2)
