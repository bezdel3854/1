import json
import sqlite3
import unittest
import io
from unittest.mock import patch
from research.trader_review.late_entry_probe import DAY_MS, RULES, initialize, sample, quote


class ProbeTests(unittest.TestCase):
    def test_live_nested_snake_case_book_ticker(self):
        body={'code':0,'data':{'book_ticker':{'symbol':'HYPE-USDT','bid_price':'65.936',
            'ask_price':'65.937','bid_qty':'200.959','ask_qty':'0.2321','lastUpdateId':1795163800000}}}
        with patch('research.trader_review.late_entry_probe.urlopen',return_value=io.BytesIO(json.dumps(body).encode())):
            self.assertEqual(quote('HYPE-USDT'),(1795163800000,65.936,65.937,200.959,.2321))
    def setUp(self):
        self.s=sqlite3.connect(':memory:');self.o=sqlite3.connect(':memory:')
        self.s.executescript('CREATE TABLE observations(order_id TEXT,payload TEXT,category TEXT);'
                            'CREATE TABLE hl_fills(id TEXT,payload TEXT);'
                            'CREATE TABLE polls(observed_ms INTEGER);INSERT INTO polls VALUES (1000);')
    def tearDown(self):self.s.close();self.o.close()
    def test_restart_keeps_original_window_and_rules(self):
        a=initialize(self.o,self.s,1000);b=initialize(self.o,self.s,9999)
        self.assertEqual(a,b);self.assertEqual(a['end_ms'],1000+3*DAY_MS)
        self.assertFalse(RULES['real_orders_enabled'])
    def test_records_new_events_quotes_and_health_without_source_writes(self):
        state=initialize(self.o,self.s,1000)
        event={'orderId':'hl:HYPE:1100:1:open','symbol':'HYPEUSDT','updatedAt':1100,'action':'OPEN'}
        self.s.execute('INSERT INTO observations VALUES (?,?,?)',(event['orderId'],json.dumps(event),'observed'))
        self.s.execute('INSERT INTO hl_fills VALUES (?,?)',('HYPE:1100:1','{}'))
        changes=self.s.total_changes
        self.assertTrue(sample(self.o,self.s,state,1200,lambda _: (1200,10,11,1,1)))
        self.assertEqual(self.s.total_changes,changes)
        self.assertEqual(self.o.execute('SELECT count(*) FROM source_events').fetchone()[0],1)
        self.assertEqual(self.o.execute('SELECT count(*) FROM quotes').fetchone()[0],1)
    def test_quote_failure_is_recorded_and_end_stops_sampling(self):
        state=initialize(self.o,self.s,1000)
        def bad(_):raise TimeoutError()
        sample(self.o,self.s,state,1200,bad)
        self.assertEqual(self.o.execute('SELECT count(*) FROM errors').fetchone()[0],1)
        self.assertFalse(sample(self.o,self.s,state,state['end_ms'],bad))
        self.assertEqual(json.loads(self.o.execute("SELECT value FROM meta WHERE key='status'").fetchone()[0]),'complete')
