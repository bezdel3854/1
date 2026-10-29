import json
import tempfile
import unittest

from bingx_bot.data import Rules, Funding
from research.trader_review.finmonster_feed import Observer, canonical
from research.trader_review.finmonster_paper import Paper


class FakeMarket:
    def __init__(self):
        self.bid, self.ask, self.stamp = 99.98, 100.02, 1000000
        self.funds = []
    def scope(self, symbols): pass
    def quote(self, symbol): return self.bid, self.ask, self.stamp
    def rules(self, symbol): return Rules('.001', .001, 1)
    def funding(self, symbol, start, end): return [f for f in self.funds if start <= f.ts < end]


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.observer = Observer(self.temp.name+'/paper.sqlite')
        self.paper, self.market = Paper(self.observer.db), FakeMarket()
    def tearDown(self):
        self.observer.db.close(); self.temp.cleanup()
    def event(self, identifier, action='OPEN', side='BUY', qty=100, stamp=999990, position='p', category='observed'):
        body = dict(orderId=identifier, positionId=position, symbol='AGTUSDT', action=action,
                    side=side, quantity=qty, price=100, status='Filled', updatedAt=stamp)
        with self.observer.db:
            self.observer.db.execute('INSERT INTO observations VALUES(?,?,?,?,?,?)',
                                     (identifier,identifier,stamp,1000000,category,canonical(body)))
    def test_baseline_never_opens_and_stale_event_is_rejected(self):
        self.event('a', category='baseline')
        self.event('b', stamp=1)
        result=self.paper.step(self.market, 1000000)
        self.assertEqual(result['positions'],0); self.assertEqual(result['cash'],1000)
        self.assertIn('stale_source_event',self.observer.db.execute('SELECT result FROM paper_orders').fetchone()[0])
    def test_entry_at_observed_ask_with_slippage_and_shared_risk(self):
        self.event('a')
        self.paper.step(self.market,1000000)
        state=self.paper.state(); position=state['broker']['positions']['AGT-USDT']
        self.assertAlmostEqual(position['entry'],self.market.ask*1.0002)
        self.assertLessEqual(position['budget'],5)
        self.assertNotEqual(position['qty'],100)
        self.assertEqual(Paper(self.observer.db).step(self.market,1000000)['source_decisions'],0)
    def test_partial_close_funding_and_restart_accounting(self):
        self.event('a'); self.paper.step(self.market,1000000)
        initial=self.paper.state()['broker']['positions']['AGT-USDT']['qty']
        self.market.stamp=1001000; self.market.bid=101; self.market.ask=101.02
        self.market.funds=[Funding(1000500,.001,100)]
        self.event('b','CLOSE','SELL',25,1000990)
        self.paper.step(self.market,1001000)
        state=self.paper.state(); position=state['broker']['positions']['AGT-USDT']
        self.assertAlmostEqual(position['qty'],initial*.75)
        cash=state['broker']['cash']
        self.paper.step(self.market,1001001)
        self.assertEqual(self.paper.state()['broker']['cash'],cash)
        self.assertEqual(state['tracked']['AGT-USDT']['source_remaining'],75)
        self.event('c','CLOSE','SELL',75,1000999)
        self.paper.step(self.market,1001002)
        self.assertFalse(self.paper.state()['broker']['positions'])
        trades=[json.loads(x[0]) for x in self.observer.db.execute("SELECT payload FROM paper_journal WHERE kind='trade'")]
        self.assertEqual(len(trades),2)
        self.assertAlmostEqual(sum(t['funding'] for t in trades),initial*100*.001)
        self.assertTrue(all(t['net_after_share'] <= t['net'] for t in trades))
    def test_stale_book_rolls_back_entire_paper_decision(self):
        self.event('a'); self.market.stamp=1
        with self.assertRaises(ValueError):self.paper.step(self.market,1000000)
        self.assertIsNone(self.paper.state())
        self.assertEqual(self.observer.db.execute('SELECT count(*) FROM paper_orders').fetchone()[0],0)
    def test_gap_stop_uses_current_bid_not_requested_stop_price(self):
        self.event('a'); self.paper.step(self.market,1000000)
        self.market.bid=95; self.market.ask=95.02; self.market.stamp=1001000
        result=self.paper.step(self.market,1001000)
        self.assertEqual(result['positions'],0)
        trade=json.loads(self.observer.db.execute("SELECT payload FROM paper_journal WHERE kind='trade'").fetchone()[0])
        self.assertAlmostEqual(trade['exit'],95*.9998)
        self.assertLess(trade['net'],-5) # Planned risk is not a guaranteed stop-loss cap.
    def test_roundtrip_already_in_same_batch_does_not_fake_fill(self):
        self.event('a'); self.event('b','CLOSE','SELL',100,999999)
        result=self.paper.step(self.market,1000000)
        self.assertEqual(result['positions'],0);self.assertEqual(result['cash'],1000)
    def test_source_outage_does_not_allow_new_entries(self):
        self.event('a');self.paper.step(self.market,1000000,allow_entries=False)
        self.assertFalse(self.paper.state()['broker']['positions'])


if __name__=='__main__': unittest.main()
