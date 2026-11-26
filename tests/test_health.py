"""Pure offline tests of watchdog recovery decisions. No GitHub/Muse network."""
import sys
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from check_health import assess, main


NOW = datetime(2026, 11, 16, 4, 0, tzinfo=timezone.utc)


def run(minutes, status="completed", conclusion="success", event="schedule"):
    return {
        "event": event, "status": status, "conclusion": conclusion,
        "display_title": "Muse watcher / apply" if event == "workflow_dispatch" else "",
        "created_at": (NOW - timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z"),
    }


class WatchdogTests(unittest.TestCase):
    def test_recent_success_no_recovery(self):
        self.assertEqual(assess([run(5)], NOW)[:2], ("healthy", False))

    def test_ten_minute_schedule_grace_prevents_extra_dispatch(self):
        # A normal 10-minute scheduled watcher must not cause a recovery run.
        self.assertEqual(assess([run(10)], NOW)[:2], ("healthy", False))
        self.assertEqual(assess([run(16.9)], NOW)[:2], ("healthy", False))

    def test_missed_ten_minute_schedule_starts_recovery_after_grace(self):
        # Recover after one skipped slot plus a seven-minute grace period.
        level, dispatch, reason = assess([run(17)], NOW)
        self.assertTrue(dispatch)
        self.assertIn("schedule_gap", reason)
        self.assertEqual(level, "healthy")

    def test_delayed_31_minutes_auto_recovers_without_failure_mail(self):
        level, dispatch, reason = assess([run(30.8)], NOW)
        self.assertEqual((level, dispatch), ("warning", True))
        self.assertIn("schedule_gap", reason)

    def test_active_recent_workflow_dispatch_prevents_duplicate(self):
        level, dispatch, reason = assess([
            run(30.8), run(3, "in_progress", None, "workflow_dispatch")
        ], NOW)
        self.assertEqual((level, dispatch), ("warning", False))

    def test_stale_queue_is_not_duplicated(self):
        self.assertEqual(
            assess([run(26, "queued", None)], NOW)[:2], ("warning", False)
        )

    def test_recent_failed_job_is_recovered_with_cooldown(self):
        self.assertEqual(
            assess([run(3, "completed", "failure")], NOW)[:2], ("warning", True)
        )
        self.assertEqual(
            assess([run(1, "completed", "failure")], NOW)[:2], ("warning", False)
        )

    def test_push_health_check_never_dispatches_approvals(self):
        # The workflow's push trigger must check only, even if recovery
        # would otherwise be warranted. No GitHub API POST may occur.
        recent_failure = {
            "event": "schedule", "status": "completed", "conclusion": "failure",
            "created_at": (datetime.now(timezone.utc) -
                           timedelta(minutes=5)).isoformat(),
        }
        with (
            patch.dict("check_health.os.environ", {
                "GITHUB_REPOSITORY": "example/muse-approval-watcher",
                "GITHUB_TOKEN": "placeholder-token-for-mock",
            }),
            patch("sys.argv", ["check_health.py", "--no-recovery"]),
            patch("check_health.github_request", return_value={
                "workflow_runs": [recent_failure]
            }) as request,
            patch("check_health.append_summary"),
        ):
            self.assertEqual(main(), 0)
            self.assertEqual(request.call_count, 1)
            self.assertEqual(request.call_args.kwargs.get("method", "GET"), "GET")

    def test_two_consecutive_failures_allow_first_extra_dispatch(self):
        # A first recovery is allowed; subsequent extra dispatches cool down.
        runs = [
            run(4, "completed", "failure"),
            run(50, "completed", "failure"),
        ]
        level, dispatch, reason = assess(runs, NOW)
        self.assertEqual((level, dispatch), ("warning", True))
        self.assertIn("retry_failure", reason)

    def test_repeated_failures_pause_watchdog_recovery(self):
        # Three consecutive failed jobs should not dispatch a fourth
        # near-identical job merely because GitHub cron is delayed.
        runs = [
            run(3, "completed", "failure"),
            run(15, "completed", "failure", "workflow_dispatch"),
            run(35, "completed", "failure"),
        ]
        severity, dispatch, reason = assess(runs, NOW)
        self.assertEqual((severity, dispatch), ("warning", False))
        self.assertIn("failure_cooldown", reason)

    def test_recovery_resumes_after_cooldown_and_success_resets_breaker(self):
        recent_failures = [
            run(46, "completed", "failure"),
            run(60, "completed", "failure"),
            run(80, "completed", "failure"),
        ]
        self.assertEqual(assess(recent_failures, NOW)[:2], ("critical", True))
        # A long outage still permits a recovery attempt.
        old_failures = [
            run(91, "completed", "failure"),
            run(105, "completed", "failure"),
            run(120, "completed", "failure"),
        ]
        self.assertEqual(assess(old_failures, NOW)[:2], ("critical", True))
        with_success = [run(1, event="workflow_dispatch")] + [
            run(3, "completed", "failure"),
            run(12, "completed", "failure"),
            run(25, "completed", "failure"),
        ]
        self.assertEqual(assess(with_success, NOW)[:2], ("healthy", False))

    def test_critical_60_minute_outage_still_alerts(self):
        self.assertEqual(assess([run(61)], NOW)[:2], ("critical", True))

    def test_no_runs_attempts_recovery(self):
        self.assertEqual(assess([], NOW)[:2], ("warning", True))

    def test_unexpected_status_requires_attention(self):
        self.assertEqual(
            assess([run(30, "error", None)], NOW)[:2], ("error", False)
        )

    def test_old_scheduled_run_is_not_dominant_if_manual_recovery_succeeded(self):
        self.assertEqual(
            assess([run(38), run(8, event="workflow_dispatch")], NOW)[:2],
            ("healthy", False),
        )

    def test_future_time_guard(self):
        self.assertEqual(assess([run(-10)], NOW)[:2], ("error", False))


if __name__ == "__main__":
    unittest.main()
