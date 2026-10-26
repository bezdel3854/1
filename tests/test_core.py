import unittest
import math
import tempfile
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch
from bingx_bot.config import Config, BAR, SYMBOLS
from bingx_bot.data import Candle, Funding, Rules, validate, PublicClient, save_dataset, load_dataset
from bingx_bot.strategy import Strategy, Signal, Indicators
from bingx_bot.engine import Broker, bar_exit, backtest
from bingx_bot.store import Store
from bingx_bot.paper import PaperWorker

R=Rules('.0001',.0001,2)
START=1_780_012_800_000//3_600_000*3_600_000

def bar(i=0, price=100, high=None, low=None):
    return Candle(START+i*BAR,price,high or price*1.001,low or price*.999,price,1000)

def entered(side=1,config=Config()):
    b=Broker(config)
    assert b.enter('BTC-USDT',side,100,2,START,R,{'BTC-USDT':100,'ETH-USDT':100})
    return b

class RiskTests(unittest.TestCase):
    def test_budget_includes_fees_slip_funding(self):
        b=entered();p=b.positions['BTC-USDT']
        b.exit('BTC-USDT',p.stop,START+BAR,'stop')
        self.assertLessEqual(-b.trades[0]['net']+p.qty*p.entry*b.config.funding_reserve,5+1e-9)

    def test_short_stop_budget(self):
        b=entered(-1);p=b.positions['BTC-USDT']
        b.exit('BTC-USDT',p.stop,START+BAR,'stop')
        self.assertLessEqual(-b.trades[0]['net']+p.qty*p.entry*b.config.funding_reserve,5+1e-9)

    def test_quantity_rounds_down(self):
        self.assertEqual(R.floor(.123456),.1234)
        self.assertEqual(Rules('.005',0,0).floor(.019),.015)

    def test_margin_and_notional_cap(self):
        b=entered();p=b.positions['BTC-USDT']
        self.assertLessEqual(p.qty*p.entry,1000)
        self.assertLessEqual(p.margin,b.cash)

    def test_portfolio_risk_cap(self):
        b=entered();b.enter('ETH-USDT',1,100,2,START,R,{'BTC-USDT':100,'ETH-USDT':100})
        self.assertLessEqual(sum(p.budget for p in b.positions.values()),10)
        self.assertEqual(len(b.positions),2)

    def test_minimum_no_forced_oversize(self):
        b=Broker();self.assertFalse(b.enter('BTC-USDT',1,100,2,START,Rules('1',100,10000),{}))
        self.assertEqual(b.cash,1000)

    def test_daily_halt_latched_until_next_utc_day(self):
        b=Broker();b.risk_check(START,{})
        b.cash=979;self.assertTrue(b.risk_check(START+BAR,{}))
        b.cash=990;self.assertTrue(b.risk_check(START+2*BAR,{}))
        self.assertFalse(b.risk_check(START+86400000,{}))

    def test_drawdown_halt_survives_day_and_restore(self):
        b=Broker();b.risk_check(START,{})
        b.cash=899;b.risk_check(START+BAR,{})
        b=Broker.restore(b.snapshot(),b.config)
        b.cash=1000;self.assertTrue(b.risk_check(START+86400000,{}))

    def test_invalid_config_rejected(self):
        for kwargs in [{'risk':.02},{'deposit':math.nan},{'leverage':100},{'fee':-.1}]:
            with self.assertRaises(ValueError):Config(**kwargs)

    def test_funding_long_and_short_sign(self):
        for side in [1,-1]:
            b=entered(side);p=b.positions['BTC-USDT'];cash=b.cash
            b.fund('BTC-USDT',START+BAR,.001,100)
            self.assertAlmostEqual(b.cash-cash,-side*p.qty*.1)

    def test_new_entry_at_settlement_not_charged(self):
        b=entered();cash=b.cash;b.fund('BTC-USDT',START,.01,100)
        self.assertEqual(cash,b.cash)

    def test_accounting_reconciles(self):
        b=entered();b.fund('BTC-USDT',START+BAR,.001,100)
        b.exit('BTC-USDT',104,START+2*BAR,'test')
        self.assertAlmostEqual(b.cash-1000,b.trades[0]['net'])

    def test_both_hit_stop_first_long(self):
        b=entered();bar_exit(b,'BTC-USDT',bar(1,100,110,90))
        self.assertEqual(b.trades[0]['reason'],'stop_both')
        self.assertLess(b.trades[0]['net'],0)

    def test_both_hit_stop_first_short(self):
        b=entered(-1);bar_exit(b,'BTC-USDT',bar(1,100,110,90))
        self.assertEqual(b.trades[0]['reason'],'stop_both')

    def test_gap_fills_at_worse_open(self):
        b=entered();bar_exit(b,'BTC-USDT',bar(1,95,96,94))
        self.assertEqual(b.trades[0]['reason'],'stop_gap')
        self.assertLess(b.trades[0]['exit'],95)
        self.assertLess(b.trades[0]['net'],-5)

    def test_no_intrabar_cash_available_at_open(self):
        b=entered();bar_exit(b,'BTC-USDT',bar(1,100,110,90),'open')
        self.assertIn('BTC-USDT',b.positions)
        bar_exit(b,'BTC-USDT',bar(1,100,110,90),'intrabar')
        self.assertNotIn('BTC-USDT',b.positions)

    def test_isolated_gap_loss_cap(self):
        b=entered();margin=b.positions['BTC-USDT'].margin
        bar_exit(b,'BTC-USDT',bar(1,1,2,.5))
        self.assertLessEqual(-b.trades[0]['net'],margin+b.trades[0]['entry_fee']+1e-9)

class DataStrategyTests(unittest.TestCase):
    def test_flat_rsi(self):
        i=Indicators()
        for k in range(100):cross,rsi=i.update(bar(k))
        self.assertEqual(rsi,50)
        self.assertEqual(cross,0)

    def test_rising_falling_rsi(self):
        for side in [1,-1]:
            i=Indicators()
            for k in range(100):_,rsi=i.update(bar(k,200+side*k))
            self.assertEqual(rsi,100 if side==1 else 0)

    def test_hour_only_after_four_closed_candles(self):
        s=Strategy(Config())
        for i in range(3):s.update(bar(i))
        self.assertEqual(s.h1.count,0)
        s.update(bar(3));self.assertEqual(s.h1.count,1)
        s.update(bar(4));self.assertEqual(s.h1.count,1)

    def test_partial_start_hour_discarded(self):
        s=Strategy(Config())
        for i in range(1,8):s.update(bar(i))
        self.assertEqual(s.h1.count,1)

    def test_signal_prefix_invariance(self):
        def calculate(rows):
            s=Strategy(Config());return [s.update(c) for c in rows]
        rows=[bar(i,100+3*math.sin(i/8)+i*.001) for i in range(700)]
        self.assertEqual(calculate(rows[:500]),calculate(rows)[:500])

    def test_validation_gaps_nan_bad_ohlc(self):
        for rows in [[bar(0),bar(2)],[replace(bar(),close=math.nan)],[replace(bar(),high=1)]]:
            with self.assertRaises(ValueError):validate(rows)

    def test_checksum_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as t:
            candles={s:[bar(0),bar(1)] for s in SYMBOLS};fs={s:[] for s in SYMBOLS};rs={s:R for s in SYMBOLS}
            save_dataset(t,candles,fs,rs)
            self.assertEqual(load_dataset(t)[0],candles)
            p=Path(t)/'BTC-USDT.json';p.write_bytes(p.read_bytes()+b' ')
            with self.assertRaises(ValueError):load_dataset(t)

    def test_private_endpoint_rejected_before_network(self):
        with self.assertRaises(KeyError):PublicClient().get('/openApi/swap/v2/trade/order')

    def test_actual_bingx_book_schema(self):
        c=PublicClient()
        with patch.object(c,'get',return_value={'book_ticker':{'bid_price':100,'ask_price':101,'time':START}}):
            self.assertEqual(c.quote('BTC-USDT'),(100,101,START))

    def test_inactive_contract_fails(self):
        c=PublicClient()
        with patch.object(c,'get',return_value=[{'symbol':'BTC-USDT','status':0,'apiStateOpen':'true','apiStateClose':'true'}]):
            with self.assertRaises(ValueError):c.rules()

    def test_nonfinite_funding_mark_fails(self):
        c=PublicClient()
        with patch.object(c,'get',return_value=[{'fundingTime':START,'fundingRate':'.0001','markPrice':'nan'}]):
            with self.assertRaises(ValueError):c.funding('BTC-USDT',START,START+BAR)

    def test_history_empty_fails(self):
        c=PublicClient()
        with patch.object(c,'get',return_value=[]):
            with self.assertRaises(ValueError):c.candles('BTC-USDT',START,START+BAR)

    def test_funding_missing_fails(self):
        c=PublicClient()
        with patch.object(c,'get',return_value=[]):
            with self.assertRaises(ValueError):c.funding('BTC-USDT',START,START+86400000)

    def test_narrow_funding_window_fetches_verified_day_without_replaying_events(self):
        c=PublicClient();end=START+10000
        page=[{'fundingTime':START-i*8*3600000,'fundingRate':'.0001','markPrice':'100'} for i in range(3)]
        with patch.object(c,'get',return_value=page) as get:
            self.assertEqual(c.funding('BTC-USDT',START+1,end),[])
            self.assertEqual(get.call_args.kwargs['startTime'],end-86400000)
            self.assertEqual(get.call_args.kwargs['endTime'],end-1)
        with patch.object(c,'get',return_value=page):
            self.assertEqual(c.funding('BTC-USDT',START-1,end),[Funding(START,.0001,100)])

    def test_narrow_funding_window_rejects_unknown_or_sparse_history(self):
        c=PublicClient()
        for page in (None,[],[{'fundingTime':START,'fundingRate':'.0001','markPrice':'100'}]):
            with self.subTest(page=page),patch.object(c,'get',return_value=page):
                with self.assertRaises(ValueError):c.funding('BTC-USDT',START+1,START+10000)

    def test_backtest_next_open_and_no_future_signal(self):
        rows=[bar(i,100+i) for i in range(10)]
        class Fixed:
            def __init__(self,c):self.i=0
            def update(self,c):
                self.i+=1
                return Signal(1,20) if self.i==2 else Signal()
        with patch('bingx_bot.engine.Strategy',Fixed):
            b,eq=backtest({s:rows for s in SYMBOLS},{s:[] for s in SYMBOLS},{s:R for s in SYMBOLS})
        self.assertTrue(b.trades)
        self.assertEqual(b.trades[0]['opened'],rows[2].ts)
        self.assertAlmostEqual(b.trades[0]['entry'],rows[2].open*(1+Config().slip))

    def test_future_candles_do_not_leak_through_test_end(self):
        rows=[bar(i,100+i*.01) for i in range(500)]
        args=lambda xs:({s:xs for s in SYMBOLS},{s:[] for s in SYMBOLS},{s:R for s in SYMBOLS})
        a,_=backtest(*args(rows),end=rows[450].ts)
        b,_=backtest(*args(rows[:450]))
        self.assertEqual(a.cash,b.cash)
        self.assertEqual(a.trades,b.trades)

class FakeClient:
    def __init__(self,now=None):self.clock=START+1000*BAR+10_000 if now is None else now;self.age=0
    def now(self):return self.clock
    def rules(self):return {s:R for s in SYMBOLS},[]
    def candles(self,s,start,end):return [bar((t-START)//BAR) for t in range(start,end,BAR)]
    def quote(self,s):return 99.99,100.01,self.clock-self.age
    def funding(self,s,start,end):return []

class StorePaperTests(unittest.TestCase):
    def test_atomic_restart_and_no_duplicate_journal(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'paper.sqlite';s=Store(p,Config());s.broker=entered();s.commit();s.close()
            s=Store(p,Config());self.assertEqual(len(s.broker.positions),1)
            s.commit();self.assertEqual(s.db.execute('SELECT count(*) FROM journal').fetchone()[0],1);s.close()

    def test_single_writer(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'p.sqlite';s=Store(p,Config())
            with self.assertRaises(RuntimeError):Store(p,Config())
            s.close()

    def test_config_change_blocked(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'p.sqlite';s=Store(p,Config());s.commit();s.close()
            with self.assertRaises(ValueError):Store(p,Config(fee=.001))

    def test_same_signal_not_duplicated_after_restart(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'p.sqlite';s=Store(path,Config());client=FakeClient()
            class Fixed:
                def __init__(self,c):pass
                def update(self,c):return Signal(1,2)
            with patch('bingx_bot.paper.Strategy',Fixed):
                PaperWorker(s,client).cycle()
                entries=s.db.execute("SELECT count(*) FROM journal WHERE kind='entry'").fetchone()[0]
                self.assertEqual(entries,2)
                s.close();s=Store(path,Config());PaperWorker(s,client).cycle()
                self.assertEqual(s.db.execute("SELECT count(*) FROM journal WHERE kind='entry'").fetchone()[0],2)
            s.close()

    def test_stale_quote_no_execution(self):
        with tempfile.TemporaryDirectory() as d:
            s=Store(Path(d)/'p.sqlite',Config());client=FakeClient();client.age=60000
            with self.assertRaises(ValueError):PaperWorker(s,client).cycle()
            self.assertFalse(s.broker.positions);s.close()

    def test_stop_file_flattens_and_blocks_entry(self):
        with tempfile.TemporaryDirectory() as d:
            s=Store(Path(d)/'p.sqlite',Config());s.broker=entered();Path(d,'STOP').touch()
            PaperWorker(s,FakeClient()).cycle()
            self.assertFalse(s.broker.positions)
            self.assertEqual(s.db.execute("SELECT count(*) FROM journal WHERE kind='exit'").fetchone()[0],1);s.close()

    def test_missed_signal_not_replayed(self):
        with tempfile.TemporaryDirectory() as d:
            s=Store(Path(d)/'p.sqlite',Config());client=FakeClient();client.clock+=120000
            class Fixed:
                def __init__(self,c):pass
                def update(self,c):return Signal(1,2)
            with patch('bingx_bot.paper.Strategy',Fixed):PaperWorker(s,client).cycle()
            self.assertFalse(s.broker.positions);s.close()

    def test_uncommitted_cash_not_recovered(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'p.sqlite';s=Store(path,Config());s.commit();s.broker.cash=50;s.close()
            s=Store(path,Config());self.assertEqual(s.broker.cash,1000);s.close()

if __name__=='__main__':unittest.main()
