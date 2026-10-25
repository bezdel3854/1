import json
import math
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from bingx_bot.config import BAR, Config, SYMBOLS
from bingx_bot.data import Candle, Rules, load_dataset
from bingx_bot.strategy import Signal
from bingx_bot.approaches import NAMES, Context, factory, runtime_config
from bingx_bot.engine import Broker, apply_trail, backtest
from bingx_bot.experiments import block_bootstrap, correlation, daily_returns, gate
from bingx_bot.paper import PaperWorker
from bingx_bot.store import Store, existing_config
from test_core import START, FakeClient, R, bar, entered

class ApproachTests(unittest.TestCase):
    def test_completed_hour_channel_excludes_just_closed_hour(self):
        c=Context()
        for i in range(8):f=c.update(bar(i))
        self.assertEqual(len(c.hours),2)
        self.assertEqual(len(f['hours']),1)
        self.assertLess(f['hours'][-1].ts,START+4*BAR)

    def test_every_model_is_prefix_invariant(self):
        rows=[bar(i,100+math.sin(i/12)*3+i*.002) for i in range(600)]
        for name in NAMES:
            with self.subTest(name=name):
                def calculate(xs):
                    model=factory(name)(runtime_config(name));return [model.update(c) for c in xs]
                self.assertEqual(calculate(rows[:490]),calculate(rows)[:490])

    def test_no_signals_before_hourly_warmup(self):
        for name in NAMES[1:]:
            model=factory(name)(runtime_config(name))
            for i in range(399):self.assertEqual(model.update(bar(i,100+i*.01)).side,0)

    def test_efficiency_regime_detects_trend(self):
        c=Context()
        for i in range(440):f=c.update(bar(i,100+i*.1))
        self.assertAlmostEqual(f['er'],1)

    def test_trailing_never_relaxes_long_or_short(self):
        for side in [1,-1]:
            b=entered(side);p=b.positions['BTC-USDT'];before=p.stop
            apply_trail(b,'BTC-USDT',Signal(trail_long=before-10,trail_short=before+10))
            self.assertEqual(p.stop,before)
            apply_trail(b,'BTC-USDT',Signal(trail_long=before+1,trail_short=before-1))
            self.assertEqual(p.stop,before+side)

    def test_invalid_trailing_rejected(self):
        with self.assertRaises(ValueError):apply_trail(entered(),'BTC-USDT',Signal(trail_long=math.nan))

    def test_trailing_activates_next_bar_and_gap_is_not_filled_at_stop(self):
        rows=[bar(0),bar(1),Candle(START+2*BAR,100,110,99.9,109,1000),bar(3,107),bar(4,107)]
        class Fixed:
            def __init__(self,c):self.i=0
            def update(self,c):
                self.i+=1
                if self.i==2:return Signal(1,10)
                if self.i==3:return Signal(trail_long=108)
                return Signal()
        b,_=backtest({s:rows for s in SYMBOLS},{s:[] for s in SYMBOLS},{s:R for s in SYMBOLS},strategy_factory=Fixed)
        t=b.trades[0]
        self.assertEqual(t['closed'],rows[3].ts)
        self.assertAlmostEqual(t['exit'],107*(1-Config().slip))
        self.assertEqual(t['reason'],'stop_gap')

    def test_extra_latency_delays_entry_exactly_one_bar(self):
        class Fixed:
            def __init__(self,c):self.i=0
            def update(self,c):
                self.i+=1
                return Signal(1,10) if self.i==2 else Signal()
        rows=[bar(i,100+i) for i in range(8)]
        args=({s:rows for s in SYMBOLS},{s:[] for s in SYMBOLS},{s:R for s in SYMBOLS})
        a,_=backtest(*args,strategy_factory=Fixed)
        b,_=backtest(*args,strategy_factory=Fixed,entry_delay=1)
        self.assertEqual(b.trades[0]['opened']-a.trades[0]['opened'],BAR)

    def test_strategy_exit_executes_next_open(self):
        class Fixed:
            def __init__(self,c):self.i=0
            def update(self,c):
                self.i+=1
                if self.i==2:return Signal(1,10)
                return Signal(close_long=self.i==3)
        rows=[bar(i,100+i) for i in range(8)]
        b,_=backtest({s:rows for s in SYMBOLS},{s:[] for s in SYMBOLS},{s:R for s in SYMBOLS},strategy_factory=Fixed)
        self.assertEqual(b.trades[0]['closed'],rows[3].ts)
        self.assertEqual(b.trades[0]['reason'],'strategy_exit')

    def test_bootstrap_reproducible_and_zero_has_no_edge(self):
        a=block_bootstrap([0]*82);b=block_bootstrap([0]*82)
        self.assertEqual(a,b)
        self.assertEqual(a['lower_return_pct'],0)
        self.assertLess(block_bootstrap([-.001]*82)['lower_return_pct'],0)

    def test_no_trades_does_not_pass_gate(self):
        runs={k:{'risk_halted':False,'net_usdt':0,'completed_trades':0,'profit_factor':None} for k in ['full','early','late','late_cost_stress']}
        self.assertFalse(gate(runs,{'lower_return_pct':0})['eligible_experimental_paper'])

    def test_daily_mark_end_at_midnight_belongs_to_previous_day(self):
        d=86_400_000
        self.assertEqual(daily_returns([{'ts':d,'equity':1010},{'ts':2*d,'equity':999.9}]),[.010000000000000009,-.010000000000000009])

    def test_correlated_votes_are_measured_as_correlated(self):
        self.assertAlmostEqual(correlation([1,2,3],[2,4,6]),1)
        self.assertIsNone(correlation([0,0],[1,1]))

    def test_baseline_regression_on_saved_dataset(self):
        cs,fs,rs,_=load_dataset('data/history')
        start=cs[SYMBOLS[0]][0].ts+7*86_400_000
        b,_=backtest(cs,fs,rs,start=start)
        reference=json.loads(Path('reports/validation/results.json').read_text())['runs']['full']
        self.assertAlmostEqual(b.cash,reference['final_usdt'],places=9)
        self.assertEqual(len(b.trades),reference['completed_trades'])

    def test_experimental_defaults_observation_only(self):
        with tempfile.TemporaryDirectory() as d:
            store=Store(Path(d)/'p.sqlite',runtime_config('regime_composite'))
            class Fixed:
                def update(self,c):return Signal(1,2)
            with patch('bingx_bot.paper.factory',return_value=lambda c:Fixed()):
                worker=PaperWorker(store,FakeClient(),'regime_composite')
                status=worker.cycle()
            self.assertTrue(status['observe_only'])
            self.assertFalse(store.broker.positions)
            self.assertEqual(store.db.execute("SELECT count(*) FROM journal WHERE kind='decision'").fetchone()[0],2)
            store.close()

    def test_cannot_switch_model_on_same_account(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'p.sqlite';cfg=runtime_config('ewmac');store=Store(path,cfg)
            PaperWorker(store,FakeClient(),'ewmac');store.commit();store.close()
            store=Store(path,cfg)
            with self.assertRaises(ValueError):PaperWorker(store,FakeClient(),'donchian')
            store.close()

    def test_export_config_recovered_for_nonbaseline_account(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'p.sqlite';cfg=runtime_config('ewmac');store=Store(path,cfg);store.commit();store.close()
            self.assertEqual(existing_config(path),cfg)
            with self.assertRaises(Exception):existing_config(Path(d)/'absent.sqlite')

if __name__=='__main__':unittest.main()
