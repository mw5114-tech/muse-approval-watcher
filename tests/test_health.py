"""Pure offline tests of watchdog recovery decisions. No GitHub/Muse network."""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from check_health import assess


NOW = datetime(2026, 11, 16, 4, 0, tzinfo=timezone.utc)


def run(minutes, status="completed", conclusion="success", event="schedule"):
    return {
        "event": event, "status": status, "conclusion": conclusion,
        "created_at": (NOW - timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z"),
    }


class WatchdogTests(unittest.TestCase):
    def test_recent_success_no_recovery(self):
        self.assertEqual(assess([run(5)], NOW)[:2], ("healthy", False))

    def test_delayed_31_minutes_auto_recovers_without_failure_mail(self):
        level, dispatch, reason = assess([run(30.8)], NOW)
        self.assertEqual((level, dispatch), ("warning", True))
        self.assertIn("watcher_missing_schedule", reason)

    def test_active_recent_workflow_dispatch_prevents_duplicate(self):
        level, dispatch, reason = assess([
            run(30.8), run(3, "in_progress", None, "workflow_dispatch")
        ], NOW)
        self.assertEqual((level, dispatch), ("healthy", False))

    def test_stale_queue_is_recovered(self):
        self.assertEqual(
            assess([run(26, "queued", None)], NOW)[:2], ("warning", True)
        )

    def test_recent_failed_job_is_recovered_with_cooldown(self):
        self.assertEqual(
            assess([run(3, "completed", "failure")], NOW)[:2], ("warning", True)
        )
        self.assertEqual(
            assess([run(1, "completed", "failure")], NOW)[:2], ("warning", False)
        )

    def test_critical_90_minute_outage_still_alerts(self):
        self.assertEqual(assess([run(91)], NOW)[:2], ("critical", True))

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
