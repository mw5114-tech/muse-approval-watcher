import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Error, TimeoutError, sync_playwright


LOG = logging.getLogger("muse-watcher")
FIELDS = ("site", "chat", "action", "scope")
OBSERVE = """(() => {
    const observer = new MutationObserver(() => { window.__museDirty = true; });
    observer.observe(document, {subtree: true, childList: true,
        attributes: true, characterData: true});
    window.__museDirty = true;
})()"""
DIRTY = """() => {
    if (!window.__museDirty) return false;
    window.__museDirty = false;
    return true;
}"""


def policy(request, rules):
    if request.get("action") != "read_public_news":
        return "held"
    if request.get("scope") != "public_read":
        return "held"
    if not all(isinstance(request.get(field), str) and request[field] for field in FIELDS):
        return "held"
    for rule in rules:
        if all(request[field] == rule.get(field) for field in FIELDS):
            return "review_allowlisted"
    return "held"


def scan(page, config, seen):
    if urlsplit(page.url).netloc != "muse.ai":
        return
    cards = page.locator(config["card_selector"])
    active = set()
    for card in cards.all():
        if not card.is_visible():
            continue
        marker = card.get_by_text(config["pending_marker"], exact=True)
        if marker.count() != 1 or not marker.is_visible():
            continue
        request_id = card.get_attribute(config["request_id_attribute"], timeout=1000)
        if not request_id:
            continue
        fingerprint = hashlib.sha256(request_id.encode()).hexdigest()[:16]
        active.add(fingerprint)
        if fingerprint not in seen:
            request = {}
            for name, selector in config["fields"].items():
                field = card.locator(selector)
                if field.count() == 1 and field.is_visible():
                    request[name] = field.inner_text(timeout=1000).strip()
            LOG.info("pending request=%s disposition=%s", fingerprint,
                     policy(request, config["rules"]))
    seen.intersection_update(active)
    seen.update(active)


def configuration(path):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    url = urlsplit(config["url"])
    if url.scheme != "https" or url.netloc != "muse.ai" or url.query or url.fragment:
        raise ValueError("URL must be an HTTPS Muse page without query or fragment")
    selectors = config.get("fields", {})
    if set(selectors) != set(FIELDS):
        raise ValueError("Configure all four request fields")
    for value in (config["card_selector"], config["pending_marker"],
                  config["request_id_attribute"], *selectors.values()):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Set the selectors from a verified live pending card")
    if not isinstance(config["rules"], list) or not all(
        isinstance(rule, dict) and set(rule) == set(FIELDS) for rule in config["rules"]
    ):
        raise ValueError("Rules must provide exact site, chat, action and scope")
    return config


def authentication_required(page):
    return urlsplit(page.url).netloc != "muse.ai" or any(
        page.get_by_role("heading", name=name, exact=True).count()
        for name in ("로그인 또는 계정 만들기", "Log in or sign up")
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--login", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    os.umask(0o077)
    profile = Path(os.environ.get("MUSE_PROFILE", "profile")).resolve()
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    config = None if args.login else configuration(os.environ.get("MUSE_CONFIG", "config.json"))
    with sync_playwright() as playwright:
        with playwright.chromium.launch_persistent_context(
            str(profile), headless=not args.login, chromium_sandbox=True,
        ) as browser:
            page = browser.pages[0] if browser.pages else browser.new_page()
            if args.login:
                page.goto("https://muse.ai/", wait_until="domcontentloaded")
                input("Complete sign-in in Chromium, then press Enter here. ")
                return
            browser.add_init_script(OBSERVE)
            page.goto(config["url"], wait_until="domcontentloaded")
            seen = set()
            while True:
                try:
                    page.wait_for_function(DIRTY, timeout=15000, polling=750)
                except TimeoutError:
                    pass
                if authentication_required(page):
                    LOG.error("Interactive sign-in required")
                    return 20
                scan(page, config, seen)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, KeyError, OSError) as error:
        LOG.error("Configuration or filesystem error (%s)", type(error).__name__)
        raise SystemExit(78)
    except Error as error:
        LOG.error("Chromium or page failure (%s)", type(error).__name__)
        raise SystemExit(1)
