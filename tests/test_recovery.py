"""Regression checks for gateway cooldown and recovery without request amplification."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from bingx_bot.config import BAR, SYMBOLS, Config
from bingx_bot.data import Candle, Rules, PublicClient, PublicAPIUnavailable
from bingx_bot.paper import PaperWorker, run
from bingx_bot.store import Store
from bingx_bot.strategy import Signal

BASE=1780012800000//BAR*BAR

class GatewayTests(unittest.TestCase):
    def test_partial_warmup_survives_outage_and_shifted_close_without_refetch(self):
        client=PublicClient();calls=[];failed=False
        def page(endpoint,**kw):
            nonlocal failed
            calls.append(kw['startTime'])
            if len(calls)==2 and not failed:
                failed=True;raise PublicAPIUnavailable('klines','limited',60)
            return [[t,100,100.1,99.9,100,1000] for t in range(kw['startTime'],kw['endTime']+1,BAR)]
        with patch.object(client,'get',side_effect=page):
            with self.assertRaises(PublicAPIUnavailable):client.candles('BTC-USDT',BASE,BASE+1001*BAR)
            rows=client.candles('BTC-USDT',BASE+BAR,BASE+1002*BAR)
        self.assertEqual(len(rows),1001)
        self.assertEqual(calls,[BASE,BASE+1000*BAR,BASE+1000*BAR])

    def test_invalid_candle_chunk_is_not_cached(self):
        client=PublicClient()
        with patch.object(client,'get',return_value=[[BASE,100,1,99,100,1]]):
            with self.assertRaises(ValueError):client.candles('BTC-USDT',BASE,BASE+BAR)
        self.assertFalse(client.candle_cache['BTC-USDT'])

    def test_business_rate_limit_sets_global_cooldown_without_immediate_retry(self):
        client=PublicClient()
        response=io.BytesIO(json.dumps({'code':100410,'data':None}).encode())
        with patch('bingx_bot.data.urlopen',return_value=response) as request,patch('bingx_bot.data.time.sleep'):
            with self.assertRaises(PublicAPIUnavailable) as err:client.get('time')
            self.assertEqual(err.exception.retry_after,60)
            with self.assertRaises(PublicAPIUnavailable):client.get('contracts')
            self.assertEqual(request.call_count,1)

    def test_http_retry_after_is_respected(self):
        client=PublicClient()
        error=HTTPError('https://open-api.bingx.com',429,'limited',{'Retry-After':'180'},None)
        with patch('bingx_bot.data.urlopen',side_effect=error) as request,patch('bingx_bot.data.time.sleep'):
            with self.assertRaises(PublicAPIUnavailable) as err:client.get('time')
            self.assertGreaterEqual(err.exception.retry_after,180)
            self.assertEqual(request.call_count,1)

    def test_network_exhaustion_is_recoverable_but_parameter_error_is_not(self):
        with patch('bingx_bot.data.urlopen',side_effect=URLError('offline')) as request,patch('bingx_bot.data.time.sleep'):
            with self.assertRaises(PublicAPIUnavailable):PublicClient().get('time')
            self.assertEqual(request.call_count,4)
        response=io.BytesIO(b'{"code":100400,"msg":"bad parameter","data":null}')
        with patch('bingx_bot.data.urlopen',return_value=response),patch('bingx_bot.data.time.sleep'):
            with self.assertRaises(ValueError):PublicClient().get('time')

class Client:
    def __init__(self):
        self.clock=BASE+1000*BAR+10000
        self.candle_reads=[]
        self.fail_eth=False
        self.quote_failures=0
    def now(self):return self.clock
    def rules(self):return {s:Rules('.0001',.0001,2) for s in SYMBOLS},[]
    def candles(self,s,start,end):
        self.candle_reads.append((s,start,end))
        if self.fail_eth and s=='ETH-USDT':raise PublicAPIUnavailable('klines','limited',60)
        return [Candle(t,100,100.1,99.9,100,10000) for t in range(start,end,BAR)]
    def quote(self,s):
        if self.quote_failures:
            self.quote_failures-=1
            raise PublicAPIUnavailable('book','limited',60)
        return 100,100.01,self.clock
    def funding(self,*args):return []

class RecoveryTests(unittest.TestCase):
    def test_partial_symbol_update_is_atomic_and_retry_matches_uninterrupted(self):
        class Counter:
            def __init__(self,c):self.n=0
            def update(self,c):self.n+=1;return Signal()
        with tempfile.TemporaryDirectory() as d,patch('bingx_bot.paper.Strategy',Counter):
            store=Store(Path(d)/'p.sqlite',Config());client=Client();worker=PaperWorker(store,client)
            worker.cycle();self.assertEqual(worker.strategies['BTC-USDT'].n,1000)
            previous=worker.strategy_ts;client.clock+=BAR;client.fail_eth=True
            with self.assertRaises(PublicAPIUnavailable):worker.cycle()
            self.assertEqual(worker.strategy_ts,previous)
            self.assertEqual(worker.strategies['BTC-USDT'].n,1000)
            self.assertEqual(worker.strategies['ETH-USDT'].n,1000)
            client.fail_eth=False;worker.cycle()
            self.assertEqual([worker.strategies[s].n for s in SYMBOLS],[1001,1001])
            self.assertEqual(store.broker.cash,1000);store.close()

    def test_more_than_five_outages_recover_without_rebuilding_client_or_warmup(self):
        class Stop:
            stopped=False
            def __init__(self):self.waits=[]
            def set(self):self.stopped=True
            def is_set(self):return self.stopped
            def wait(self,seconds):
                self.waits.append(seconds)
                if seconds==5:self.set()
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'p.sqlite';client=Client();client.quote_failures=7;stop=Stop()
            with patch('bingx_bot.paper.PublicClient',return_value=client) as constructor,patch('bingx_bot.paper.signal.signal'):
                run(path,stop_event=stop)
            self.assertEqual(constructor.call_count,1)
            self.assertEqual(len(client.candle_reads),2)
            self.assertTrue(all(60<=n<=300 for n in stop.waits[:-1]))
            self.assertEqual(len(stop.waits),8)
            store=Store(path,Config())
            self.assertEqual(store.db.execute("select count(*) from journal where kind='api_unavailable'").fetchone()[0],7)
            self.assertEqual(store.db.execute("select count(*) from journal where kind='api_recovered'").fetchone()[0],1)
            self.assertEqual(store.db.execute("select count(*) from journal where kind='heartbeat'").fetchone()[0],1)
            self.assertEqual(store.db.execute("select count(*) from journal where kind='entry'").fetchone()[0],0)
            self.assertEqual(store.broker.cash,1000);store.close()

    def test_once_mode_reports_outage_instead_of_waiting_forever(self):
        with tempfile.TemporaryDirectory() as d:
            client=Client();client.quote_failures=1
            with patch('bingx_bot.paper.PublicClient',return_value=client),patch('bingx_bot.paper.signal.signal'):
                with self.assertRaises(PublicAPIUnavailable):run(Path(d)/'p.sqlite',once=True)

if __name__=='__main__':unittest.main()
