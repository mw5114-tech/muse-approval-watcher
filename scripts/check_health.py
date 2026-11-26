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
    if not isinstance(value, str):
        raise ValueError("invalid timestamp type")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp missing timezone")
    return parsed


def age(item, now, field='created_at'):
    return max(0, (now-parse_iso(item[field])).total_seconds()/60)


def assess(runs, now, recovery_age=12, warning_age=25, critical_age=60, stuck_age=20, failure_cooldown=30):
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
    # Only a failed extra attempt in the current failure streak limits recovery.
    recovery = next((r for r in candidates[:failures] if r.get("event") in {"workflow_dispatch", "push"}), None)
    cooldown_ok = failures<2 or recovery is None or age(recovery,now)>=failure_cooldown
    ready = elapsed>=2 and cooldown_ok
    return ('critical' if level=='critical' else 'warning'),ready,'retry_failure' if ready else 'failure_cooldown'



def active_diagnostics(runs, now, queue_warning=10, execution_warning=12):
    """Read-only timing diagnostics. Never changes recovery or approval policy."""
    details = []
    for run in runs:
        status = run.get("status")
        if status not in ACTIVE or not run.get("created_at"):
            continue
        try:
            created = parse_iso(run["created_at"])
            if created > now:
                continue
            total = (now - created).total_seconds() / 60
            if status == "in_progress":
                started = parse_iso(run["run_started_at"]) if run.get("run_started_at") else None
                if started is not None and created <= started <= now:
                    queue = (started - created).total_seconds() / 60
                    running = (now - started).total_seconds() / 60
                    detail = f"running_minutes={running:.1f} queue_minutes={queue:.1f}"
                    warning = running >= execution_warning or queue >= queue_warning
                else:
                    detail = f"active_minutes={total:.1f} start_time_unknown"
                    warning = total >= execution_warning
            else:
                detail = f"waiting_minutes={total:.1f}"
                warning = total >= queue_warning
            details.append((warning, f"status={status} {detail}"))
        except (TypeError, ValueError, KeyError):
            details.append((True, "active_timing_unavailable"))
    return details


def emit_diagnostics(runs, now):
    for warning, detail in active_diagnostics(runs, now):
        if warning:
            print(f"::warning title=Muse workflow timing::{detail}", flush=True)
        else:
            print(f"MUSE_TIMING {detail}", flush=True)


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


def api_error_detail(error):
    """Expose failure category and HTTP status, never URLs, tokens or bodies."""
    if isinstance(error, HTTPError):
        status = error.code
        if status in {401, 403}:
            category = "auth_permission_or_rate_limit"
        elif status == 404:
            category = "resource_not_found_or_inaccessible"
        elif status == 429:
            category = "rate_limited"
        elif status >= 500:
            category = "github_server_error"
        else:
            category = "http_error"
        return f"{category} http_status={status}"
    if isinstance(error, (URLError, TimeoutError)):
        return "network_or_timeout"
    return f"response_error type={type(error).__name__}"


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
    except (HTTPError, URLError, TimeoutError) as error:
        detail = api_error_detail(error)
        print(f"::error title=Muse watchdog API read::{detail}", flush=True)
        print(f"MUSE_WATCHDOG=ERROR read_api={detail}", flush=True)
        append_summary("error", detail, "none")
        return 2
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        detail = f"invalid_workflow_run_data type={type(error).__name__}"
        print(f"::error title=Muse watchdog data validation::{detail}", flush=True)
        append_summary("error", detail, "none")
        return 2

    emit_diagnostics(runs, datetime.now(timezone.utc))
    if level in {"warning", "critical", "error"}:
        print(f"::warning title=Muse watchdog health::level={level} reason={reason}", flush=True)

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
            detail = api_error_detail(error)
            print(f"::error title=Muse watchdog dispatch::{detail}", flush=True)
            print(f"MUSE_WATCHDOG=ERROR dispatch_api={detail}", flush=True)
            append_summary("error", detail, "dispatch_failed")
            return 2
    print(f"MUSE_WATCHDOG={level.upper()} reason={reason} recovery={recovery}", flush=True)
    append_summary(level, reason, recovery)

    # A dispatch was accepted, so a gap alone is not a failure; the next
    # watchdog run will re-check. Avoid false "all jobs failed" notifications
    # for transient GitHub scheduling delays.
    if level in {"error", "critical"}:
        print(f"::error title=Muse watchdog health failure::level={level} reason={reason} recovery={recovery}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
