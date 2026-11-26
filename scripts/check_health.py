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
from urllib.parse import quote


WORKFLOW = "muse-approval-watch.yml"
ACTIVE = {"queued", "pending", "waiting", "in_progress", "requested"}
TERMINAL = {"completed"}


def parse_iso(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def age(item, now, field='created_at'):
    return max(0, (now-parse_iso(item[field])).total_seconds()/60)


def assess(runs, now, recovery_age=17, warning_age=25, critical_age=60, stuck_age=20, failure_cooldown=30):
    """Decide recovery using apply history; never dispatch into an active run."""
    candidates = sorted((r for r in runs if r.get('created_at') and (r.get('event')=='schedule' or (r.get('event') in {'workflow_dispatch','push'} and r.get('display_title')=='Muse watcher / apply'))), key=lambda r:r['created_at'], reverse=True)
    active = [r for r in runs if r.get('status') in ACTIVE and r.get('created_at')]
    if any((parse_iso(r['created_at'])-now).total_seconds()>300 for r in candidates+active):
        return 'error',False,'invalid_run_timestamp'
    if not candidates:
        if active: return 'warning',False,'active_without_apply_history'
        return 'warning',True,'no_apply_runs'
    latest = candidates[0]
    if latest.get('status') not in ACTIVE | {'completed'}:
        return 'error',False,'unknown_run_status'
    last_ok = next((r for r in candidates if r.get('status')=='completed' and r.get('conclusion')=='success'),None)
    outage = age(last_ok or candidates[-1],now)
    level = 'critical' if outage>=critical_age else 'warning' if outage>=warning_age else 'healthy'
    if active:
        stalled = max(age(r,now) for r in active)>=stuck_age
        return ('critical' if level=='critical' else 'warning') if stalled else level,False,'active_stuck' if stalled else 'active'
    if latest.get('conclusion')=='success':
        return level,age(latest,now)>=recovery_age,'schedule_gap' if age(latest,now)>=recovery_age else 'recent'
    elapsed = age(latest,now,'updated_at' if latest.get('updated_at') else 'created_at')
    failures=0
    for r in candidates:
        if age(r,now)>90: break
        if r.get('status')=='completed' and r.get('conclusion')=='success': break
        if r.get('status')=='completed': failures+=1
    recovery = next((r for r in candidates if r.get('event')=='workflow_dispatch'),None)
    cooldown_ok = failures<2 or recovery is None or age(recovery,now)>=failure_cooldown
    ready = elapsed>=2 and cooldown_ok
    return ('critical' if level=='critical' else 'warning'),ready,'retry_failure' if ready else 'failure_cooldown'


def github_request(url, token, method="GET", data=None):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "muse-approval-watchdog",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = Request(url, headers=headers, method=method, data=data)
    # A lost POST response may already have created a run. Recheck next tick.
    attempts = 3 if method == "GET" else 1
    for attempt in range(attempts):
        try:
            with urlopen(request, timeout=20) as response:
                body = response.read()
                return json.loads(body) if body else None
        except HTTPError as error:
            if error.code not in {429, 500, 502, 503, 504} or attempt == attempts - 1:
                raise
        except (URLError, TimeoutError):
            if attempt == attempts - 1:
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
    parser.add_argument("--recovery-age", type=int, default=17)
    parser.add_argument("--warning-age", type=int, default=25)
    parser.add_argument("--critical-age", type=int, default=60)
    parser.add_argument("--no-recovery", action="store_true", help="check health only; do not dispatch")
    args = parser.parse_args()
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    branch = os.environ.get("GITHUB_EVENT_REPOSITORY_DEFAULT_BRANCH", "main")
    if not token or not repo or "/" not in repo:
        print("MUSE_WATCHDOG=ERROR missing_github_context", flush=True)
        return 2
    base = f"https://api.github.com/repos/{repo}/actions/workflows/{WORKFLOW}"
    query_url = base + "/runs?per_page=100&branch=" + quote(branch, safe="")
    try:
        payload = github_request(query_url, token)
        if not isinstance(payload, dict) or not isinstance(payload.get("workflow_runs"), list):
            raise ValueError("invalid workflow runs response")
        runs = payload["workflow_runs"]
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

    recovery = "disabled_for_push" if dispatch and args.no_recovery else "not_needed"
    if dispatch and not args.no_recovery:
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
    return 1 if level in {"error", "critical"} else 0


if __name__ == "__main__":
    sys.exit(main())
