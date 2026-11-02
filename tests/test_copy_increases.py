import json
import tempfile
import unittest
from copy import deepcopy
from unittest.mock import patch

from research.trader_review.finmonster_feed import Observer, canonical
from research.trader_review.finmonster_paper import Paper, LEGACY_POLICY, POLICY
from tests import test_finmonster_paper as fixtures


class CopyIncreaseTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.path=self.folder.name+'/paper.sqlite'
        self.observer=Observer(self.path);self.paper=Paper(self.observer.db);self.market=fixtures.FakeMarket()
        self.now=1000000
    def tearDown(self):
        self.observer.db.close();self.folder.cleanup()
    def event(self,identifier,action='OPEN',side='BUY',qty=100,stamp=None):
        stamp=self.now-1 if stamp is None else stamp
        body=dict(orderId=identifier,positionId='p',symbol='AGTUSDT',action=action,
                  side=side,quantity=qty,price=100,status='Filled',updatedAt=stamp)
        with self.observer.db:
            self.observer.db.execute('INSERT INTO observations VALUES(?,?,?,?,?,?)',
                (identifier,identifier,stamp,self.now,'observed',canonical(body)))
    def step(self):
        self.market.stamp=self.now
        return self.paper.step(self.market,self.now)
    def position(self):return self.paper.state()['broker']['positions']['AGT-USDT']
    def test_seven_proportional_increases_then_half_and_full_close_survive_restart(self):
        self.event('initial');self.step();first=self.position()['qty'];ratio=first/100
        for i in range(7):
            self.now+=1000;self.event('add'+str(i),qty=10);self.step()
        state=self.paper.state();total=self.position()['qty']
        self.assertAlmostEqual(total,first+7*.001*int(10*ratio/.001),places=8)
        decisions=[json.loads(r[0]) for r in self.observer.db.execute('SELECT result FROM paper_orders')]
        self.assertEqual(sum(x['result']=='increase' for x in decisions),7)
        self.assertEqual(state['tracked']['AGT-USDT']['source_remaining'],170)
        self.observer.db.close();self.observer=Observer(self.path);self.paper=Paper(self.observer.db)
        self.now+=1000;self.event('half','CLOSE','SELL',85);self.step()
        self.assertAlmostEqual(self.position()['qty'],total/2)
        self.assertEqual(self.paper.state()['tracked']['AGT-USDT']['source_remaining'],85)
        self.now+=1000;self.event('full','CLOSE','SELL',85);result=self.step()
        self.assertEqual(result['positions'],0)
        trades=[json.loads(r[0]) for r in self.observer.db.execute("SELECT payload FROM paper_journal WHERE kind='trade'")]
        self.assertEqual(len(trades),2)
        self.assertAlmostEqual(sum(t['qty'] for t in trades),total)
        self.assertAlmostEqual(result['cash']-1000,sum(t['net_after_share'] for t in trades))
        self.assertEqual(self.step()['source_decisions'],0)
    def test_short_addition_preserves_weighted_entry_fees_and_common_stop(self):
        self.event('initial',side='SELL');self.step();before=deepcopy(self.position())
        self.now+=1000;self.market.bid=99.8;self.market.ask=99.82
        self.event('add',side='SELL',qty=20);self.step();after=self.position()
        added=after['qty']-before['qty'];fill=99.8*.9998
        self.assertGreater(added,0)
        self.assertAlmostEqual(after['entry'],(before['qty']*before['entry']+added*fill)/after['qty'])
        self.assertAlmostEqual(after['entry_fee'],before['entry_fee']+added*fill*.0005)
        self.assertEqual(after['stop'],before['stop'])
        self.now+=1000;self.event('close','CLOSE','BUY',120);result=self.step()
        self.assertEqual(result['positions'],0)
        trade=json.loads(self.observer.db.execute("SELECT payload FROM paper_journal WHERE kind='trade'").fetchone()[0])
        self.assertAlmostEqual(result['cash']-1000,trade['net_after_share'])
    def test_huge_increase_is_clipped_to_shared_budget_and_records_clipping(self):
        self.event('initial');self.step()
        self.now+=1000;self.event('huge',qty=10000);self.step()
        state=self.paper.state();budget=sum(p['budget'] for p in state['broker']['positions'].values())
        self.assertLessEqual(budget,10)
        row=json.loads(self.observer.db.execute("SELECT result FROM paper_orders WHERE order_id='huge'").fetchone()[0])
        self.assertEqual(row['result'],'increase');self.assertTrue(row['limited_by_risk_or_size'])
        self.assertLess(row['filled_paper_qty'],row['requested_paper_qty'])
        self.now+=1000;self.event('again',qty=10000);self.step()
        state=self.paper.state();self.assertLessEqual(sum(p['budget'] for p in state['broker']['positions'].values()),10)
    def test_rejected_stale_addition_still_updates_source_close_denominator(self):
        self.event('initial');self.step();qty=self.position()['qty']
        self.now+=1000;self.event('stale',qty=100,stamp=1);self.step()
        self.assertEqual(self.position()['qty'],qty)
        self.assertEqual(self.paper.state()['tracked']['AGT-USDT']['source_remaining'],200)
        self.now+=1000;self.event('close-half','CLOSE','SELL',100);self.step()
        self.assertAlmostEqual(self.position()['qty'],qty/2)
    def test_legacy_policy_migrates_once_without_replaying_or_resetting_account(self):
        self.event('initial');self.step();old=self.paper.state();old['policy']=LEGACY_POLICY
        old['tracked']['AGT-USDT'].pop('copy_ratio')
        with self.observer.db:self.observer.db.execute("UPDATE meta SET value=? WHERE key='paper_state'",(canonical(old),))
        before=deepcopy(old['broker']);self.now+=1000;self.step();new=self.paper.state()
        self.assertEqual(new['policy'],POLICY)
        self.assertEqual(new['broker']['cash'],before['cash'])
        self.assertEqual(new['broker']['positions'],before['positions'])
        self.assertEqual(self.observer.db.execute("SELECT count(*) FROM paper_orders").fetchone()[0],1)
        self.step()
        self.assertEqual(self.observer.db.execute("SELECT count(*) FROM paper_journal WHERE kind='policy_migration'").fetchone()[0],1)
    def test_stale_quote_rolls_back_addition_and_source_quantity_together(self):
        self.event('initial');self.step();before=self.paper.state()
        self.now+=1000;self.event('add',qty=10)
        with patch.object(self.market,'quote',return_value=(99.98,100.02,1)):
            with self.assertRaises(ValueError):self.step()
        self.assertEqual(self.paper.state(),before)
        self.assertEqual(self.observer.db.execute("SELECT count(*) FROM paper_orders WHERE order_id='add'").fetchone()[0],0)
