import io
import json
import sqlite3
import tempfile
import unittest
import importlib
import sys
import types
from decimal import Decimal as D
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from research.trader_review.bingx_execution import (
    BingXExecution, EntryExpired, HyperliquidBingXCopy, require_entry_deadline,
)


class FakeExecution:
    def __init__(self):
        self.orders = []
        self.pending = set()
        self.on_verify = lambda: None

    def needs_reconciliation(self, signal):
        return signal in self.pending

    def verify_position(self, *args):
        self.on_verify()

    def submit(self, signal, symbol, side, quantity, *, reduce_only=False, deadline_ms=None):
        if signal not in self.pending:
            require_entry_deadline(deadline_ms)
        self.orders.append((signal, side, quantity, reduce_only, deadline_ms))
        return {'status': 'FILLED', 'executedQty': str(quantity)}


class FreshnessTests(unittest.TestCase):
    def setUp(self):
        self.now = 10_000_000
        self.clock = patch('research.trader_review.bingx_execution.time.time_ns',
                           side_effect=lambda: self.now * 1_000_000)
        self.clock.start()
        self.db = sqlite3.connect(':memory:')
        self.db.execute('CREATE TABLE meta (key TEXT PRIMARY KEY,value TEXT)')
        self.execution = FakeExecution()
        self.copier = HyperliquidBingXCopy(self.db, self.execution, 1)
        self.market = tuple(map(D, ('100', '.001', '.001', '5')))

    def tearDown(self):
        self.db.close()
        self.clock.stop()

    def event(self, name='signal', age=1000, action='OPEN', qty=20, epoch='epoch'):
        return {'orderId': name, 'positionId': epoch, 'symbol': 'HYPEUSDT',
                'action': action, 'side': 'BUY' if action == 'OPEN' else 'SELL',
                'quantity': qty, 'updatedAt': self.now-age, 'price': 100}

    def apply(self, event, before=0):
        return self.copier.apply(event, D(before), D('.1'), self.market)

    def position(self):
        return self.db.execute('SELECT quantity FROM copied_positions').fetchone()

    def test_fresh_entry_has_absolute_source_deadline(self):
        event = self.event()
        self.assertEqual(self.apply(event), D('2'))
        self.assertEqual(self.execution.orders[0][-1], event['updatedAt']+30_000)

    def test_stale_open_and_add_never_send_or_change_position(self):
        self.db.execute("INSERT INTO copied_positions VALUES ('epoch','.1','5')")
        before = self.db.execute('SELECT * FROM copied_positions').fetchall()
        for age in (30_000, 3_600_000):
            self.assertIsNone(self.apply(self.event(str(age), age=age), before=100))
        self.assertEqual(self.db.execute('SELECT * FROM copied_positions').fetchall(), before)
        self.assertEqual(len(self.execution.orders), 0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM copy_skips').fetchone()[0], 2)
        self.assertEqual(self.db.execute('SELECT count(*) FROM copied_signals').fetchone()[0], 0)

    def test_partial_close_of_existing_position_still_runs_even_if_old(self):
        self.db.execute("INSERT INTO copied_positions VALUES ('epoch','.1','5')")
        self.assertEqual(self.apply(self.event(age=90_000, action='CLOSE', qty=25), before=100), D('3.75'))
        self.assertEqual(self.execution.orders[0][2:], (D('1.25'), True, None))

    def test_expiry_during_position_verification_does_not_open_or_consume_seed(self):
        self.copier.arm_add_once(self.now-2000)
        event = self.event(epoch='HYPE:prebaseline:1')
        self.execution.on_verify = lambda: setattr(self, 'now', self.now+40_000)
        self.assertIsNone(self.apply(event, before=100))
        self.assertIsNone(self.position())
        self.assertFalse(self.execution.orders)
        self.assertIsNone(self.db.execute("SELECT value FROM meta WHERE key='bingx_add_once_signal'").fetchone())
        self.assertEqual(self.db.execute('SELECT reason FROM copy_skips').fetchone()[0], 'expired_before_dispatch')

    def test_already_sent_order_is_reconciled_after_deadline(self):
        event = self.event(age=90_000)
        self.db.execute("INSERT INTO copy_plans VALUES ('signal','.1','0','2')")
        self.execution.pending.add('signal')
        self.assertEqual(self.apply(event), D('2'))
        self.assertEqual(self.position(), ('2',))

    def test_expired_plan_is_not_replayed_after_restart_or_clock_change(self):
        event = self.event(age=90_000)
        self.db.execute("INSERT INTO copy_plans VALUES ('signal','.1','0','2')")
        self.assertIsNone(self.apply(event))
        self.copier = HyperliquidBingXCopy(self.db, self.execution, 1)
        self.now = event['updatedAt']+1000
        self.assertIsNone(self.apply(event))
        self.assertFalse(self.execution.orders)

    def test_future_timestamp_is_skipped_and_recorded(self):
        self.assertIsNone(self.apply(self.event(age=-6000)))
        self.assertEqual(self.db.execute('SELECT reason FROM copy_skips').fetchone()[0], 'future_source_timestamp')

    def test_fresh_add_keeps_existing_position_and_ratio(self):
        self.db.execute("INSERT INTO copied_positions VALUES ('epoch','.1','5')")
        self.assertEqual(self.apply(self.event(qty=10), before=100), D('6'))
        self.assertEqual(self.db.execute('SELECT ratio FROM copied_positions').fetchone()[0], '0.1')

    def test_worker_advances_past_stale_add_and_still_copies_partial_close(self):
        feed = types.ModuleType('research.trader_review.finmonster_feed'); feed.Observer = object
        hyper = types.ModuleType('research.trader_review.hyperliquid_feed'); hyper.FillObserver = object
        with patch.dict(sys.modules, {'research.trader_review.finmonster_feed': feed,
                                     'research.trader_review.hyperliquid_feed': hyper}):
            worker = importlib.import_module('research.trader_review.bingx_copy_worker')
        self.db.execute('CREATE TABLE observations (order_id TEXT, payload TEXT, category TEXT)')
        self.db.execute('CREATE TABLE hl_fills (id TEXT, payload TEXT)')
        self.db.execute("INSERT INTO copied_positions VALUES ('epoch','.1','5')")
        for i, action in enumerate(('OPEN', 'CLOSE'), 1):
            event = self.event(f'hl:HYPE:{i}:{i}:'+action.lower(), age=90_000,
                               action=action, qty=25)
            self.db.execute('INSERT INTO observations VALUES (?,?,?)',
                            (event['orderId'],json.dumps(event),'observed'))
            self.db.execute('INSERT INTO hl_fills VALUES (?,?)',
                            (f'HYPE:{i}:{i}',json.dumps({'startPosition':'100'})))
        def quote(method, path, **params):
            self.assertEqual(method, 'GET')
            if path == 'quote/price': return {'price':'100'}
            return [{'symbol':'HYPE-USDT','quantityPrecision':3,'tradeMinQuantity':'.001','tradeMinUSDT':'5'}]
        self.execution._request = quote
        source = types.SimpleNamespace(poll=lambda: None)
        worker.cycle(source,self.copier,self.execution)
        self.assertEqual(self.position(), ('3.75',))
        self.assertEqual(self.db.execute("SELECT value FROM meta WHERE key='bingx_copy_row'").fetchone()[0], '2')
        self.assertEqual(len(self.execution.orders), 1)
        self.assertTrue(self.execution.orders[0][3])


class DispatchDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = self.folder.name+'/journal.sqlite'
        self.x = BingXExecution('key', 'secret', self.path, trading_enabled=True)
        self.now = 1_000_000
        self.clock = patch('research.trader_review.bingx_execution.time.time_ns',
                           side_effect=lambda: self.now*1_000_000)
        self.clock.start()

    def tearDown(self):
        self.clock.stop()
        self.folder.cleanup()

    def test_expired_dispatch_never_calls_network(self):
        with patch('research.trader_review.bingx_execution.urlopen') as request:
            with self.assertRaises(EntryExpired):
                self.x._request('POST', 'trade/order', _deadline_ms=self.now)
            request.assert_not_called()

    def test_receiving_window_cannot_outlive_source_deadline(self):
        response = io.BytesIO(json.dumps({'code': 0, 'data': {}}).encode())
        with patch('research.trader_review.bingx_execution.urlopen', return_value=response) as request:
            self.x._request('POST', 'trade/order', _deadline_ms=self.now+1200)
            params = parse_qs(urlsplit(request.call_args.args[0].full_url).query)
            self.assertEqual(params['recvWindow'], ['1200'])
            self.assertNotIn('_deadline_ms', params)

    def test_delay_in_mode_lookup_expires_unsent_claim(self):
        def slow_mode():
            self.now += 40_000
            return True
        with patch.object(self.x, 'hedge_mode', side_effect=slow_mode), patch.object(self.x, '_request') as request:
            with self.assertRaises(EntryExpired):
                self.x.submit('signal', 'HYPE-USDT', 'BUY', D('1'), deadline_ms=self.now+20_000)
            request.assert_not_called()
        self.assertFalse(self.x.has_submission('signal'))

    def test_existing_ambiguous_submission_is_not_reposted_after_deadline(self):
        with patch.object(self.x, 'hedge_mode', return_value=True), patch.object(self.x, '_request', side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):
                self.x.submit('signal', 'HYPE-USDT', 'BUY', D('1'), deadline_ms=self.now+20_000)
        self.now += 60_000
        with patch.object(self.x, 'hedge_mode', return_value=True), patch.object(self.x, '_request', return_value={}) as request:
            self.x.submit('signal', 'HYPE-USDT', 'BUY', D('1'), deadline_ms=self.now-40_000)
            self.assertEqual(request.call_args.args, ('GET', 'trade/order'))
            self.assertEqual(request.call_count, 1)

    def test_definitive_rejection_can_expire_but_timeout_must_reconcile(self):
        from research.trader_review.bingx_execution import BingXAPIError
        with patch.object(self.x, 'hedge_mode', return_value=True), patch.object(self.x, '_request',
                side_effect=BingXAPIError(109400,'bad parameters')):
            with self.assertRaises(BingXAPIError):
                self.x.submit('rejected','HYPE-USDT','BUY',D('1'))
        self.assertFalse(self.x.needs_reconciliation('rejected'))
        with patch.object(self.x,'hedge_mode',return_value=True), patch.object(self.x,'_request',side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):
                self.x.submit('uncertain','HYPE-USDT','BUY',D('1'))
        self.assertTrue(self.x.needs_reconciliation('uncertain'))


if __name__ == '__main__':
    unittest.main()
