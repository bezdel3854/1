import io
import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal as D
from unittest.mock import patch
from urllib.parse import urlsplit

from research.trader_review.bingx_demo_worker import BingXDemoExecution
from research.trader_review.full_copy_contract import compile_contract, CopyBlocked
from tests.test_full_copy_contract import fixture


class DemoTests(unittest.TestCase):
    def make(self,**kwargs):
        return BingXDemoExecution('key','secret',':memory:',allocation=D('300'),
            source_client=None,source_address='test',expected_uid='123',**kwargs)

    def test_demo_orders_settings_and_reads_never_use_real_exchange_host(self):
        x=self.make(trading_enabled=True)
        for method,path in [('GET','user/positions'),('POST','trade/order'),
                            ('POST','trade/leverage'),('DELETE','trade/order')]:
            response=io.BytesIO(json.dumps({'code':0,'data':{}}).encode())
            with patch('research.trader_review.bingx_execution.urlopen',return_value=response) as request:
                x._request(method,path,symbol='HYPE-USDT')
                self.assertEqual(urlsplit(request.call_args.args[0].full_url).hostname,'open-api-vst.bingx.com')
        with self.assertRaises(ValueError):self.make(environment='live')

    def test_exchange_balance_is_actual_vst_not_allocated_budget(self):
        x=self.make()
        x._request=lambda *a,**kw:[{'asset':'VST','equity':'100000'}]
        self.assertEqual(x.equity(),D('100000'))
        self.assertEqual(x.contract_capital(),D('300'))

    def test_underwater_source_is_scaled_down_to_allocated_margin(self):
        x=self.make()
        x.equity=lambda:D('100000')
        account={'marginSummary':{'accountValue':'1648','totalMarginUsed':'1860'}}
        ratio=x.copy_ratio(account,D('300'))
        self.assertEqual((ratio*D('1860')).quantize(D('.000001')),D('297'))
        self.assertLess(ratio,D('300')/D('1648'))

    def test_demo_copies_absence_of_stop_without_inventing_one(self):
        a,o,r=fixture();o=[]
        contract=compile_contract(a,o,'BTC',300,r,observed_ms=1000,now_ms=1000,require_protection=False)
        self.assertFalse(contract.exits)
        self.assertEqual(self.make().entry_brackets(contract),{})
        with self.assertRaisesRegex(CopyBlocked,'no_stop_loss'):
            compile_contract(a,o,'BTC',300,r,observed_ms=1000,now_ms=1000)

    def test_partial_limit_exit_is_preserved_in_demo(self):
        a,o,r=fixture();o=[{**o[1],'orderType':'Limit','isTrigger':False,
                           'isPositionTpsl':False,'sz':'5','tif':'Gtc'}]
        contract=compile_contract(a,o,'BTC',300,r,observed_ms=1000,now_ms=1000,require_protection=False)
        self.assertEqual(contract.exits[0].kind,'LIMIT')
        self.assertEqual(D(contract.exits[0].quantity),D('1.5'))
        self.assertEqual(self.make().entry_brackets(contract),{})

    def test_demo_account_mismatch_still_blocks_before_any_order(self):
        x=self.make(trading_enabled=True)
        x._request=lambda *a,**kw:{'uid':'456'}
        with self.assertRaisesRegex(CopyBlocked,'account_uid_mismatch'):
            x.submit('x','BTC-USDT','BUY',D('.01'))


if __name__=='__main__':unittest.main()
