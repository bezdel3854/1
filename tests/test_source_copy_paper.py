import json
import unittest
from pathlib import Path
from unittest.mock import Mock

from research.trader_review.source_copy_paper import SourceCopyPaper, PREVIOUS_POLICY, BINGX_POLICY
from research.trader_review.finmonster_feed import canonical
from tests import test_copy_increases as fixtures


class SourceOnlyTests(unittest.TestCase):
    tearDown = fixtures.CopyIncreaseTests.tearDown
    position = fixtures.CopyIncreaseTests.position

    def setUp(self):
        fixtures.CopyIncreaseTests.setUp(self)
        self.client=Mock()
        self.mark = 100
        self.funding = []
        self.client.get.side_effect = lambda kind, **kwargs: {
            'clearinghouseState': {'marginSummary': {'accountValue':'10000'}},
            'metaAndAssetCtxs': [{'universe':[{'name':'AGT'},{'name':'LIT'}]},
                                 [{'markPx':self.mark},{'markPx':20}]],
            'userFunding': self.funding,
        }[kind]
        self.observer.db.execute('CREATE TABLE hl_fills(id TEXT PRIMARY KEY,time INTEGER,payload TEXT)')
        self.paper=SourceCopyPaper(self.observer.db,self.client,'0x'+'1'*40)

    def step(self):
        return self.paper.step(self.now)

    def event(self, identifier, action='OPEN', side='BUY', qty=100, stamp=None,
              price=100, fee=0, coin='AGT', position='p', raw_id=None):
        stamp = self.now-1 if stamp is None else stamp
        fid = raw_id or f'{coin}:{stamp}:{identifier}'
        oid = f'hl:{fid}:{action.lower()}'
        order = dict(orderId=oid,positionId=position,symbol=coin+'USDT',action=action,
                     side=side,quantity=qty,price=price,status='Filled',updatedAt=stamp)
        with self.observer.db:
            self.observer.db.execute('INSERT OR IGNORE INTO hl_fills VALUES(?,?,?)',
                (fid,stamp,canonical(dict(px=price,sz=qty,fee=fee,feeToken='USDC'))))
            self.observer.db.execute('INSERT INTO observations VALUES(?,?,?,?,?,?)',
                (oid,oid,stamp,self.now,'observed',canonical(order)))

    def test_deployment_includes_the_source_copy_module(self):
        self.assertIn('research/trader_review/source_copy_paper.py',
                      Path('Dockerfile.finmonster').read_text())

    def test_source_sizes_unclipped_even_below_minimum_or_above_old_caps(self):
        self.event('a',qty=.001);self.step()
        self.assertAlmostEqual(self.position()['qty'],.0001)
        self.now+=1000;self.event('b',qty=10000);self.step()
        self.assertAlmostEqual(self.position()['qty'],1000.0001)
        self.assertEqual(sum(x.args[0]=='clearinghouseState' for x in self.client.get.call_args_list),1)
        self.assertEqual(self.position()['stop'],0)
        self.now+=1000;self.event('c',qty=1);self.step()
        self.assertAlmostEqual(self.position()['qty'],1000.1001)

    def test_no_stop_daily_loss_drawdown_or_price_drift_exit(self):
        self.event('a');self.step();qty=self.position()['qty']
        self.now+=1000;self.mark=20
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
        self.now+=1000;self.mark=90;self.step()
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
        self.assertEqual(sum(x.args[0]=='clearinghouseState' for x in self.client.get.call_args_list),1)

    def test_mark_failure_does_not_discard_execution(self):
        self.event('lit',coin='LIT',price=19,fee=.5)
        get = self.client.get.side_effect
        def no_marks(kind, **kwargs):
            if kind == 'metaAndAssetCtxs':
                raise OSError('Mark service temporarily unavailable')
            return get(kind, **kwargs)
        self.client.get.side_effect = no_marks
        self.assertIsNone(self.step()['equity'])
        self.assertEqual(self.paper.state()['broker']['positions']['LIT-USDT']['entry'],19)
        self.client.get.side_effect = get
        self.assertAlmostEqual(self.step()['equity'],1009.95)
        self.assertEqual(self.step()['source_decisions'],0)

    def test_pending_fills_and_funding_survive_v4_migration(self):
        self.event('a',coin='LIT',price=20,fee=1)
        self.step()
        old = self.paper.state()
        old['policy'] = BINGX_POLICY
        old.pop('funding_through_ms')
        with self.observer.db:
            self.observer.db.execute("UPDATE meta SET value=? WHERE key='paper_state'",(canonical(old),))
        self.funding.append({'time':self.now+100,'delta':{'coin':'LIT','szi':'100','usdc':'-1'}})
        self.event('z','CLOSE','SELL',100,stamp=self.now+200,coin='LIT',price=22,fee=2)
        self.now+=1000
        self.assertAlmostEqual(self.step()['cash'],1019.6)
        self.assertEqual(self.step()['source_decisions'],0)
        trade=json.loads(self.observer.db.execute(
            "SELECT payload FROM paper_journal WHERE kind='trade'").fetchone()[0])
        self.assertEqual((trade['exit'],trade['exit_fee'],trade['funding']), (22,.2,.1))
        self.assertEqual(trade['closed'],self.now-800)

    def test_reversal_preserves_inserted_order_and_splits_raw_fill_fee(self):
        self.event('first',price=100)
        self.step()
        stamp=self.now+100
        raw_id=f'AGT:{stamp}:999'
        self.event('z','CLOSE','SELL',100,stamp=stamp,price=102,raw_id=raw_id)
        self.event('a','OPEN','SELL',50,stamp=stamp,price=102,position='short',raw_id=raw_id)
        with self.observer.db:
            self.observer.db.execute('UPDATE hl_fills SET payload=? WHERE id=?',
                (canonical(dict(px=102,sz=150,fee=3,feeToken='USDC')),raw_id))
        self.now+=1000
        self.step()
        self.assertEqual(self.position()['side'],-1)
        self.assertAlmostEqual(self.position()['entry_fee'],.1)
        trade=json.loads(self.observer.db.execute(
            "SELECT payload FROM paper_journal WHERE kind='trade'").fetchone()[0])
        self.assertAlmostEqual(trade['exit_fee'],.2)

    def test_source_outage_keeps_pending_fills(self):
        self.event('a')
        with self.assertRaises(ValueError):self.paper.step(self.now,allow_entries=False)
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
        self.now+=1000;self.mark=1000
        self.event('b','CLOSE','BUY',100,price=1000);self.event('c','OPEN','BUY',50,price=1000)
        self.step()
        self.assertEqual(self.position()['side'],1)
        self.assertEqual(self.position()['qty'],5)
        trade=json.loads(self.observer.db.execute(
            "SELECT payload FROM paper_journal WHERE kind='trade'").fetchone()[0])
        self.assertEqual(trade['reason'],'source_close')
        self.assertLess(trade['gross'],-trade['margin'])

if __name__=='__main__':unittest.main()
