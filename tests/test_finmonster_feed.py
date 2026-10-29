import copy
import tempfile
import unittest

from research.trader_review.finmonster_feed import Feed, Observer, TRADER


def order(identifier='a', stamp=900):
    return dict(orderId=identifier, positionId='p', symbol='AGTUSDT', side='SELL',
                action='OPEN', status='Filled', updatedAt=stamp, quantity=10, price=.1)


class FakeFeed:
    def __init__(self):
        self.profile = dict(traderId=TRADER, name='FinMonster')
        self.positions = dict(hidden=False, positions=[])
        self.pages = {None: dict(orders=[order()], nextCursor=None)}
    def get(self, endpoint, **params):
        return copy.deepcopy({'traderProfile': self.profile, 'openPositions': self.positions,
                              'orderHistory': self.pages.get(params.get('cursor'))}[endpoint])


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = self.temp.name + '/observations.sqlite'
        self.observer, self.feed = Observer(self.path), FakeFeed()
    def tearDown(self):
        self.observer.db.close()
        self.temp.cleanup()
    def test_restart_deduplicates_and_new_event_is_prospective(self):
        self.observer.poll(self.feed, 1000)
        self.observer.db.close()
        self.observer = Observer(self.path)
        self.assertEqual(self.observer.poll(self.feed, 1100)['new_order_versions'], 0)
        self.feed.pages[None]['orders'].insert(0, order('b', 1150))
        result = self.observer.poll(self.feed, 1200)
        self.assertEqual(result['new_order_versions'], 1)
        self.assertFalse(result['paper_trading_started'])
        self.assertEqual(self.observer.db.execute('SELECT category FROM observations ORDER BY source_ms').fetchall(),
                         [('baseline',), ('observed',)])
    def test_pagination_finds_anchor_and_does_not_lose_new_rows(self):
        self.observer.poll(self.feed, 1000)
        self.feed.pages = {None: dict(orders=[order('b', 1100)], nextCursor='next'),
                           'next': dict(orders=[order()], nextCursor=None)}
        self.assertEqual(self.observer.poll(self.feed, 1200)['new_order_versions'], 1)
    def test_hidden_or_wrong_identity_does_not_mutate_journal(self):
        self.feed.positions['hidden'] = True
        with self.assertRaises(ValueError): self.observer.poll(self.feed, 1000)
        self.feed.positions['hidden'] = False
        self.feed.profile['name'] = 'Other'
        with self.assertRaises(ValueError): self.observer.poll(self.feed, 1000)
        self.assertEqual(self.observer.db.execute('SELECT count(*) FROM polls').fetchone()[0], 0)
    def test_gap_or_bad_timestamp_does_not_advance_anchor(self):
        self.observer.poll(self.feed, 1000)
        self.feed.pages[None]['orders'] = [order('b', 1100)]
        with self.assertRaisesRegex(ValueError, 'gap'): self.observer.poll(self.feed, 1200)
        self.assertEqual(self.observer.db.execute("SELECT value FROM meta WHERE key='anchor'").fetchone()[0], 'a')
        self.feed.pages[None]['orders'] = [order(), order('b', 999999999)]
        with self.assertRaises(ValueError): self.observer.poll(self.feed, 1200)
        self.assertEqual(self.observer.db.execute('SELECT count(*) FROM polls').fetchone()[0], 1)
    def test_partial_close_remains_an_event_not_a_full_position_close(self):
        event = order(); event.update(action='CLOSE', quantity=3)
        self.feed.pages[None]['orders'] = [event]
        self.observer.poll(self.feed, 1000)
        import json
        stored = json.loads(self.observer.db.execute('SELECT payload FROM observations').fetchone()[0])
        self.assertEqual(stored['quantity'], 3)
        self.assertEqual(stored['action'], 'CLOSE')
    def test_only_read_endpoints_are_allowed(self):
        with self.assertRaises(ValueError): Feed().get('placeOrder')


if __name__ == '__main__':
    unittest.main()
