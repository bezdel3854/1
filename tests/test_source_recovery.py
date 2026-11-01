import io
import json
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from research.trader_review.finmonster_feed import Feed, FeedUnavailable, Observer
from research.trader_review.finmonster_paper import Paper
from research.trader_review.finmonster_worker import record_source_health
from tests import test_finmonster_feed as feed_tests


def unavailable(headers=None):
    return HTTPError('https://example.invalid',503,'unavailable',headers or {},
                     io.BytesIO(b'{"error":{"code":"SOURCE_UNAVAILABLE","message":"upstream unavailable"}}'))


class RecoveryTests(unittest.TestCase):
    def test_503_backoff_persists_and_unrelated_success_does_not_reset_it(self):
        with tempfile.TemporaryDirectory() as folder:
            observer=Observer(folder+'/paper.sqlite')
            with patch('research.trader_review.finmonster_feed.time.time',return_value=1000), \
                 patch('research.trader_review.finmonster_feed.urllib.request.urlopen',side_effect=unavailable()) as network:
                feed=Feed(db=observer.db)
                with self.assertRaises(FeedUnavailable) as caught:feed.get('orderHistory')
                self.assertEqual(caught.exception.retry_at_ms,1060000)
                self.assertEqual(caught.exception.reason,'source_unavailable')
                with self.assertRaises(FeedUnavailable):Feed(db=observer.db).get('orderHistory')
                self.assertEqual(network.call_count,1)
            feed=Feed(db=observer.db)
            with patch('research.trader_review.finmonster_feed.time.time',return_value=1061), \
                 patch('research.trader_review.finmonster_feed.urllib.request.urlopen',return_value=feed_tests.QuotaEfficiencyTests.response({})):
                feed.get('openPositions')
            with patch('research.trader_review.finmonster_feed.time.time',return_value=1061), \
                 patch('research.trader_review.finmonster_feed.urllib.request.urlopen',side_effect=unavailable()):
                with self.assertRaises(FeedUnavailable) as caught:feed.get('orderHistory')
                self.assertEqual(caught.exception.retry_at_ms,1181000)
            with patch('research.trader_review.finmonster_feed.time.time',return_value=1182), \
                 patch('research.trader_review.finmonster_feed.urllib.request.urlopen',return_value=feed_tests.QuotaEfficiencyTests.response({'orders':[]})):
                Feed(db=observer.db).get('orderHistory')
            self.assertIsNone(Feed(db=observer.db).cooldown)
            observer.db.close()

    def test_explicit_retry_after_is_respected_even_when_longer_than_default(self):
        with patch('research.trader_review.finmonster_feed.time.time',return_value=1000), \
             patch('research.trader_review.finmonster_feed.urllib.request.urlopen',side_effect=unavailable({'Retry-After':'900'})):
            with self.assertRaises(FeedUnavailable) as caught:Feed().get('orderHistory')
            self.assertEqual(caught.exception.retry_at_ms,1900000)

    def test_repeated_503_backoff_is_bounded_and_counted_against_quota(self):
        feed=Feed()
        now=1000
        for delay in (60,120,240,300,300):
            with patch('research.trader_review.finmonster_feed.time.time',return_value=now), \
                 patch('research.trader_review.finmonster_feed.urllib.request.urlopen',side_effect=unavailable()):
                with self.assertRaises(FeedUnavailable) as caught:feed.get('orderHistory')
                self.assertEqual(caught.exception.retry_at_ms,int((now+delay)*1000))
            now+=delay+1
        self.assertEqual(feed.usage['requests'],5)

    def test_health_journal_recovers_after_restart_without_duplicate_incidents(self):
        with tempfile.TemporaryDirectory() as folder:
            path=folder+'/paper.sqlite';observer=Observer(path);Paper(observer.db)
            record_source_health(observer.db,True,now_ms=1000)
            err={'endpoint':'orderHistory','http_status':503,'reason':'source_unavailable'}
            record_source_health(observer.db,False,err,now_ms=2000)
            record_source_health(observer.db,False,err,now_ms=3000)
            observer.db.close();observer=Observer(path)
            state=record_source_health(observer.db,True,now_ms=4000)
            rows=[json.loads(r[0]) for r in observer.db.execute("select payload from paper_journal where kind='source_health'")]
            self.assertEqual(len(rows),3)
            self.assertEqual(rows[-1]['outage_duration_ms'],2000)
            self.assertEqual(state['consecutive_failed_cycles'],0)
            self.assertNotIn('error',state)
            observer.db.close()
