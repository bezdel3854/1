import contextlib
import gzip
import json
import sqlite3
import tempfile
import time
import types
import unittest
from decimal import Decimal as D
from unittest.mock import Mock, patch

from research.trader_review.bingx_account_stream import AccountOrderStream, decode_message, record_event
from research.trader_review.bingx_execution import HyperliquidBingXCopy, ReconciliationRequired
from research.trader_review.strict_bingx_copy import StrictBingXExecution
import tests.test_full_copy_contract as fixtures


class StreamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = self.tmp.name + '/events.sqlite'

    def tearDown(self):
        self.tmp.cleanup()

    def test_parent_links_preserve_large_ids_and_survive_missing_link_on_update(self):
        parent, oid = '9000000000000000001', 9000000000000000002
        message = {'e': 'TRADE_UPDATE', 'o': {'i': oid, 'li': parent, 's': 'BTC-USDT'}}
        decoded = decode_message(gzip.compress(json.dumps(message).encode()))
        record_event(self.path, decoded)
        record_event(self.path, {'e': 'ORDER_TRADE_UPDATE', 'o': {'i': oid, 's': 'BTC-USDT'}})
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT order_id,parent_id FROM copy_exchange_links').fetchone(),
                             (str(oid), parent))
        for change in ({'li': '123'}, {'s': 'SOL-USDT'}, {'i': float(oid)}):
            with self.assertRaises(ValueError):
                record_event(self.path, {'e': 'TRADE_UPDATE', 'o': {**message['o'], **change}})

    def test_session_decodes_ping_and_persists_events_without_trading_requests(self):
        execution = Mock(environment='live', api_origin='https://open-api.bingx.com')
        stream = AccountOrderStream(execution, self.path)
        self.assertFalse(stream.health()['ready'])
        with self.assertRaises(ReconciliationRequired):
            stream.require_ready()
        message = {'e': 'TRADE_UPDATE', 'o': {'i': '123', 'li': '456', 's': 'BTC-USDT'}}
        class StopSession(Exception):
            pass
        ws = Mock()
        ws.recv.side_effect = [gzip.compress(b'Ping'), json.dumps(message), StopSession]
        with patch.object(stream, '_auth', return_value='private-key'), \
                patch('websockets.sync.client.connect', return_value=contextlib.nullcontext(ws)):
            with self.assertRaises(StopSession):
                stream.session()
        execution.verify_account_identity.assert_called_once()
        execution._request.assert_not_called()
        ws.send.assert_called_once_with('Pong')
        self.assertTrue(stream.health()['ready'])
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT parent_id FROM copy_exchange_links').fetchone(), ('456',))


class ImmediateProtectionTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.StrictExecutionTests()
        self.case.setUp()
        self.x = self.case.x
        self.db = sqlite3.connect(self.case.path)
        self.db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)')
        self.copy = HyperliquidBingXCopy(self.db, self.x, 1)
        self.event = dict(orderId='entry', positionId='epoch', symbol='BTCUSDT', action='OPEN',
                          side='BUY', quantity=10, price=100, updatedAt=time.time_ns()//1_000_000-1000)
        self.x.set_context(self.event, D('0'), D('.085'))
        self.x.account_stream = types.SimpleNamespace(require_ready=lambda: None)
        self.market = tuple(map(D, ('100', '.001', '.001', '2')))
        self.parent_id = '9000000000000000001'
        self.wrong_parent = False
        self.partial = False
        self.cancel_sibling = False
        self.events_path = self.case.path
        self.history, self.parents = {}, {}
        self.position_qty = D('0')
        self.original_request = self.case.request
        self.x._request = self.request

    def tearDown(self):
        self.db.close()
        self.case.tearDown()

    def request(self, method, path, **kwargs):
        if path == 'user/positions':
            self.case.requests.append((method, path, kwargs))
            return ([dict(symbol='BTC-USDT', positionSide='LONG', positionAmt=str(self.position_qty),
                          availableAmt=str(self.position_qty))] if self.position_qty else [])
        if path == 'trade/order' and method == 'POST':
            result = self.original_request(method, path, **kwargs)
            parent_id = str(int(self.parent_id) + len(self.parents))
            self.parents[kwargs['clientOrderId']] = dict(orderId=parent_id, symbol='BTC-USDT',
                side=kwargs['side'], status='FILLED', executedQty=kwargs['quantity'])
            if kwargs.get('reduceOnly') == 'true':
                self.position_qty = D('0')
                return result
            self.position_qty = D('.400') if self.partial else D('0')
            for order in self.case.orders:
                if order['orderId'] in self.history:
                    order['orderId'] = str(max(map(int, self.history)) + 1)
                sl = order['type'] == 'STOP_MARKET'
                self.history[order['orderId']] = {**order,
                    'status': ('PARTIALLYFILLED' if self.partial else 'FILLED') if sl else ('CANCELLED' if self.cancel_sibling else 'NEW'),
                    'executedQty': ('.450' if self.partial else '.850') if sl else '0'}
                record_event(self.events_path, {'e': 'TRADE_UPDATE', 'o': {
                    'i': order['orderId'], 'li': '999' if self.wrong_parent else parent_id,
                    's': 'BTC-USDT'}})
            if not self.partial:
                self.case.orders[:] = [o for o in self.case.orders if o['type'] != 'STOP_MARKET' and not self.cancel_sibling]
            return result
        if path == 'trade/order' and method == 'GET':
            self.case.requests.append((method, path, kwargs))
            if 'clientOrderId' in kwargs:
                return self.parents[kwargs['clientOrderId']]
            return self.history[kwargs['orderId']]
        if path == 'trade/order' and method == 'DELETE':
            self.case.requests.append((method, path, kwargs))
            self.case.orders[:] = [o for o in self.case.orders if o['orderId'] != kwargs['orderId']]
            self.history[kwargs['orderId']]['status'] = 'CANCELLED'
            return {}
        return self.original_request(method, path, **kwargs)

    def entry_posts(self):
        return sum(m == 'POST' and p == 'trade/order' and k.get('reduceOnly') != 'true'
                   for m, p, k in self.case.requests)

    def test_immediate_stop_is_adopted_by_parent_then_closed_without_duplicate_entry(self):
        self.assertEqual(self.copy.apply(self.event, D('0'), D('.085'), self.market), D('.850'))
        self.x.synchronize_exits(self.db)
        self.assertIsNone(self.db.execute('SELECT * FROM copied_positions').fetchone())
        self.x.synchronize_exits(self.db)
        self.assertEqual(self.copy.apply(self.event, D('0'), D('.085'), self.market), None)
        self.assertEqual(self.entry_posts(), 1)
        self.assertEqual(self.db.execute('SELECT adopted FROM copy_entry_children').fetchone(), (1,))
        self.assertEqual(D(self.db.execute('SELECT executed_qty FROM copy_exit_fills WHERE order_id=\'1\'').fetchone()[0]), D('.850'))
        self.assertEqual(self.case.orders, [])

    def test_wrong_parent_never_adopts_similar_historical_protection(self):
        self.wrong_parent = True
        for _ in range(2):
            with self.assertRaises(ReconciliationRequired):
                self.copy.apply(self.event, D('0'), D('.085'), self.market)
        self.assertEqual(self.entry_posts(), 1)
        self.assertEqual(self.db.execute('SELECT adopted FROM copy_entry_children').fetchone(), (0,))
        self.assertEqual(self.db.execute('SELECT count(*) FROM copied_signals').fetchone(), (0,))

    def test_immediate_stop_and_cancelled_sibling_recover_with_no_open_children(self):
        self.cancel_sibling = True
        self.copy.apply(self.event, D('0'), D('.085'), self.market)
        self.x.synchronize_exits(self.db)
        self.x.synchronize_exits(self.db)
        self.assertEqual(self.db.execute('SELECT count(*) FROM copied_positions').fetchone(), (0,))
        self.assertEqual(self.entry_posts(), 1)
        self.assertFalse(any(m == 'DELETE' for m, _, _ in self.case.requests))

    def test_secondary_trader_recovers_from_shared_account_event_journal(self):
        self.events_path = self.case.tmp.name + '/shared-account.sqlite'
        self.x.event_journal_path = self.events_path
        self.copy.apply(self.event, D('0'), D('.085'), self.market)
        self.x.synchronize_exits(self.db)
        self.assertEqual(self.db.execute('SELECT count(*) FROM copied_positions').fetchone(), (0,))
        self.assertEqual(self.db.execute('SELECT count(*) FROM copy_exchange_links').fetchone(), (0,))
        self.assertEqual(self.entry_posts(), 1)

    def test_crash_after_child_adoption_keeps_proof_until_entry_journal_commit(self):
        self.db.execute('INSERT INTO copy_plans VALUES (?,?,?,?)', ('entry', '.085', '0', '.850'))
        self.db.commit()
        self.x.submit('entry', 'BTC-USDT', 'BUY', D('.850'), deadline_ms=self.case.deadline)
        restarted = StrictBingXExecution('test-key', 'test-secret', self.case.path, trading_enabled=True,
            source_client=None, source_address='public', expected_uid='123')
        restarted._request, restarted.hedge_mode = self.request, lambda: False
        restarted.set_context(self.event, D('0'), D('.085'))
        self.x, self.copy.execution = restarted, restarted
        with self.assertRaisesRegex(ReconciliationRequired, 'journal not committed'):
            self.x.synchronize_exits(self.db)
        self.assertEqual(self.db.execute('SELECT count(*) FROM copy_exit_orders').fetchone(), (2,))
        self.copy.apply(self.event, D('0'), D('.085'), self.market)
        self.x.synchronize_exits(self.db)
        self.assertEqual(self.db.execute('SELECT count(*) FROM copied_positions').fetchone(), (0,))
        self.assertEqual(self.entry_posts(), 1)

    def test_disconnected_stream_never_sends_new_entry_but_allows_later_recovery(self):
        ready = Mock(side_effect=ReconciliationRequired('disconnected'))
        self.x.account_stream = types.SimpleNamespace(require_ready=ready)
        with self.assertRaises(ReconciliationRequired):
            self.copy.apply(self.event, D('0'), D('.085'), self.market)
        self.assertEqual(self.entry_posts(), 0)
        ready.side_effect = None
        self.copy.apply(self.event, D('0'), D('.085'), self.market)
        self.assertEqual(self.entry_posts(), 1)

    def test_partial_protection_fill_keeps_remaining_quantity(self):
        from research.trader_review.full_copy_contract import CopyBlocked
        self.partial = True
        self.copy.apply(self.event, D('0'), D('.085'), self.market)
        self.x._contract = Mock(side_effect=CopyBlocked('source_snapshot_changed'))
        self.x.synchronize_exits(self.db)
        self.assertEqual(self.db.execute('SELECT quantity FROM copied_positions').fetchone(), ('0.400',))
        close = {**self.event, 'orderId': 'source-close', 'action': 'CLOSE', 'side': 'SELL'}
        self.x.account_stream.require_ready = Mock(side_effect=ReconciliationRequired('stream disconnected'))
        self.copy.apply(close, D('10'), D('0'), self.market)
        closes = [k['quantity'] for m, p, k in self.case.requests
                  if m == 'POST' and p == 'trade/order' and k.get('reduceOnly') == 'true']
        self.assertEqual(closes, ['0.400'])
        self.assertEqual(self.entry_posts(), 1)

    def test_cancel_racing_with_stop_fill_preserves_evidence_until_accounted(self):
        self.copy.apply(self.event, D('0'), D('.085'), self.market)
        original = self.x._request
        queried = False
        def race(method, path, **kwargs):
            nonlocal queried
            if path == 'trade/order' and kwargs.get('orderId') == '1':
                if method == 'GET' and not queried:
                    queried = True
                    return {**self.history['1'], 'status': 'NEW', 'executedQty': '0'}
                if method == 'DELETE':
                    self.case.requests.append((method, path, kwargs))
                    self.history['1']['status'] = 'FILLED'
                    return {}
            return original(method, path, **kwargs)
        self.x._request = race
        with sqlite3.connect(self.case.path) as journal:
            filled = self.x._dispatch_exit(journal, 'epoch',
                dict(action='cancel', source_id='100', order_id='1'), 'BTC-USDT')
            self.assertTrue(filled)
            self.assertEqual(journal.execute("SELECT order_id FROM copy_exit_orders WHERE source_id='100'").fetchone(), ('1',))
        self.x.synchronize_exits(self.db)
        self.assertEqual(self.db.execute('SELECT count(*) FROM copied_positions').fetchone(), (0,))
        self.assertEqual(self.entry_posts(), 1)

    def test_new_epoch_waits_for_old_take_profit_cleanup_then_enters_once(self):
        self.copy.apply(self.event, D('0'), D('.085'), self.market)
        self.x.synchronize_exits(self.db)
        self.assertTrue(self.case.orders)  # Old TP is still active after SL.
        fresh = {**self.event, 'orderId': 'next-entry', 'positionId': 'next-epoch'}
        self.x.set_context(fresh, D('0'), D('.085'))
        with self.assertRaisesRegex(ReconciliationRequired, 'Previous epoch'):
            self.copy.apply(fresh, D('0'), D('.085'), self.market)
        self.assertEqual(self.entry_posts(), 1)
        self.x.synchronize_exits(self.db)
        self.assertEqual(self.case.orders, [])
        self.copy.apply(fresh, D('0'), D('.085'), self.market)
        self.assertEqual(self.entry_posts(), 2)


if __name__ == '__main__':
    unittest.main()
