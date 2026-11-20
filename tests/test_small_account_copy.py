import json
import sqlite3
import time
import types
import unittest
from decimal import Decimal as D

from research.trader_review.bingx_copy_worker import cycle
from research.trader_review.bingx_execution import HyperliquidBingXCopy
from tests.test_bingx_freshness import FakeExecution


class SmallAccountCopyTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.execute('CREATE TABLE meta (key TEXT PRIMARY KEY,value TEXT)')
        self.db.execute('CREATE TABLE observations (order_id TEXT,payload TEXT,category TEXT)')
        self.db.execute('CREATE TABLE hl_fills (id TEXT,payload TEXT)')
        self.x = FakeExecution()
        self.x.equity = lambda: D('85')
        self.x._request = self.quote
        self.copier = HyperliquidBingXCopy(self.db, self.x, 1)
        self.source = types.SimpleNamespace(poll=lambda: None, address='test',
            client=types.SimpleNamespace(get=lambda *a, **k: {'marginSummary': {'accountValue': '1000'}}))

    def tearDown(self):
        self.db.close()

    def quote(self, method, path, **kwargs):
        self.assertEqual(method, 'GET')
        if path == 'quote/price':
            return {'price': '100'}
        symbol = kwargs['symbol']
        return [] if symbol == 'UNKNOWN-USDT' else [{'symbol': symbol,
            'quantityPrecision': 3, 'tradeMinQuantity': '.001', 'tradeMinUSDT': '5'}]

    def add(self, coin='BTC', action='OPEN', before='0', qty=10, epoch=None):
        n = self.db.execute('SELECT count(*) FROM observations').fetchone()[0] + 1
        event = {'orderId': f'hl:{coin}:{n}:{n}:'+action.lower(),
                 'positionId': epoch or f'{coin}:position', 'symbol': coin+'USDT',
                 'action': action, 'side': 'BUY' if action == 'OPEN' else 'SELL',
                 'quantity': qty, 'updatedAt': time.time_ns()//1_000_000-1000, 'price': 100}
        self.db.execute('INSERT INTO observations VALUES (?,?,?)',
                        (event['orderId'], json.dumps(event), 'observed'))
        self.db.execute('INSERT INTO hl_fills VALUES (?,?)',
                        (f'{coin}:{n}:{n}', json.dumps({'startPosition': before})))
        return event

    def test_85_account_requires_explicit_lower_threshold_and_uses_actual_equity(self):
        self.add()
        cycle(self.source, self.copier, self.x)
        self.assertFalse(self.x.orders)
        self.add(epoch='new')
        cycle(self.source, self.copier, self.x, min_equity=D('50'))
        self.assertEqual(self.x.orders[0][2], D('.850'))

    def test_no_catchup_after_balance_increases(self):
        self.add()
        cycle(self.source, self.copier, self.x)
        self.x.equity = lambda: D('150')
        cycle(self.source, self.copier, self.x)
        self.assertFalse(self.x.orders)

    def test_selection_filter_does_not_block_existing_position_close_below_threshold(self):
        self.db.execute("INSERT INTO copied_positions VALUES ('old','.085','1')")
        self.add('ETH', 'CLOSE', '10', qty=5, epoch='old')
        self.x.equity = lambda: D('5')
        cycle(self.source, self.copier, self.x, allowed_symbols={'BTC-USDT'})
        self.assertEqual(self.x.orders[0][2], D('.500'))
        self.assertTrue(self.x.orders[0][3])

    def test_unsupported_or_unselected_entry_does_not_block_next_supported_entry(self):
        self.add('UNKNOWN')
        self.add('BTC')
        cycle(self.source, self.copier, self.x, min_equity=D('50'))
        self.assertEqual(len(self.x.orders), 1)
        self.assertEqual(self.db.execute('SELECT reason FROM copy_skips').fetchone()[0],
                         'unsupported_contract')
        self.add('ETH')
        cycle(self.source, self.copier, self.x, min_equity=D('50'), allowed_symbols={'BTC-USDT'})
        self.assertEqual(len(self.x.orders), 1)
        self.assertEqual(self.db.execute('SELECT reason FROM copy_skips ORDER BY rowid DESC').fetchone()[0],
                         'symbol_not_selected')


if __name__ == '__main__':
    unittest.main()
