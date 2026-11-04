import json
import sqlite3
import unittest
from research.trader_review.finmonster_feed import Observer
from research.trader_review.hyperliquid_feed import FillObserver, PublicInfo, normalize

ADDR='0x'+'1'*40

def fill(t=1000,tid=1,start='0',sz='2',side='B',coin='BTC'):
    return dict(coin=coin,time=t,tid=tid,startPosition=start,sz=sz,side=side,px='100')

class Fake:
    def __init__(self,rows):self.rows=rows
    def get(self,*args,**kwargs):return self.rows

class HyperliquidTests(unittest.TestCase):
    def test_baseline_restart_and_duplicate(self):
        o=Observer(':memory:',ADDR,'test');api=Fake([fill()]);h=FillObserver(o.db,ADDR,api)
        h.poll(now=2000)
        self.assertEqual(o.db.execute('select category from observations').fetchone()[0],'baseline')
        api.rows=[fill(),fill(2500,2,'2','1')];h=FillObserver(o.db,ADDR,api);h.poll(now=3000);h.poll(now=3001)
        self.assertEqual(o.db.execute('select count(*) from observations').fetchone()[0],2)
        rows=[json.loads(r[0]) for r in o.db.execute('select payload from observations order by source_ms')]
        self.assertEqual(rows[0]['positionId'],rows[1]['positionId'])

    def test_partial_close_and_reverse(self):
        epochs={};a=normalize(fill(),epochs,2000)[0]
        b=normalize(fill(1100,2,'2','.5','A'),epochs,2000)[0]
        self.assertEqual(b['positionId'],a['positionId']);self.assertEqual(b['quantity'],.5)
        c=normalize(fill(1200,3,'1.5','3','A'),epochs,2000)
        self.assertEqual([x['action'] for x in c],['CLOSE','OPEN'])
        self.assertEqual([x['quantity'] for x in c],[1.5,1.5]);self.assertNotEqual(c[0]['positionId'],c[1]['positionId'])

    def test_gap_and_invalid_do_not_advance(self):
        o=Observer(':memory:',ADDR,'test');api=Fake([fill()]);h=FillObserver(o.db,ADDR,api);h.poll(now=2000)
        before=o.db.execute("select value from meta where key='hl_cursor'").fetchone()[0]
        api.rows=[fill(2500,2)]
        with self.assertRaisesRegex(ValueError,'gap'):h.poll(now=3000)
        self.assertEqual(before,o.db.execute("select value from meta where key='hl_cursor'").fetchone()[0])
        api.rows=[fill(),fill(2500,2,sz='nan')]
        with self.assertRaises(ValueError):h.poll(now=3000)
        self.assertEqual(o.db.execute('select count(*) from hl_fills').fetchone()[0],1)

    def test_late_historical_fill_stays_baseline(self):
        o=Observer(':memory:',ADDR,'test');api=Fake([fill()]);h=FillObserver(o.db,ADDR,api);h.poll(now=2000)
        api.rows=[fill(),fill(1500,2,start='2')];h.poll(now=3000)
        self.assertEqual(o.db.execute("select count(*) from observations where category='observed'").fetchone()[0],0)

    def test_no_order_api_and_bad_address(self):
        with self.assertRaises(ValueError):PublicInfo().get('order')
        with self.assertRaises(ValueError):FillObserver(sqlite3.connect(':memory:'),'bad')

    def test_future_and_unsupported(self):
        with self.assertRaises(ValueError):normalize(fill(9000),{},2000)
        self.assertEqual(normalize(fill(coin='xyz:TSLA'),{},2000),[])

    def test_empty_baseline_then_first_new_fill(self):
        o=Observer(':memory:',ADDR,'test');api=Fake([]);h=FillObserver(o.db,ADDR,api);h.poll(now=2000)
        api.rows=[fill(2500)];h.poll(now=3000)
        self.assertEqual(o.db.execute('select category from observations').fetchone()[0],'observed')

    def test_full_close_new_epoch(self):
        epochs={};a=normalize(fill(),epochs,2000)[0]
        normalize(fill(1100,2,'2','2','A'),epochs,2000)
        b=normalize(fill(1200,3),epochs,2000)[0]
        self.assertNotEqual(a['positionId'],b['positionId'])

if __name__=='__main__':unittest.main()
