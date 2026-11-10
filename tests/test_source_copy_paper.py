import json
import unittest
from pathlib import Path
from unittest.mock import Mock

from research.trader_review.source_copy_paper import SourceCopyPaper, PREVIOUS_POLICY
from research.trader_review.finmonster_feed import canonical
from tests import test_copy_increases as fixtures


class SourceOnlyTests(unittest.TestCase):
    tearDown = fixtures.CopyIncreaseTests.tearDown
    event = fixtures.CopyIncreaseTests.event
    step = fixtures.CopyIncreaseTests.step
    position = fixtures.CopyIncreaseTests.position

    def setUp(self):
        fixtures.CopyIncreaseTests.setUp(self)
        self.client=Mock()
        self.client.get.return_value={'marginSummary':{'accountValue':'10000'}}
        self.paper=SourceCopyPaper(self.observer.db,self.client,'0x'+'1'*40)

    def test_deployment_includes_the_source_copy_module(self):
        self.assertIn('research/trader_review/source_copy_paper.py',
                      Path('Dockerfile.finmonster').read_text())

    def test_source_sizes_unclipped_even_below_minimum_or_above_old_caps(self):
        self.event('a',qty=.001);self.step()
        self.assertAlmostEqual(self.position()['qty'],.0001)
        self.now+=1000;self.event('b',qty=10000);self.step()
        self.assertAlmostEqual(self.position()['qty'],1000.0001)
        self.assertEqual(self.client.get.call_count,1)
        self.assertEqual(self.position()['stop'],0)
        self.market.rules=Mock(side_effect=AssertionError('No virtual minimums'))
        self.now+=1000;self.event('c',qty=1);self.step()
        self.assertAlmostEqual(self.position()['qty'],1000.1001)

    def test_no_stop_daily_loss_drawdown_or_price_drift_exit(self):
        self.event('a');self.step();qty=self.position()['qty']
        self.now+=1000;self.market.bid=20;self.market.ask=20.02
        self.step()
        self.assertEqual(self.position()['qty'],qty)
        self.now+=1000;self.event('b',qty=100);self.step()
        self.assertEqual(self.position()['qty'],qty*2)
        self.now+=1000;self.event('c','CLOSE','SELL',100);self.step()
        self.assertEqual(self.position()['qty'],qty)
        self.now+=1000;self.event('d','CLOSE','SELL',100);self.step()
        self.assertEqual(self.paper.state()['broker']['positions'],{})
        trades=[json.loads(r[0]) for r in self.observer.db.execute(
            "SELECT payload FROM paper_journal WHERE kind='trade'")]
        self.assertTrue(all(t['reason']=='source_close' for t in trades))
        self.assertAlmostEqual(self.paper.state()['broker']['cash']-1000,
                               sum(t['net'] for t in trades))

    def test_migrate_preserves_history_position_and_cash_then_source_close(self):
        self.event('a');self.step();old=self.paper.state();old['policy']=PREVIOUS_POLICY
        old.pop('account_copy_ratio');old.pop('copy_cutover_ms')
        old['broker']['positions']['AGT-USDT']['stop']=99
        with self.observer.db:self.observer.db.execute(
            "UPDATE meta SET value=? WHERE key='paper_state'",(canonical(old),))
        self.now+=1000;self.market.bid=90;self.market.ask=90.02;self.step()
        self.assertEqual(self.paper.state()['broker']['cash'],old['broker']['cash'])
        self.assertEqual(self.position(),old['broker']['positions']['AGT-USDT'])
        self.assertEqual(self.observer.db.execute('SELECT count(*) FROM paper_orders').fetchone()[0],1)
        self.now+=1000;self.event('b','CLOSE','SELL',100);self.step()
        self.assertEqual(self.paper.state()['broker']['positions'],{})

    def test_baseline_not_copied_and_restart_does_not_replay(self):
        self.event('historical')
        with self.observer.db:self.observer.db.execute("UPDATE observations SET category='baseline'")
        self.step();self.assertFalse(self.paper.state()['broker']['positions'])
        self.now+=1000;self.event('new');self.step();qty=self.position()['qty']
        self.paper=SourceCopyPaper(self.observer.db,self.client,'0x'+'1'*40)
        self.step();self.assertEqual(self.position()['qty'],qty)
        self.assertEqual(self.client.get.call_count,1)

    def test_stale_quote_rolls_back_and_retries_same_fill(self):
        self.event('a');self.market.stamp=1
        with self.assertRaises(ValueError):self.paper.step(self.market,self.now)
        self.assertIsNone(self.paper.state())
        self.assertEqual(self.observer.db.execute('SELECT count(*) FROM paper_orders').fetchone()[0],0)
        self.step();self.assertEqual(self.position()['qty'],10)

    def test_source_outage_keeps_pending_fills(self):
        self.event('a')
        with self.assertRaises(ValueError):self.paper.step(self.market,self.now,allow_entries=False)
        self.assertEqual(self.observer.db.execute('SELECT count(*) FROM paper_orders').fetchone()[0],0)
        self.step();self.assertEqual(self.position()['qty'],10)

    def test_close_copies_execution_quantity_without_historical_position_entry(self):
        self.event('a',qty=10)
        row=json.loads(self.observer.db.execute('SELECT payload FROM observations').fetchone()[0])
        row['sourcePositionAfter']=100
        with self.observer.db:self.observer.db.execute('UPDATE observations SET payload=?',(canonical(row),))
        self.step();self.assertEqual(self.position()['qty'],1)
        self.now+=1000;self.event('b','CLOSE','SELL',5);self.step()
        self.assertEqual(self.position()['qty'],.5)
        self.now+=1000;self.event('c','CLOSE','SELL',95);self.step()
        self.assertFalse(self.paper.state()['broker']['positions'])

    def test_short_source_reversal_and_accounting_without_margin_loss_cap(self):
        self.event('a',side='SELL');self.step()
        self.now+=1000;self.market.bid=1000;self.market.ask=1000.02
        self.event('b','CLOSE','BUY',100);self.event('c','OPEN','BUY',50)
        self.step()
        self.assertEqual(self.position()['side'],1)
        self.assertEqual(self.position()['qty'],5)
        trade=json.loads(self.observer.db.execute(
            "SELECT payload FROM paper_journal WHERE kind='trade'").fetchone()[0])
        self.assertEqual(trade['reason'],'source_close')
        self.assertLess(trade['gross'],-trade['margin'])

if __name__=='__main__':unittest.main()
