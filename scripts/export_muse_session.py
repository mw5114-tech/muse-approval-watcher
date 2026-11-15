"""Interactive local Muse sign-in. Copies compressed Playwright storage state for GitHub Secrets.
Run only on your own computer. Never paste the result in a chat.
"""
import base64
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

SECRET_NAME = "MUSE_AUTH_B64"
MAX_SECRET_BYTES = 47_000

def encode_state(state):
    raw = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(gzip.compress(raw, compresslevel=9)).decode("ascii")

def main():
    print("Muse 전용 새 브라우저가 열립니다. 본인 PC에서 직접 로그인하세요.")
    print("로그인 정보는 열린 브라우저에서만 입력합니다.")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(locale="ko-KR")
        page = context.new_page()
        page.goto("https://muse.ai/", wait_until="domcontentloaded", timeout=60_000)
        input("Muse 로그인 완료 후 채팅 화면이 보이면 Enter를 누르세요: ")
        if urlsplit(page.url).hostname != "muse.ai":
            print("Muse 사이트가 아닙니다. 다시 로그인하세요.", file=sys.stderr)
            browser.close()
            return 2
        state = context.storage_state(indexed_db=True)
        browser.close()
    if not state.get("cookies") and not state.get("origins"):
        print("저장할 로그인 데이터가 없습니다.", file=sys.stderr)
        return 2
    encoded = encode_state(state)
    if len(encoded.encode("ascii")) > MAX_SECRET_BYTES:
        print("세션이 GitHub Secrets 48KB 제한에 가까워 저장할 수 없습니다.", file=sys.stderr)
        return 2
    if os.name == "nt":
        try:
            subprocess.run(["clip.exe"], input=encoded.encode("ascii"),
                           check=True, stdout=subprocess.DEVNULL)
            print(f"성공: {SECRET_NAME} 값이 클립보드에 복사됐습니다.")
            print("GitHub Secrets 입력란에 Ctrl+V 후 Add secret을 누르세요.")
            print("완료 후 클립보드를 다른 텍스트로 덮어쓰세요.")
            return 0
        except (FileNotFoundError, subprocess.CalledProcessError):
            pass
    path = Path.home() / "muse-auth-secret.txt"
    if path.exists():
        print(f"{path} 파일이 이미 존재해 덮어쓰지 않았습니다.", file=sys.stderr)
        return 2
    with path.open("x", encoding="ascii") as file:
        file.write(encoded)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    print(f"GitHub Secret 값이 {path}에 저장됐습니다. 등록 후 삭제하세요.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
