"""Secure local Muse sign-in and GitHub Actions Secret exporter.

Only ever paste each value into GitHub repository Secrets. A Muse auth session
is a credential. No secret data is printed to the terminal or committed.
"""
import base64
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright
from check_muse_session import auth_status

SINGLE_SECRET = "MUSE_AUTH_B64"
PART_PREFIX = "MUSE_AUTH_B64_"
PART_SIZE = 39000
MAX_PARTS = 12
AUTH_RE = re.compile(r"auth|firebase|session|token|clerk|identity|oidc|supabase|msal|persist|login|user", re.I)


def encode_state(state):
    raw = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(gzip.compress(raw, compresslevel=9)).decode("ascii")


def make_secrets(encoded):
    if len(encoded) <= 46000:
        return [(SINGLE_SECRET, encoded)]
    parts = [encoded[i:i + PART_SIZE] for i in range(0, len(encoded), PART_SIZE)]
    if len(parts) > MAX_PARTS:
        raise ValueError("Muse 세션 데이터가 압축 후에도 저장 한도를 초과합니다.")
    digest = hashlib.sha256(encoded.encode("ascii")).hexdigest()[:16]
    parts[0] = f"muse1:{len(parts)}:{digest}:" + parts[0]
    return [(f"{PART_PREFIX}{i:02d}", part) for i, part in enumerate(parts, 1)]


def is_muse_host(host):
    return host == "muse.ai" or host.endswith(".muse.ai")


def compact_state(state, include_idb="auth", local_storage="all"):
    """Keep only Muse-origin session material; avoid large unrelated app caches."""
    cookies = [
        cookie for cookie in state.get("cookies", [])
        if is_muse_host(str(cookie.get("domain", "")).lstrip(".").lower())
    ]
    origins = []
    for origin in state.get("origins", []):
        if not is_muse_host((urlsplit(origin.get("origin", "")).hostname or "").lower()):
            continue
        items = origin.get("localStorage", [])
        if local_storage == "auth":
            items = [item for item in items if AUTH_RE.search(item.get("name", ""))]
        if local_storage == "none":
            items = []
        entry = {"origin": origin["origin"], "localStorage": items}
        if include_idb != "none":
            databases = origin.get("indexedDB", [])
            if include_idb == "auth":
                databases = [db for db in databases if AUTH_RE.search(db.get("name", ""))]
            if databases:
                entry["indexedDB"] = databases
        origins.append(entry)
    return {"cookies": cookies, "origins": origins}


def select_export(state):
    candidates = (
        ("Muse 사이트 데이터 전체", dict(include_idb="all", local_storage="all")),
        ("인증 관련 IndexedDB + 로컬 저장소", dict(include_idb="auth", local_storage="all")),
        ("Muse 쿠키 + 로컬 저장소", dict(include_idb="none", local_storage="all")),
        ("Muse 쿠키 + 인증 관련 로컬 저장소", dict(include_idb="none", local_storage="auth")),
        ("Muse 쿠키만", dict(include_idb="none", local_storage="none")),
    )
    feasible = []
    for priority, (label, opts) in enumerate(candidates):
        compact = compact_state(state, **opts)
        if not compact["cookies"] and not any(o["localStorage"] or o.get("indexedDB") for o in compact["origins"]):
            continue
        encoded = encode_state(compact)
        if len(encoded) <= MAX_PARTS * PART_SIZE:
            secrets = make_secrets(encoded)
            feasible.append((len(secrets), priority, label, secrets))
    if not feasible:
        raise ValueError("Muse 인증 데이터가 최적화 후에도 너무 큽니다. 로그인 보존 방식을 다시 확인해야 합니다.")
    # Prefer a single Secret where possible; use richer state if equally small.
    _, _, label, secrets = min(feasible, key=lambda item: (item[0], item[1]))
    return label, secrets


def copy_and_prompt(name, value, position, count):
    subprocess.run(["clip.exe"], input=value.encode("ascii"),
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"\n[{position}/{count}] GitHub → Settings → Secrets and variables → Actions")
    print(f"Secret name: {name}")
    print("Secret 칸에 Ctrl+V → Add secret")
    input("GitHub에 저장 완료한 뒤 PowerShell에서 Enter를 누르세요: ")


def main():
    if os.name != "nt":
        print("Windows PowerShell에서 실행하세요.", file=sys.stderr)
        return 2
    # The dedicated profile stays under the Windows user directory, NOT GitHub.
    profile = Path(os.environ.get("LOCALAPPDATA") or str(Path.home())) / "muse-watcher-login-profile"
    profile.mkdir(parents=True, exist_ok=True)
    print("Muse 전용 Chromium 브라우저를 열어 로그인합니다.")
    print("이후 다시 실행해도 별도 로컬 프로필에서 로그인 세션을 재사용합니다.")
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(str(profile), headless=False, locale="ko-KR")
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto("https://muse.ai/", wait_until="domcontentloaded", timeout=60_000)
            input("Muse 로그인 후 채팅 화면이 보이면 PowerShell에서 Enter를 누르세요: ")
            if (urlsplit(page.url).hostname or "").lower() != "muse.ai":
                print("Muse 페이지에서 로그인을 완료해야 합니다.", file=sys.stderr)
                return 2
            # A homepage URL alone does NOT prove an authenticated session.
            # Prevent accidentally replacing a working Actions secret with
            # a signed-out, checkpoint or partially loaded browser snapshot.
            if auth_status(page) != "AUTHENTICATED_UI_PRESENT":
                print("Muse 로그인된 채팅 화면이 확인되지 않아 저장을 중단합니다.", file=sys.stderr)
                return 2
            state = context.storage_state(indexed_db=True)
        finally:
            context.close()

    try:
        label, secrets = select_export(state)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2

    print(f"\n추출 방식: {label}")
    print(f"등록할 GitHub Secret: {len(secrets)}개")
    print("Secrets 등록 후에만 GitHub Actions 로그인 성공 여부를 확인할 수 있습니다.")
    print("다른 방식으로 이전에 등록한 MUSE_AUTH_B64 또는 MUSE_AUTH_B64_XX는 삭제하세요.")
    try:
        for index, (name, value) in enumerate(secrets, 1):
            copy_and_prompt(name, value, index, len(secrets))
        print("\n완료. GitHub의 'Muse login smoke test'를 수동 실행하세요.")
        return 0
    finally:
        subprocess.run(["clip.exe"], input=b"", check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    raise SystemExit(main())
