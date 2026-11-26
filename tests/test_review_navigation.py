"""Synthetic review navigation tests: never contact Muse."""
import sys
import unittest
from pathlib import Path
from playwright.sync_api import sync_playwright
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from approve_all import open_review_panel, review_state

class ReviewNavigationTests(unittest.TestCase):
    def setUp(self):
        self.p = sync_playwright().start()
        self.browser = self.p.chromium.launch(headless=True)
        self.page = self.browser.new_page()
    def tearDown(self):
        self.browser.close()
        self.p.stop()
    def test_composer_review_opens_panel(self):
        self.page.set_content("""<div>검토가 필요한 작업 5개가 있습니다 <button onclick="document.querySelector('aside').hidden=false">검토</button></div>
        <aside hidden data-testid="hatch-status-panel-sliding-surface">
          <article data-testid="hatch-inline-approval-card" data-hatch-approval-surface="panel"><button onclick="window.granted=true">한 번만 허용</button><button>거부</button></article>
        </aside>""")
        self.assertTrue(open_review_panel(self.page))
        self.assertEqual(review_state(self.page), "PENDING")
        self.assertIsNone(self.page.evaluate("window.granted"))
    def test_shield_tab_opens_review(self):
        self.page.set_content("""<aside data-testid="hatch-status-panel-sliding-surface">
          <button aria-pressed="true" aria-label="작업 목록">목록</button>
          <button aria-pressed="false" aria-label="승인" onclick="this.setAttribute('aria-pressed','true');document.querySelector('article').hidden=false">방패</button>
          <article hidden data-testid="hatch-inline-approval-card" data-hatch-approval-surface="panel"><button>한 번만 허용</button><button>거부</button></article>
        </aside>""")
        self.assertTrue(open_review_panel(self.page))
        self.assertEqual(review_state(self.page), "PENDING")
    def test_selected_tab_with_no_cards_is_not_verified_empty(self):
        self.page.set_content('<aside data-testid="hatch-status-panel-sliding-surface"><button aria-pressed="true" aria-label="승인">방패</button></aside>')
        self.assertEqual(review_state(self.page), "UNVERIFIED")
    def test_explicit_empty(self):
        self.page.set_content('<aside data-testid="hatch-status-panel-sliding-surface"><p>검토할 항목이 없습니다</p></aside>')
        self.assertEqual(review_state(self.page), "EMPTY_CONFIRMED")
    def test_banner_and_missing_cards(self):
        self.page.set_content('<div>검토가 필요한 작업 5개가 있습니다</div><aside data-testid="hatch-status-panel-sliding-surface"></aside>')
        self.assertEqual(review_state(self.page), "PENDING_NOT_RENDERED")
if __name__ == "__main__":
    unittest.main()
