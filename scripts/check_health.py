"""Check that the Muse approval schedule has genuinely been executing.

Read-only GitHub API check. No Muse login credentials are used or emitted.
Returns nonzero on missing, stale, or failed scheduled runs.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def parse_iso(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def check(runs, now, max_age_minutes=30):
    scheduled = [r for r in runs if r.get("event") == "schedule"]
    if not scheduled:
        return False, "no_scheduled_runs"
    latest = max(scheduled, key=lambda r: r.get("created_at", ""))
    age_min = (now - parse_iso(latest["created_at"])).total_seconds() / 60
    if age_min < -5:
        return False, "clock_or_github_timestamp_mismatch"
    if age_min > max_age_minutes:
        return False, f"latest_schedule_stale age_minutes={age_min:.1f}"
    status = latest.get("status", "unknown")
    conclusion = latest.get("conclusion")
    if status == "completed" and conclusion != "success":
        return False, f"last_schedule_failed conclusion={conclusion}"
    if status not in {"queued", "pending", "in_progress", "completed", "waiting"}:
        return False, f"unexpected_status status={status}"
    if status in {"queued", "pending", "in_progress", "waiting"} and age_min > 12:
        return False, f"stuck_run age_minutes={age_min:.1f} status={status}"
    return True, f"last_schedule_age_minutes={max(age_min, 0):.1f} status={status}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-age-minutes", type=int, default=30)
    args = parser.parse_args()
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token or not repo or "/" not in repo:
        print("MUSE_WATCHDOG=ERROR missing_github_context", flush=True)
        return 2
    url = (
        f"https://api.github.com/repos/{repo}/actions/workflows/"
        "muse-approval-watch.yml/runs?event=schedule&per_page=30"
    )
    request = Request(
        url, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "muse-approval-watcher",
        }
    )
    try:
        with urlopen(request, timeout=20) as response:
            payload = json.load(response)
    except (HTTPError, URLError, ValueError, TimeoutError) as error:
        print(f"MUSE_WATCHDOG=ERROR api={type(error).__name__}", flush=True)
        return 2
    runs = payload.get("workflow_runs", [])
    now = datetime.now(timezone.utc)
    okay, detail = check(runs, now, args.max_age_minutes)
    print(f"MUSE_WATCHDOG={'OK' if okay else 'FAIL'} {detail}", flush=True)
    return 0 if okay else 1


if __name__ == "__main__":
    sys.exit(main())
