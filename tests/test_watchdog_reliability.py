import sys
import unittest
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import patch
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from check_health import assess, github_request, active_diagnostics, api_error_detail, parse_iso

now=datetime(2026,11,26,12,tzinfo=timezone.utc)

def r(a, status='completed', conclusion='success', event='schedule', title='', finished=None):
    d={'created_at':(now-timedelta(minutes=a)).isoformat(),'status':status,'conclusion':conclusion,'event':event,'display_title':title}
    if finished is not None:d['updated_at']=(now-timedelta(minutes=finished)).isoformat()
    return d

class WatchdogReliabilityTest(unittest.TestCase):
    def test_recent(self):self.assertEqual(assess([r(10)],now),('healthy',False,'recent'))
    def test_boundary(self):self.assertFalse(assess([r(11)],now)[1]);self.assertTrue(assess([r(12)],now)[1])
    def test_missing(self):self.assertTrue(assess([],now)[1])
    def test_active(self):self.assertFalse(assess([r(40),r(1,'queued',None)],now)[1])
    def test_stuck(self):self.assertEqual(assess([r(40,'queued',None),r(55)],now),('warning',False,'active_stuck'))
    def test_older_active(self):self.assertFalse(assess([r(24),r(30,'in_progress',None)],now)[1])
    def test_observe_success(self):self.assertTrue(assess([r(1,event='workflow_dispatch',title='Muse watcher / observe'),r(35)],now)[1])
    def test_apply_success(self):self.assertFalse(assess([r(1,event='workflow_dispatch',title='Muse watcher / apply'),r(35)],now)[1])
    def test_recent_failure(self):self.assertFalse(assess([r(8,conclusion='failure',finished=1),r(20)],now)[1])
    def test_single_failure(self):self.assertTrue(assess([r(8,conclusion='failure',finished=3),r(20)],now)[1])
    def test_multiple_failures(self):self.assertFalse(assess([r(8,conclusion='failure'),r(23,conclusion='failure',event='workflow_dispatch',title='Muse watcher / apply'),r(40)],now)[1])
    def test_cooldown_expires(self):self.assertTrue(assess([r(3,conclusion='failure'),r(16,conclusion='failure'),r(31,conclusion='failure',event='workflow_dispatch',title='Muse watcher / apply'),r(61)],now)[1])
    def test_push_failure_respects_cooldown(self):self.assertFalse(assess([r(4,conclusion='failure'),r(15,conclusion='failure',event='push',title='Muse watcher / apply'),r(35)],now)[1])
    def test_old_success_does_not_block_new_failure_streak(self):self.assertTrue(assess([r(3,conclusion='failure'),r(12,conclusion='failure'),r(22,event='workflow_dispatch',title='Muse watcher / apply')],now)[1])
    def test_no_prior_recovery(self):self.assertTrue(assess([r(5,conclusion='failure'),r(20,conclusion='failure'),r(35)],now)[1])
    def test_outage_not_reset_by_failure(self):self.assertEqual(assess([r(2,conclusion='failure'),r(17,conclusion='failure'),r(70)],now)[0],'critical')
    def test_observe_active(self):self.assertFalse(assess([r(1,'in_progress',None,'workflow_dispatch','Muse watcher / observe'),r(40)],now)[1])
    def test_invalid_time(self):self.assertEqual(assess([r(-10)],now)[0],'error')
    def test_unknown_status(self):self.assertEqual(assess([r(1,'other')],now)[0],'error')
    def test_api_error_categories_do_not_expose_response(self):
        for code, category in [(401,'auth_permission_or_rate_limit'),
                               (403,'auth_permission_or_rate_limit'),
                               (404,'resource_not_found_or_inaccessible'),
                               (429,'rate_limited'),
                               (503,'github_server_error')]:
            error=HTTPError('https://example.invalid/private',code,'sensitive body',None,None)
            self.assertEqual(api_error_detail(error),f'{category} http_status={code}')
        self.assertEqual(api_error_detail(URLError('private URL')), 'network_or_timeout')
        self.assertEqual(api_error_detail(TimeoutError()), 'network_or_timeout')
    def test_timestamp_validation(self):
        for bad in [None, 123, 'invalid', '2026-11-26T12:00:00']:
            with self.assertRaises(ValueError):
                parse_iso(bad)
        self.assertEqual(parse_iso('2026-11-26T12:00:00Z'), now)
    def test_queue_timing_warning(self):
        self.assertEqual(active_diagnostics([r(11,'queued',None)],now),
                         [(True,'status=queued waiting_minutes=11.0')])
        self.assertEqual(active_diagnostics([r(2,'queued',None)],now),
                         [(False,'status=queued waiting_minutes=2.0')])
    def test_running_timing_distinguishes_queue_and_runtime(self):
        run=r(15,'in_progress',None)
        run['run_started_at']=(now-timedelta(minutes=4)).isoformat()
        self.assertEqual(active_diagnostics([run],now),
                         [(True,'status=in_progress running_minutes=4.0 queue_minutes=11.0')])
        run['run_started_at']=(now-timedelta(minutes=13)).isoformat()
        self.assertEqual(active_diagnostics([run],now),
                         [(True,'status=in_progress running_minutes=13.0 queue_minutes=2.0')])
    def test_missing_start_time_is_not_fabricated(self):
        self.assertEqual(active_diagnostics([r(13,'in_progress',None)],now),
                         [(True,'status=in_progress active_minutes=13.0 start_time_unknown')])
    def test_completed_and_future_runs_not_reported_as_active(self):
        self.assertEqual(active_diagnostics([r(40),r(-1,'queued',None)],now),[])
    def test_invalid_start_time_is_nonfatal(self):
        run=r(15,'in_progress',None)
        run['run_started_at']='invalid'
        self.assertEqual(active_diagnostics([run],now),[(True,'active_timing_unavailable')])
    @patch('check_health.urlopen', side_effect=URLError('simulated timeout'))
    def test_post_not_repeated(self, mocked):
        with self.assertRaises(URLError):
            github_request('https://api.github.com/test','fake',method='POST',data=b'{}')
        self.assertEqual(mocked.call_count,1)
    @patch('check_health.time.sleep')
    @patch('check_health.urlopen', side_effect=URLError('simulated timeout'))
    def test_get_retries(self, mocked, sleep):
        with self.assertRaises(URLError):
            github_request('https://api.github.com/test','fake')
        self.assertEqual(mocked.call_count,3)

if __name__=='__main__':
    unittest.main()
