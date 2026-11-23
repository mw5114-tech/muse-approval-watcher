"""Offline synthetic DOM tests; no real Muse approvals are sent."""
import sys
import unittest
from unittest.mock import patch
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from approve_all import (scan_once, validate_config, open_review_row, review_rows,
                         wait_for_authenticated_ui, establish_authenticated_session)
from check_muse_session import auth_status


CONFIG = {
    "enabled": True, "approve_all": True, "prefer_always": True,
    "max_approvals_per_run": 50, "scan_seconds": 120,
}


class AllApprovalsTests(unittest.TestCase):
    def test_changed_muse_login_screen_is_detected(self):
        # Offline network mock; does not contact Muse or touch session tokens.
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.route("https://muse.ai/", lambda route: route.fulfill(
                    status=200, content_type="text/html",
                    body="<html><body><div>Sign in to Muse</div><button>Sign in</button></body></html>"
                ))
                page.goto("https://muse.ai/")
                self.assertEqual(auth_status(page), "LOGIN_REQUIRED")
                page.set_content("""<div data-hatch-avatar-interaction="true"></div>
                    <button>Sign in to your mailbox</button>""")
                self.assertEqual(auth_status(page), "AUTHENTICATED_UI_PRESENT")
            finally:
                browser.close()

    def test_auth_poll_waits_for_hydrated_muse_ui(self):
        class FakePage:
            polls = 0
            def wait_for_timeout(self, milliseconds):
                self.polls += 1
        page = FakePage()
        states = iter(["INCONCLUSIVE", "INCONCLUSIVE", "AUTHENTICATED_UI_PRESENT"])
        result = wait_for_authenticated_ui(
            page, checks=4, interval_ms=1, status_fn=lambda _: next(states)
        )
        self.assertEqual(result, "AUTHENTICATED_UI_PRESENT")
        self.assertEqual(page.polls, 2)


    def test_auth_login_prompt_waits_for_consistent_ui_then_returns(self):
        class FakePage:
            polls = 0
            def wait_for_timeout(self, milliseconds):
                self.polls += 1
        page = FakePage()
        states = iter(["LOGIN_REQUIRED"] * 10)
        result = wait_for_authenticated_ui(
            page, checks=18, interval_ms=1, status_fn=lambda _: next(states)
        )
        self.assertEqual(result, "LOGIN_REQUIRED")
        self.assertEqual(page.polls, 4)

    def test_transient_login_prompt_does_not_block_eventual_auth(self):
        class FakePage:
            polls = 0
            def wait_for_timeout(self, milliseconds):
                self.polls += 1
        page = FakePage()
        states = iter([
            "LOGIN_REQUIRED", "LOGIN_REQUIRED", "INCONCLUSIVE",
            "LOGIN_REQUIRED", "AUTHENTICATED_UI_PRESENT",
        ])
        self.assertEqual(wait_for_authenticated_ui(
            page, checks=10, interval_ms=1,
            status_fn=lambda _: next(states),
        ), "AUTHENTICATED_UI_PRESENT")

    def test_two_confirmed_login_pages_abort_futile_third_attempt(self):
        class FakeResponse:
            status = 200
        class FakePage:
            navigations = 0
            reloads = 0
            def goto(self, *args, **kwargs):
                self.navigations += 1
                return FakeResponse()
            def reload(self, *args, **kwargs):
                self.reloads += 1
                return FakeResponse()
            def wait_for_timeout(self, milliseconds):
                pass
        page = FakePage()
        with patch("approve_all.wait_for_authenticated_ui",
                   return_value="LOGIN_REQUIRED"):
            result = establish_authenticated_session(page, attempts=3)
        self.assertFalse(result)
        self.assertEqual((page.navigations, page.reloads), (1, 1))

    def test_auth_startup_reloads_before_failure_without_approving(self):
        class FakeResponse:
            status = 200
        class FakePage:
            navigations = 0
            reloads = 0
            def goto(self, *args, **kwargs):
                self.navigations += 1
                return FakeResponse()
            def reload(self, *args, **kwargs):
                self.reloads += 1
                return FakeResponse()
            def wait_for_timeout(self, milliseconds):
                pass
        page = FakePage()
        with patch("approve_all.wait_for_authenticated_ui",
                   side_effect=["INCONCLUSIVE", "AUTHENTICATED_UI_PRESENT"]):
            result = establish_authenticated_session(page, attempts=3)
        self.assertTrue(result)
        self.assertEqual((page.navigations, page.reloads), (1, 1))

    def test_auth_fail_closed_after_bounded_retries(self):
        class FakeResponse:
            status = 200
        class FakePage:
            reloads = 0
            def goto(self, *args, **kwargs):
                return FakeResponse()
            def reload(self, *args, **kwargs):
                self.reloads += 1
                return FakeResponse()
            def wait_for_timeout(self, milliseconds):
                pass
        page = FakePage()
        with patch("approve_all.wait_for_authenticated_ui", return_value="INCONCLUSIVE"):
            result = establish_authenticated_session(page, attempts=3)
        self.assertFalse(result)
        self.assertEqual(page.reloads, 2)

    def test_configuration_requires_explicit_bool(self):
        self.assertIs(validate_config(CONFIG), CONFIG)
        for config in ({**CONFIG, "approve_all": "true"},
                       {**CONFIG, "max_approvals_per_run": 1000},
                       {**CONFIG, "scan_seconds": 400}):
            with self.subTest(config=config):
                with self.assertRaises(ValueError):
                    validate_config(config)

    def test_mixed_external_sharing_reading_and_uploads_approved(self):
        cards = """
          <article data-testid="hatch-inline-approval-card"><p>공개 뉴스 읽기</p>
            <button id="read" onclick="this.dataset.clicked='yes'">허용</button><button>거부</button>
          </article>
          <article data-testid="hatch-inline-approval-card"><p>외부 사이트에 정보 공유</p>
            <button id="share" onclick="this.dataset.clicked='yes'">허용</button><button>거부</button>
          </article>
          <article data-testid="hatch-inline-approval-card"><p>빈 이미지 업로드</p>
            <button id="upload" onclick="this.dataset.clicked='yes'">한 번만 허용</button><button>거부</button>
          </article>"""
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(cards)
                seen = set()
                scanned, clicked, blocked = scan_once(page, "apply", seen, False, 50)
                self.assertEqual((scanned, clicked, blocked), (3, 3, 0))
                for name in ("read", "share", "upload"):
                    self.assertEqual(page.locator("#" + name).get_attribute("data-clicked"), "yes")
                self.assertEqual(scan_once(page, "apply", seen, False, 50), (0, 0, 0))
            finally:
                browser.close()

    def test_background_queue_batch_items(self):
        html = """
          <article data-testid="hatch-inline-approval-card">
            <div role="button" aria-expanded="true">
              <p>공유 요청 A</p>
              <button id="a" data-hatch-approval-primary-action="true"
                onclick="this.dataset.clicked='yes'">허용</button><button>거부</button>
            </div>
            <div role="button" aria-expanded="true">
              <p>공유 요청 B</p>
              <button id="b" data-hatch-approval-primary-action="true"
                onclick="this.dataset.clicked='yes'">허용</button><button>거부</button>
            </div>
          </article>"""
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(html)
                observed, clicked, blocked = scan_once(page, "apply", set(), True, 20)
                self.assertEqual((observed, clicked, blocked), (2, 2, 0))
                self.assertEqual(page.locator("#a").get_attribute("data-clicked"), "yes")
                self.assertEqual(page.locator("#b").get_attribute("data-clicked"), "yes")
            finally:
                browser.close()

    def test_prefer_persistent_permission_menu_then_fallback(self):
        html = """
          <article data-testid="hatch-inline-approval-card">
            <p>사이트 접속 요청</p><button id="once" onclick="this.dataset.clicked='yes'">허용</button>
            <button>거부</button>
            <button aria-haspopup="menu"
              onclick="document.querySelector('#always').style.display='block'">▼</button>
          </article>
          <button id="always" role="menuitem" style="display:none"
            onclick="this.dataset.clicked='yes'">이 사이트에 대해 계속 허용</button>
        """
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(html)
                _, clicked, _ = scan_once(page, "apply", set(), True, 3)
                self.assertEqual(clicked, 1)
                self.assertEqual(page.locator("#always").get_attribute("data-clicked"), "yes")
                self.assertIsNone(page.locator("#once").get_attribute("data-clicked"))
            finally:
                browser.close()

    def test_observe_mode_and_nonapproval_surfaces_never_clicked(self):
        html = """
          <button id="outside" onclick="this.dataset.clicked='yes'">허용</button>
          <article data-testid="hatch-inline-approval-card">
            <button id="inside" onclick="this.dataset.clicked='yes'">허용</button>
            <button>거부</button>
          </article>"""
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(html)
                observed, clicked, blocked = scan_once(page, "observe", set(), True, 10)
                self.assertEqual((observed, clicked, blocked), (1, 0, 0))
                self.assertIsNone(page.locator("#inside").get_attribute("data-clicked"))
                self.assertIsNone(page.locator("#outside").get_attribute("data-clicked"))
            finally:
                browser.close()

    def test_reindexing_cards_are_all_processed(self):
        """Click-removal of card 1 must not shift card 2 out of the queue."""
        html = "".join(
            f'''<article data-testid="hatch-inline-approval-card" data-hatch-approval-id="{i}">
                  <p>신규 승인 {i}</p>
                  <button id="yes{i}" onclick="this.closest('article').remove()">허용</button>
                  <button>거부</button>
                </article>'''
            for i in range(1, 4)
        )
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(html)
                scanned, clicked, blocked = scan_once(
                    page, "apply", set(), False, 10,
                    confirmation_timeout_ms=150,
                )
                self.assertEqual((scanned, clicked, blocked), (3, 3, 0))
                self.assertEqual(
                    page.locator('[data-testid="hatch-inline-approval-card"]').count(), 0
                )
            finally:
                browser.close()

    def test_unconfirmed_same_card_retries_at_most_three_times(self):
        html = """
          <article data-testid="hatch-inline-approval-card" data-hatch-approval-id="stable-1">
            <p>승인 처리가 늦은 요청</p>
            <button id="retry" onclick="
              this.dataset.count=String(Number(this.dataset.count||0)+1);
              if(Number(this.dataset.count)>=3)this.closest('article').remove()
            ">허용</button>
            <button>거부</button>
          </article>
        """
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(html)
                seen, retries = set(), {}
                totals = []
                for _ in range(3):
                    totals.append(scan_once(
                        page, "apply", seen, False, 10,
                        retry_state=retries, max_attempts=3,
                        retry_delay_seconds=0,
                        confirmation_timeout_ms=120,
                    ))
                self.assertEqual([i[1] for i in totals], [1, 1, 1])
                self.assertEqual(
                    page.locator('[data-testid="hatch-inline-approval-card"]').count(), 0
                )
                self.assertIn("approval:stable-1", seen)
                self.assertEqual(
                    scan_once(page, "apply", seen, False, 10,
                              retry_state=retries, max_attempts=3,
                              retry_delay_seconds=0,
                              confirmation_timeout_ms=120)[1], 0
                )
            finally:
                browser.close()

    def test_unconfirmed_card_stops_after_three_attempts(self):
        html = """
          <article data-testid="hatch-inline-approval-card" data-hatch-approval-id="stable-2">
            <button id="retry" onclick="this.dataset.count=String(Number(this.dataset.count||0)+1)">허용</button>
            <button>거부</button>
          </article>
        """
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(html)
                seen, retries = set(), {}
                counts = []
                for _ in range(4):
                    counts.append(scan_once(
                        page, "apply", seen, False, 10,
                        retry_state=retries, max_attempts=3,
                        retry_delay_seconds=0,
                        confirmation_timeout_ms=120,
                    )[1])
                self.assertEqual(counts, [1, 1, 1, 0])
                self.assertEqual(page.locator("#retry").get_attribute("data-count"), "3")
                self.assertIn("approval:stable-2", seen)
            finally:
                browser.close()

    def test_request_changed_after_click_is_not_retried(self):
        html = """
          <article data-testid="hatch-inline-approval-card" data-hatch-approval-id="stable-3">
            <p id="scope">이미 처리 중인 요청</p>
            <button id="retry" onclick="
              this.dataset.count=String(Number(this.dataset.count||0)+1);
              document.getElementById('scope').innerText='권한 범위 변경됨'
            ">허용</button>
            <button>거부</button>
          </article>
        """
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(html)
                seen, retries = set(), {}
                first = scan_once(
                    page, "apply", seen, False, 10,
                    retry_state=retries, max_attempts=3,
                    retry_delay_seconds=0, confirmation_timeout_ms=120,
                )
                second = scan_once(
                    page, "apply", seen, False, 10,
                    retry_state=retries, max_attempts=3,
                    retry_delay_seconds=0, confirmation_timeout_ms=120,
                )
                self.assertEqual((first[1], second[1]), (1, 0))
                self.assertEqual(page.locator("#retry").get_attribute("data-count"), "1")
                self.assertIn("approval:stable-3", seen)
            finally:
                browser.close()

    def test_retry_waits_for_cooldown_instead_of_hammering(self):
        html = """<article data-testid="hatch-inline-approval-card" data-hatch-approval-id="cooldown">
            <button id="retry" onclick="this.dataset.count=String(Number(this.dataset.count||0)+1)">허용</button>
            <button>거부</button></article>"""
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(html)
                seen, retries = set(), {}
                one = scan_once(
                    page, "apply", seen, False, 5,
                    retry_state=retries, max_attempts=3,
                    retry_delay_seconds=30, confirmation_timeout_ms=120,
                )
                two = scan_once(
                    page, "apply", seen, False, 5,
                    retry_state=retries, max_attempts=3,
                    retry_delay_seconds=30, confirmation_timeout_ms=120,
                )
                self.assertEqual((one[1], two[1]), (1, 0))
                self.assertEqual(page.locator("#retry").get_attribute("data-count"), "1")
            finally:
                browser.close()

    def test_disabled_button_is_never_retried(self):
        html = """<article data-testid="hatch-inline-approval-card" data-hatch-approval-id="disabled">
            <button id="retry" onclick="
                this.dataset.count=String(Number(this.dataset.count||0)+1);
                this.disabled=true
            ">허용</button>
            <button>거부</button></article>"""
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(html)
                seen, retries = set(), {}
                one = scan_once(
                    page, "apply", seen, False, 5,
                    retry_state=retries, max_attempts=3,
                    retry_delay_seconds=0, confirmation_timeout_ms=120,
                )
                two = scan_once(
                    page, "apply", seen, False, 5,
                    retry_state=retries, max_attempts=3,
                    retry_delay_seconds=0, confirmation_timeout_ms=120,
                )
                self.assertEqual((one[1], two[1]), (1, 0))
                self.assertEqual(page.locator("#retry").get_attribute("data-count"), "1")
            finally:
                browser.close()

    def test_nine_like_review_rows_are_traversed_one_at_a_time(self):
        # Exactly one panel approval is visible at a time. Rows can have
        # identical text, just as the live UI diagnostics suggested.
        html = """
          <div data-testid="hatch-status-panel-sliding-surface">
            <button class="review" aria-expanded="false" onclick="show(0)">검토 필요</button>
            <button class="review" aria-expanded="false" onclick="show(1)">검토 필요</button>
            <button class="review" aria-expanded="false" onclick="show(2)">검토 필요</button>
            <article data-testid="hatch-inline-approval-card"
              data-hatch-approval-surface="panel" data-row="0" class="card" style="display:none">
              <p>동일한 승인 문구</p>
              <button class="ok" onclick="this.closest('article').remove()">허용</button>
              <button>거부</button>
            </article>
            <article data-testid="hatch-inline-approval-card"
              data-hatch-approval-surface="panel" data-row="1" class="card" style="display:none">
              <p>동일한 승인 문구</p>
              <button class="ok" onclick="this.closest('article').remove()">허용</button>
              <button>거부</button>
            </article>
            <article data-testid="hatch-inline-approval-card"
              data-hatch-approval-surface="panel" data-row="2" class="card" style="display:none">
              <p>동일한 승인 문구</p>
              <button class="ok" onclick="this.closest('article').remove()">허용</button>
              <button>거부</button>
            </article>
          </div>
          <script>
            function show(n) {
              const buttons = [...document.querySelectorAll('.review')];
              const cards = [...document.querySelectorAll('.card')];
              buttons.forEach((b,i) => b.setAttribute('aria-expanded',i===n?'true':'false'));
              cards.forEach(card => card.style.display = Number(card.dataset.row)===n?'block':'none');
            }
          </script>"""
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(html)
                seen, retries = set(), {}
                self.assertEqual(review_rows(page).count(), 3)
                for i in range(3):
                    _, opened = open_review_row(page, i)
                    self.assertTrue(opened)
                    _, clicked, _ = scan_once(
                        page, "apply", seen, False, 10,
                        retry_state=retries, max_attempts=3,
                        retry_delay_seconds=0,
                        confirmation_timeout_ms=140,
                        panel_only=True,
                        identity_prefix=f"review-row:{i}:",
                    )
                    self.assertEqual(clicked, 1)
                self.assertEqual(page.locator(".ok").count(), 0)
            finally:
                browser.close()

    def test_missing_deny_button_not_clicked(self):
        html = """<article data-testid="hatch-inline-approval-card">
          <button id="yes" onclick="this.dataset.clicked='yes'">허용</button>
        </article>"""
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(html)
                _, clicked, blocked = scan_once(page, "apply", set(), True, 10)
                self.assertEqual((clicked, blocked), (0, 1))
                self.assertIsNone(page.locator("#yes").get_attribute("data-clicked"))
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
