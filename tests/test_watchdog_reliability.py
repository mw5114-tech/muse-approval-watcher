import sys
import unittest
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import patch
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from check_health import assess, github_request

now=datetime(2026,11,26,12,tzinfo=timezone.utc)

def r(a, status='completed', conclusion='success', event='schedule', title='', finished=None):
    d={'created_at':(now-timedelta(minutes=a)).isoformat(),'status':status,'conclusion':conclusion,'event':event,'display_title':title}
    if finished is not None:d['updated_at']=(now-timedelta(minutes=finished)).isoformat()
    return d

class WatchdogReliabilityTest(unittest.TestCase):
    def test_recent(self):self.assertEqual(assess([r(15)],now),('healthy',False,'recent'))
    def test_boundary(self):self.assertFalse(assess([r(16)],now)[1]);self.assertTrue(assess([r(17)],now)[1])
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
    def test_no_prior_recovery(self):self.assertTrue(assess([r(5,conclusion='failure'),r(20,conclusion='failure'),r(35)],now)[1])
    def test_outage_not_reset_by_failure(self):self.assertEqual(assess([r(2,conclusion='failure'),r(17,conclusion='failure'),r(70)],now)[0],'critical')
    def test_observe_active(self):self.assertFalse(assess([r(1,'in_progress',None,'workflow_dispatch','Muse watcher / observe'),r(40)],now)[1])
    def test_invalid_time(self):self.assertEqual(assess([r(-10)],now)[0],'error')
    def test_unknown_status(self):self.assertEqual(assess([r(1,'other')],now)[0],'error')
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
