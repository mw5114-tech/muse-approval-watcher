"""Conservative classifier for Muse approval cards.

This policy only authorizes explicitly listed hosts for recognizable
read-only website visits. No connector actions, external information sharing,
writes, purchases, uploads or ambiguous prompts are authorized.
"""
import re
from urllib.parse import urlsplit

HOST_RE = re.compile(r"(?<![@\w.-])(?:https?://)?([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}(?=[/:\s?!.),]|$)", re.I)
DOMAIN_RE = re.compile(r"(?<![@\w.-])(?:https?://)?((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,})(?=[/:\s?!.),]|$)", re.I)
READ_RE = re.compile(r"(?:웹사이트|웹페이지|사이트|website|webpage|site|https?://).{0,70}(?:방문|접속|접근|열기|읽기|조회|방문하려|connect|access|visit|open|read|browse)|(?:방문|접속|접근|열기|읽기|조회|visit|access|open|browse).{0,70}(?:웹사이트|웹페이지|사이트|website|webpage|site|https?://|[a-z0-9-]+\.[a-z]{2,})", re.I | re.S)
BLOCK_RE = re.compile(
    r"정보.{0,25}(?:공유|전송)|(?:share|send|transmit|transfer).{0,25}(?:information|data)|"
    r"(?:외부|external).{0,20}(?:전송|공유|share|send)|"
    r"업로드|upload|다운로드|download|연락처|주소록|contacts|"
    r"결제|구매|주문|checkout|purchase|payment|billing|"
    r"삭제|수정|게시|발행|발송|메시지 전송|메일 보내|"
    r"delete|modify|update|post|publish|email|send message|"
    r"oauth|credential|token|cookie|password|secret|"
    r"이메일 주소|email address|계좌|은행|bank account|"
    r"예약 작업에 대한 웹 접근 허용", re.I
)
ALLOWED_TEXT_RE = re.compile(r"^(?:한\s*번만\s*허용|이번만\s*허용|허용|Allow|Allow once|Allow this time)$", re.I)
ALWAYS_TEXT_RE = re.compile(r"^(?:이\s*사이트에\s*대해\s*(?:계속|항상)\s*허용|계속 허용|항상 허용|Always allow|Allow always|Always allow for this site)$", re.I)
REJECT_TEXT_RE = re.compile(r"^(?:거부|차단|허용 안 함|Deny|Decline|Reject|Block)$", re.I)


def normalized_host(value):
    value = str(value or "").strip().lower().lstrip(".")
    if value.startswith("https://"):
        value = urlsplit(value).hostname or ""
    return value.removeprefix("www.")


def decide(text, hosts):
    """Return (decision, exact_host, reason) without exposing source text."""
    if not isinstance(text, str) or len(text) > 2000 or not text.strip():
        return "hold", "", "unreadable"
    if BLOCK_RE.search(text):
        return "hold", "", "sensitive_or_sharing"
    found = {normalized_host(m.group(1)) for m in DOMAIN_RE.finditer(text)}
    found.discard("muse.ai")
    found.discard("meta.ai")
    if len(found) != 1:
        return "hold", "", "ambiguous_domain"
    host = next(iter(found))
    allowed = {normalized_host(h) for h in hosts}
    if host not in allowed:
        return "hold", host, "not_allowlisted"
    if not READ_RE.search(text):
        return "hold", host, "not_verified_web_read"
    return "allow_read", host, "exact_allowlist_read"


def safe_policy(config):
    hosts = config.get("allowed_read_hosts", [])
    if not isinstance(hosts, list) or not all(
        isinstance(h, str) and h and normalized_host(h) == h and
        re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", h) for h in hosts
    ):
        raise ValueError("allowed_read_hosts must contain normalized hosts")
    if len(hosts) != len(set(hosts)):
        raise ValueError("Duplicate allowlisted hosts")
    if config.get("max_approvals_per_run", 0) not in range(1, 21):
        raise ValueError("max_approvals_per_run must be 1..20")
    if config.get("scan_seconds", 0) not in range(5, 241):
        raise ValueError("scan_seconds must be 5..240")
    if not isinstance(config.get("enabled"), bool):
        raise ValueError("enabled must be a boolean")
    return config
