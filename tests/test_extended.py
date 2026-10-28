import unittest
from dataclasses import replace
import math
import numpy as np
from bingx_bot.config import BAR, Config, SYMBOLS
from bingx_bot.data import Candle, load_dataset
from bingx_bot.strategy import Signal
from bingx_bot.engine import backtest
from research.extended.sweep import specs,bank,decisions,replay_factory,passed,score
from test_core import START,R

class ExtendedTests(unittest.TestCase):
    def rows(self,n):
        return [Candle(START+i*BAR,100+i*.002,101+i*.002,99+i*.002,100+i*.002+.4*math.sin(i/9),10) for i in range(n)]

    def test_finite_space_unique_and_counted(self):
        s=specs();self.assertEqual(len(s),256)
        self.assertEqual(len({str(x) for x in s}),256)
        self.assertEqual({x['family'] for x in s},{'donchian','ewmac','pullback','range','volatility','trend_reversion'})

    def test_features_causal_under_appended_future(self):
        rows=self.rows(1900);a=bank(rows[:1500]);b=bank(rows)
        for key in a:
            if a[key].dtype.kind in 'fiu':np.testing.assert_allclose(a[key],b[key][:1500],equal_nan=True,rtol=1e-11,atol=1e-11)
            else:np.testing.assert_equal(a[key],b[key][:1500])

    def test_all_family_signals_prefix_invariant(self):
        rows=self.rows(1800);a=bank(rows[:1500]);b=bank(rows)
        for family in ('donchian','ewmac','pullback','range','volatility','trend_reversion'):
            s=next(x for x in specs() if x['family']==family)
            left,right=decisions(a,s),decisions(b,s)
            for i in range(4):np.testing.assert_array_equal(left[i],right[i][:1500])

    def test_hour_feature_unavailable_until_close(self):
        rows=self.rows(8);features=bank(rows)
        self.assertTrue(np.isnan(features['atr'][2]))
        self.assertTrue(np.isfinite(features['atr'][3]))
        self.assertEqual(features['atr'][3],features['atr'][4])

    def test_replay_preserves_next_open(self):
        n=8;rows=self.rows(n)
        arrays={s:(np.array([0,1,0,0,0,0,0,0]),np.full(n,10),np.zeros(n,bool),np.zeros(n,bool),None,None) for s in SYMBOLS}
        b,_=backtest({s:rows for s in SYMBOLS},{s:[] for s in SYMBOLS},{s:R for s in SYMBOLS},strategy_factory=replay_factory(arrays))
        self.assertEqual(b.trades[0]['opened'],rows[2].ts)

    def test_training_gate_rejects_lucky_one_trade_and_halted_profit(self):
        m={'net_usdt':10,'completed_trades':1,'profit_factor':10,'risk_halted':False}
        self.assertFalse(passed(m));m['completed_trades']=100;m['risk_halted']=True;self.assertFalse(passed(m))

if __name__=='__main__':unittest.main()
