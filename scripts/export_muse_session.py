"""Interactive local Muse sign-in and GitHub Actions secret export.

An authenticated Playwright storage state is sensitive. Only copy it into
GitHub Actions repository Secrets. Never paste it into issues, chats or logs.
"""
import base64
import gzip
import hashlib
import json
import os
import subprocess
import sys
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

SINGLE_SECRET = "MUSE_AUTH_B64"
PART_PREFIX = "MUSE_AUTH_B64_"
PART_SIZE = 39000
MAX_PARTS = 12


def encode_state(state):
    raw = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(gzip.compress(raw, compresslevel=9)).decode("ascii")


def make_secrets(encoded):
    if len(encoded) <= 46000:
        return [(SINGLE_SECRET, encoded)]
    parts = [encoded[i:i + PART_SIZE] for i in range(0, len(encoded), PART_SIZE)]
    if len(parts) > MAX_PARTS:
        raise ValueError("Muse 로그인 세션이 너무 큽니다. 브라우저 데이터를 정리하고 다시 시도하세요.")
    digest = hashlib.sha256(encoded.encode("ascii")).hexdigest()[:16]
    parts[0] = f"muse1:{len(parts)}:{digest}:" + parts[0]
    return [(f"{PART_PREFIX}{i:02d}", part) for i, part in enumerate(parts, 1)]


def copy_and_prompt(name, value, position, count):
    subprocess.run(["clip.exe"], input=value.encode("ascii"),
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"\n[{position}/{count}] GitHub Actions > Secrets > New repository secret")
    print(f"Name: {name}")
    print("Secret 칸: Ctrl+V → Add secret")
    input("GitHub에 저장한 다음 PowerShell에서 Enter를 눌러 계속하세요: ")


def main():
    if os.name != "nt":
        print("이 도구는 Windows PowerShell 클립보드 사용을 지원합니다.", file=sys.stderr)
        return 2
    print("Muse 전용 Chromium 브라우저에서 직접 로그인하세요.")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        try:
            context = browser.new_context(locale="ko-KR")
            page = context.new_page()
            page.goto("https://muse.ai/", wait_until="domcontentloaded", timeout=60_000)
            input("Muse에 로그인하고 채팅 화면이 보이면 Enter를 누르세요: ")
            if urlsplit(page.url).hostname != "muse.ai":
                print("Muse 페이지에서 로그인 여부를 확인하세요.", file=sys.stderr)
                return 2
            state = context.storage_state(indexed_db=True)
        finally:
            browser.close()

    if not state.get("cookies") and not state.get("origins"):
        print("세션 데이터가 비어 있습니다. 로그인 여부를 확인하세요.", file=sys.stderr)
        return 2
    try:
        secrets = make_secrets(encode_state(state))
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2

    print(f"\n세션을 {len(secrets)}개 GitHub Secret으로 나눠 등록합니다.")
    print("각 Secret의 실제 값은 PowerShell 화면에 출력하지 않습니다.")
    print("예전에 MUSE_AUTH_B64를 이미 등록했다면 분할 등록 시 삭제하세요.")
    try:
        for index, (name, value) in enumerate(secrets, 1):
            copy_and_prompt(name, value, index, len(secrets))
        print("\n완료! GitHub에서 'Muse login smoke test' 워크플로를 실행하세요.")
        return 0
    finally:
        # Discard the sensitive last value from clipboard.
        subprocess.run(["clip.exe"], input=b"", check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    raise SystemExit(main())
