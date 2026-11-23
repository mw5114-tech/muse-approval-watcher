"""Offline tests of guarded Muse remembered-account login; no live Muse access."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from muse_one_click_reauth import (
    allowed_host, confirmed_account_chooser, try_one_click_reauth,
)


def visit(page, html, url="https://muse.ai/"):
    page.route(url, lambda route: route.fulfill(
        status=200, content_type="text/html", body=html,
    ))
    page.goto(url)


def chooser(*, email="member@example.com", extra="", login_count=1):
    buttons = "".join(
        f"""<button class="login" onclick="
            window.clicked=Number(window.clicked||0)+1;
            document.body.innerHTML='<div data-hatch-avatar-interaction=true></div>';
        ">로그인</button>""" for _ in range(login_count)
    )
    return f"""<html><body>
        <h1>계속하려면 계정을 선택하세요</h1>
        <p>Muse에 로그인</p>
        <article><span>Muse</span><span>{email}</span>{extra}{buttons}</article>
        <a href="#">다른 계정 사용</a>
    </body></html>"""


class OneClickLoginTests(unittest.TestCase):
    def test_allowed_hosts_are_https_and_exact_domain(self):
        self.assertTrue(allowed_host("https://muse.ai/"))
        self.assertTrue(allowed_host("https://auth.muse.ai/"))
        self.assertFalse(allowed_host("http://muse.ai/"))
        self.assertFalse(allowed_host("https://muse.ai.attacker.test/"))
        self.assertFalse(allowed_host("https://attacker-muse.ai/"))
        self.assertFalse(allowed_host("https://accounts.example.test/login"))
        self.assertTrue(allowed_host("https://accounts.example.test/login",
                                     "accounts.example.test"))

    def test_single_recognized_card_clicks_and_authenticates(self):
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                visit(page, chooser())
                with patch.dict("muse_one_click_reauth.os.environ", {
                    "MUSE_ONE_CLICK_LOGIN_ENABLED": "true",
                }):
                    self.assertEqual(
                        try_one_click_reauth(page, expected_email="member@example.com"),
                        "AUTHENTICATED",
                    )
                self.assertEqual(page.locator("[data-hatch-avatar-interaction]").count(), 1)
            finally:
                browser.close()

    def test_mismatched_account_never_clicked(self):
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                visit(page, chooser())
                self.assertIsNone(confirmed_account_chooser(
                    page.main_frame, expected_email="different@example.com",
                ))
                self.assertEqual(
                    try_one_click_reauth(page, expected_email="different@example.com"),
                    "CHOOSER_UNVERIFIED",
                )
                self.assertEqual(page.locator("button.login").count(), 1)
            finally:
                browser.close()

    def test_multiple_accounts_are_ambiguous_and_never_clicked(self):
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                visit(page, chooser(extra="<span>second@example.com</span>"))
                self.assertIsNone(confirmed_account_chooser(page.main_frame))
                self.assertEqual(try_one_click_reauth(page), "CHOOSER_UNVERIFIED")
                self.assertEqual(page.locator("button.login").count(), 1)
            finally:
                browser.close()

    def test_multiple_login_buttons_never_clicked(self):
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                visit(page, chooser(login_count=2))
                self.assertEqual(try_one_click_reauth(page), "CHOOSER_UNVERIFIED")
                self.assertEqual(page.locator("button.login").count(), 2)
            finally:
                browser.close()

    def test_password_page_never_clicked(self):
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                visit(page, chooser(extra='<input type="password"/>'))
                self.assertEqual(try_one_click_reauth(page), "CHOOSER_UNVERIFIED")
                self.assertEqual(page.locator('input[type="password"]').count(), 1)
            finally:
                browser.close()

    def test_untrusted_host_never_clicked(self):
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                visit(page, chooser(), url="https://imposter.test/")
                self.assertEqual(try_one_click_reauth(page), "NOT_PRESENT")
                self.assertEqual(page.locator("button.login").count(), 1)
            finally:
                browser.close()

    def test_disabled_mode_never_clicks(self):
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                visit(page, chooser())
                with patch.dict("muse_one_click_reauth.os.environ", {
                    "MUSE_ONE_CLICK_LOGIN_ENABLED": "false",
                }):
                    self.assertEqual(try_one_click_reauth(page), "DISABLED")
                self.assertEqual(page.locator("button.login").count(), 1)
            finally:
                browser.close()

    def test_login_entry_may_open_account_chooser(self):
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                final_html = chooser()
                # Build a safe landing page that reveals the chooser after
                # the explicit Muse login-entry button is clicked.
                visit(page, (
                    '<html><body>뮤즈 로그인'
                    '<button id="entry">로그인</button></body></html>'
                ))
                page.evaluate("""html => {
                    document.querySelector('#entry').addEventListener('click',
                        () => { document.body.innerHTML = html; });
                }""", final_html)
                self.assertEqual(try_one_click_reauth(page), "AUTHENTICATED")
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
