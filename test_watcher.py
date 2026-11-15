import tempfile
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright

from watcher import DIRTY, OBSERVE, authentication_required, configuration, policy, scan


RULE = dict(site="news.example", chat="research", action="read_public_news", scope="public_read")
CONFIG = dict(card_selector="article", request_id_attribute="data-id",
              pending_marker="Pending", rules=[RULE],
              fields={key: f".{key}" for key in RULE})


def card(request_id, **changes):
    fields = "".join(f'<span class="{key}">{value}</span>' for key, value in (RULE | changes).items())
    return f'<article data-id="{request_id}"><b>Pending</b>{fields}</article>'


class WatcherTests(unittest.TestCase):
    def test_policy_refuses_missing_or_sensitive_scope(self):
        self.assertEqual(policy(RULE, [RULE]), "review_allowlisted")
        self.assertEqual(policy(RULE | dict(action="send_mail"), [RULE]), "held")
        self.assertEqual(policy(RULE | dict(scope="public_read,write"), [RULE]), "held")
        self.assertEqual(policy(RULE | dict(chat="other"), [RULE]), "held")
        self.assertEqual(policy(RULE | dict(site=""), [RULE]), "held")

    def test_example_requires_verified_selectors(self):
        with self.assertRaises(ValueError):
            configuration(Path(__file__).with_name("config.example.json"))

    def test_chromium_pending_queue_and_browser_relaunch(self):
        with tempfile.TemporaryDirectory() as profile, sync_playwright() as playwright:
            for attempt in range(2):
                with playwright.chromium.launch_persistent_context(profile) as browser:
                    page = browser.pages[0]
                    page.route("**/*", lambda route: route.fulfill(
                        status=200, content_type="text/html", body=card("history").replace("Pending", "Approved")
                    ))
                    browser.add_init_script(OBSERVE)
                    page.goto("https://muse.ai/")
                    self.assertTrue(page.evaluate(DIRTY))
                    self.assertFalse(page.evaluate(DIRTY))
                    page.locator("body").evaluate("(el, html) => el.innerHTML += html",
                                                  card("1") + card("2", site="outside.example"))
                    page.wait_for_function(DIRTY)
                    seen = set()
                    with self.assertLogs("muse-watcher", level="INFO") as logged:
                        scan(page, CONFIG, seen)
                    self.assertEqual(len(logged.output), 2)
                    self.assertTrue(logged.output[0].endswith("review_allowlisted"))
                    self.assertTrue(logged.output[1].endswith("held"))
                    with self.assertNoLogs("muse-watcher", level="INFO"):
                        scan(page, CONFIG, seen)
                    page.set_content("<h1>로그인 또는 계정 만들기</h1>")
                    self.assertTrue(authentication_required(page))


if __name__ == "__main__":
    unittest.main()
