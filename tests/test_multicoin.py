import tempfile
import unittest
from bingx_bot.config import Config, SYMBOLS, BAR
from bingx_bot.data import Candle, Rules, PublicClient, save_dataset, load_dataset
from bingx_bot.engine import Broker, backtest
from bingx_bot.strategy import Signal

class AlwaysLong:
    def __init__(self, config): pass
    def update(self, candle): return Signal(side=1, distance=2)

class MulticoinTests(unittest.TestCase):
    def setUp(self):
        self.symbols = tuple(f'COIN{i}-USDT' for i in range(30))
        self.candles = {s:[Candle(i*BAR,100,100.1,99.9,100,1000) for i in range(4)] for s in self.symbols}
        self.funding = {s:[] for s in self.symbols}
        self.rules = {s:Rules('.0001',.0001,2) for s in self.symbols}

    def test_thirty_simultaneous_signals_share_one_risk_budget(self):
        broker, equity = backtest(self.candles,self.funding,self.rules,
            symbols=self.symbols,strategy_factory=AlwaysLong)
        entries = [e for e in broker.events if e['kind']=='entry']
        self.assertGreater(len(entries),0)
        self.assertLessEqual(sum(e['budget'] for e in entries),10)
        self.assertEqual(len(equity),4)
        from bingx_bot.report import metrics
        self.assertEqual(set(metrics(broker,equity)["per_symbol"]),set(self.symbols))
        self.assertFalse(broker.positions)

    def test_all_thirty_histories_roundtrip_and_last_misalignment_fails(self):
        with tempfile.TemporaryDirectory() as root:
            save_dataset(root,self.candles,self.funding,self.rules,symbols=self.symbols)
            cs,fs,rs,_ = load_dataset(root,symbols=self.symbols)
            self.assertEqual(cs,self.candles)
            cs[self.symbols[-1]] = cs[self.symbols[-1]][1:]
            with self.assertRaisesRegex(ValueError,'Unaligned'):
                backtest(cs,fs,rs,symbols=self.symbols)

    def test_default_broker_still_rejects_altcoins(self):
        self.assertEqual(PublicClient().symbols,SYMBOLS)
        with self.assertRaises(ValueError):
            Broker().enter(self.symbols[0],1,100,2,0,self.rules[self.symbols[0]],{})
        with self.assertRaises(ValueError):
            PublicClient().get('klines',symbol=self.symbols[0])

    def test_expanded_client_still_rejects_order_endpoints(self):
        client = PublicClient(symbols=self.symbols)
        with self.assertRaises(KeyError): client.get('order')
        with self.assertRaises(ValueError): PublicClient(symbols=['BTC-USDT','BTC-USDT'])
        with self.assertRaises(ValueError): PublicClient(symbols=['../../secret'])

    def test_missing_funding_symbol_is_not_assumed_zero(self):
        self.funding.pop(self.symbols[-1])
        with self.assertRaisesRegex(ValueError,'exactly match'):
            backtest(self.candles,self.funding,self.rules,symbols=self.symbols)
