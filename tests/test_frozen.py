import json
from pathlib import Path
import unittest
import numpy as np
from bingx_bot.frozen_candidate import FrozenCandidate,SPEC
from bingx_bot.approaches import factory,runtime_config
from bingx_bot.data import load_dataset
from bingx_bot.config import SYMBOLS,BAR
from bingx_bot.engine import backtest
from research.extended.sweep import bank,decisions

class FrozenTests(unittest.TestCase):
    def test_streaming_matches_frozen_vector_reference(self):
        cs,fs,rs,_=load_dataset('data/history')
        for symbol in SYMBOLS:
            refs=decisions(bank(cs[symbol]),SPEC)
            model=FrozenCandidate(runtime_config('candidate_120h'))
            for i,c in enumerate(cs[symbol]):
                signal=model.update(c)
                self.assertEqual(signal.side,int(refs[0][i]),(symbol,i))
                if i>=1200:
                    self.assertEqual(signal.close_long,bool(refs[2][i]))
                    self.assertEqual(signal.close_short,bool(refs[3][i]))
                    self.assertAlmostEqual(signal.distance,float(refs[1][i]),places=8)
                    self.assertAlmostEqual(signal.trail_long,float(refs[4][i]),places=8)

    def test_canonical_paper_model_reproduces_venue_backtest(self):
        cs,fs,rs,_=load_dataset('data/history')
        begin=cs[SYMBOLS[0]][0].ts+14*86_400_000
        finish=cs[SYMBOLS[0]][-1].ts+BAR
        b,_=backtest(cs,fs,rs,runtime_config('candidate_120h'),begin,finish,factory('candidate_120h'))
        ref=json.loads(Path('reports/extended/results.json').read_text())['finalist']['bingx_development']
        self.assertAlmostEqual(b.cash,ref['final_usdt'],places=7)
        self.assertEqual(len(b.trades),ref['completed_trades'])

if __name__=='__main__':unittest.main()
