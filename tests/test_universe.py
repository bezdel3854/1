import copy
import json
from pathlib import Path
import unittest

from research.trader_review.universe import select


class UniverseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot = json.loads((Path(__file__).resolve().parents[1]/'reports/trader-review/multicoin-snapshot.json').read_text())

    def test_snapshot_produces_many_symbols_sorted_by_quote_turnover(self):
        r = select(self.snapshot, now=self.snapshot['asof_ms'])
        self.assertEqual(len(r['selected']), 30)
        self.assertGreater(len({x['symbol'] for x in r['selected']} - {'BTC-USDT', 'ETH-USDT'}), 20)
        volumes = [x['quote_volume_usdt'] for x in r['selected']]
        self.assertEqual(volumes, sorted(volumes, reverse=True))
        self.assertTrue(r['research_only'])

    def test_stale_or_future_snapshot_cannot_be_used_as_current(self):
        for delta in (300001, -1):
            with self.assertRaises(ValueError):
                select(self.snapshot, now=self.snapshot['asof_ms']+delta)

    def test_untradeable_contract_removed_even_when_most_liquid(self):
        snapshot = copy.deepcopy(self.snapshot)
        btc = next(c for c in snapshot['contracts'] if c['symbol']=='BTC-USDT')
        btc['apiStateOpen'] = 'false'
        r = select(snapshot, now=snapshot['asof_ms'])
        self.assertNotIn('BTC-USDT', [x['symbol'] for x in r['selected']])

    def test_missing_book_or_nonfinite_volume_fail_closed(self):
        for field, value in [('bidPrice', None), ('quoteVolume', 'NaN')]:
            snapshot = copy.deepcopy(self.snapshot)
            btc = next(t for t in snapshot['tickers'] if t['symbol']=='BTC-USDT')
            btc[field] = value
            r = select(snapshot, now=snapshot['asof_ms'])
            self.assertNotIn('BTC-USDT', [x['symbol'] for x in r['selected']])

    def test_duplicate_ticker_rejected(self):
        snapshot = copy.deepcopy(self.snapshot)
        snapshot['tickers'].append(snapshot['tickers'][0])
        with self.assertRaises(ValueError):
            select(snapshot, now=snapshot['asof_ms'])
