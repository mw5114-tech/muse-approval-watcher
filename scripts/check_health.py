"""Detect gaps in the Muse watcher and actively recover missed GitHub schedules.

Runs using only GitHub's short-lived GITHUB_TOKEN (actions: write). No Muse
session or other repository secrets are accessed. The main watcher workflow is
dispatched only when there is no recent healthy or running attempt.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


WORKFLOW = "muse-approval-watch.yml"
ACTIVE = {"queued", "pending", "waiting", "in_progress", "requested"}
TERMINAL = {"completed"}


def parse_iso(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def age_minutes(item, now):
    return (now - parse_iso(item["created_at"])).total_seconds() / 60


def assess(runs, now, recovery_age=12, warning_age=20, critical_age=90, stuck_age=25):
    """Pure decision logic for unit tests. Returns (severity, dispatch, reason).

    Recency is measured across schedule AND workflow_dispatch runs, because a
    recovery-triggered run is a real attempt at doing approval work.
    """
    candidates = sorted(
        [r for r in runs if r.get("event") in {"schedule", "workflow_dispatch"}
         and r.get("created_at")],
        key=lambda r: r["created_at"], reverse=True,
    )
    if not candidates:
        return "warning", True, "no_watcher_runs_found"
    latest = candidates[0]
    age = age_minutes(latest, now)
    if age < -5:
        return "error", False, "invalid_run_timestamp"
    status = latest.get("status")
    if status in ACTIVE:
        if age <= stuck_age:
            return "healthy", False, f"running_or_queued age_minutes={max(age, 0):.1f}"
        # A queued/running attempt has stalled; launch one recovery attempt.
        # The main workflow's own concurrency group prevents concurrent work.
        level = "critical" if age >= critical_age else "warning"
        return level, True, f"watcher_stuck age_minutes={age:.1f}"
    if status not in TERMINAL:
        return "error", False, "unknown_run_status"
    if latest.get("conclusion") != "success":
        if age < 2:
            return "warning", False, "failed_just_now_retry_cooldown"
        level = "critical" if age >= critical_age else "warning"
        return level, True, f"last_run_failed age_minutes={age:.1f}"
    if age < recovery_age:
        return "healthy", False, f"watcher_recent age_minutes={max(age, 0):.1f}"
    level = "critical" if age >= critical_age else (
        "warning" if age >= warning_age else "healthy"
    )
    return level, True, f"watcher_missing_schedule age_minutes={age:.1f}"


def github_request(url, token, method="GET", data=None):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "muse-approval-watchdog",
    }
    request = Request(url, headers=headers, method=method, data=data)
    for attempt in range(3):
        try:
            with urlopen(request, timeout=20) as response:
                body = response.read()
                return json.loads(body) if body else None
        except HTTPError as error:
            if error.code not in {429, 500, 502, 503, 504} or attempt == 2:
                raise
        except (URLError, TimeoutError):
            if attempt == 2:
                raise
        time.sleep(2 * (attempt + 1))


def append_summary(level, detail, recovery):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    safe_detail = "".join(c for c in detail if c.isalnum() or c in " _=.-")[:220]
    with open(path, "a", encoding="utf-8") as file:
        file.write(
            f"## Muse approval watchdog\n\n"
            f"- Status: **{level}**\n"
            f"- Detail: \`{safe_detail}\`\n"
            f"- Recovery: **{recovery}**\n\n"
            "GitHub cron execution may be delayed or skipped. A recovery dispatch "
            "starts another GitHub-hosted watcher; it cannot guarantee instant execution.\n"
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--recovery-age", type=int, default=12)
    parser.add_argument("--warning-age", type=int, default=20)
    parser.add_argument("--critical-age", type=int, default=90)
    args = parser.parse_args()
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    branch = os.environ.get("GITHUB_EVENT_REPOSITORY_DEFAULT_BRANCH", "main")
    if not token or not repo or "/" not in repo:
        print("MUSE_WATCHDOG=ERROR missing_github_context", flush=True)
        return 2
    base = f"https://api.github.com/repos/{repo}/actions/workflows/{WORKFLOW}"
    query_url = base + "/runs?per_page=50"
    try:
        payload = github_request(query_url, token)
        runs = (payload or {}).get("workflow_runs", [])
        level, dispatch, reason = assess(
            runs, datetime.now(timezone.utc),
            recovery_age=args.recovery_age,
            warning_age=args.warning_age,
            critical_age=args.critical_age,
        )
    except (HTTPError, URLError, ValueError, TimeoutError, KeyError) as error:
        print(f"MUSE_WATCHDOG=ERROR read_api={type(error).__name__}", flush=True)
        append_summary("error", "github_api_unavailable", "none")
        return 2

    recovery = "not_needed"
    if dispatch:
        data = json.dumps({
            "ref": branch,
            "inputs": {"mode": "apply"},
        }).encode("utf-8")
        try:
            github_request(
                base + "/dispatches", token, method="POST", data=data
            )
            recovery = "workflow_dispatch_accepted"
            print(f"MUSE_WATCHDOG=RECOVERY_STARTED reason={reason}", flush=True)
        except (HTTPError, URLError, ValueError, TimeoutError) as error:
            print(f"MUSE_WATCHDOG=ERROR dispatch_api={type(error).__name__}", flush=True)
            append_summary("error", reason, "dispatch_failed")
            return 2
    print(f"MUSE_WATCHDOG={level.upper()} reason={reason} recovery={recovery}", flush=True)
    append_summary(level, reason, recovery)

    # A dispatch was accepted, so a gap alone is not a failure; the next
    # watchdog run will re-check. Avoid false "all jobs failed" notifications
    # for transient GitHub scheduling delays.
    return 1 if level == "error" or (level == "critical" and not dispatch) else 0


if __name__ == "__main__":
    sys.exit(main())
