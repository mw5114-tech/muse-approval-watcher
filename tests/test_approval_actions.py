"""Synthetic, offline tests. No Muse account or external websites are contacted."""
import sys
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from approval_policy import decide, safe_policy
from actions_approval_watcher import process_cards


CONFIG = {
    "enabled": True,
    "allowed_read_hosts": ["yna.co.kr"],
    "prefer_always_for_site": True,
    "max_approvals_per_run": 10,
    "scan_seconds": 35,
}


class ApprovalPolicyTests(unittest.TestCase):
    def test_exact_public_news_read(self):
        result = decide("Muse이(가) https://www.yna.co.kr/ 웹사이트에 접속하도록 허용하시겠어요?", ["yna.co.kr"])
        self.assertEqual(result, ("allow_read", "yna.co.kr", "exact_allowlist_read"))

    def test_external_information_sharing_is_blocked(self):
        result = decide("Muse(이)가 dps.heraldcorp.com Cafe1 Watch에 정보를 공유하도록 허용하시겠어요?", ["heraldcorp.com", "dps.heraldcorp.com"])
        self.assertEqual(result[0], "hold")
        self.assertEqual(result[2], "sensitive_or_sharing")

    def test_bad_scopes_and_untrusted_sites_are_blocked(self):
        for text in (
            "www.yna.co.kr 사이트에 업로드하도록 허용?",
            "www.yna.co.kr 웹사이트에 접속하여 구매?",
            "www.yna.co.kr 웹사이트에 접속하여 이메일 발송?",
            "https://www.yna.co.kr 웹사이트에 접속하여 secret 전송?",
            "https://www.yna.co.kr + other.example.com 웹사이트에 접속?",
            "other.example.com 웹사이트에 접속하도록 허용?",
            "www.yna.co.kr 확인 요청",
        ):
            with self.subTest(text=text):
                self.assertEqual(decide(text, ["yna.co.kr"])[0], "hold")

    def test_config_validation(self):
        self.assertEqual(safe_policy(CONFIG), CONFIG)
        with self.assertRaises(ValueError):
            safe_policy({**CONFIG, "allowed_read_hosts": ["*.com"]})


class BrowserFixtureTests(unittest.TestCase):
    def test_sequential_read_only_buttons(self):
        html = """
        <article data-testid="hatch-inline-approval-card" data-hatch-approval-id="1">
          <div>Muse이(가) https://www.yna.co.kr/ 웹사이트에 접속하도록 허용하시겠어요?</div>
          <button id="first" onclick="this.dataset.clicked='yes'">한 번만 허용</button>
          <button>거부</button>
        </article>
        <article data-testid="hatch-inline-approval-card" data-hatch-approval-id="2">
          <div>Muse(이)가 dps.heraldcorp.com Cafe1 Watch에 정보를 공유하도록 허용하시겠어요?</div>
          <button id="second" onclick="this.dataset.clicked='yes'">한 번만 허용</button>
          <button>거부</button>
        </article>"""
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(html)
                attempted = set()
                observed, approved, held = process_cards(page, CONFIG, "apply", attempted)
                self.assertEqual((observed, approved, held), (2, 1, 1))
                self.assertEqual(page.locator("#first").get_attribute("data-clicked"), "yes")
                self.assertIsNone(page.locator("#second").get_attribute("data-clicked"))
                _, repeat_approved, _ = process_cards(page, CONFIG, "apply", attempted)
                self.assertEqual(repeat_approved, 0)
            finally:
                browser.close()

    def test_observe_mode_never_clicks(self):
        html = """
        <article data-testid="hatch-inline-approval-card">
        Muse이(가) https://www.yna.co.kr/ 웹사이트에 접속하도록 허용하시겠어요?
        <button id="btn" onclick="this.dataset.clicked='yes'">한 번만 허용</button>
        <button>거부</button></article>"""
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(html)
                _, approved, _ = process_cards(page, CONFIG, "observe", set())
                self.assertEqual(approved, 0)
                self.assertIsNone(page.locator("#btn").get_attribute("data-clicked"))
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
