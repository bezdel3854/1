import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
import time
from dataclasses import asdict
from decimal import Decimal as D
from unittest.mock import patch
from urllib.parse import urlsplit

from research.trader_review.bingx_demo_worker import BingXDemoExecution
from research.trader_review.full_copy_contract import compile_contract, CopyBlocked
from tests.test_full_copy_contract import fixture
from tests.test_bingx_freshness import FakeExecution
from research.trader_review.bingx_execution import EntryUnfilled, HyperliquidBingXCopy, ReconciliationRequired
from research.trader_review.bingx_copy_worker import batch_executed_entries


class DemoTests(unittest.TestCase):
    def make(self,**kwargs):
        return BingXDemoExecution('key','secret',':memory:',allocation=D('300'),
            source_client=None,source_address='test',expected_uid='123',**kwargs)

    def test_entry_cap_rounds_toward_the_trader_on_both_sides(self):
        execution = self.make()
        for side, price in [('BUY', '100.20'), ('SELL', '99.81')]:
            with self.subTest(side=side):
                self.assertEqual(execution.entry_price_parameters(side, D('100.003'), {'price_tick': '.01'}),
                                 dict(type='LIMIT', timeInForce='FOK', price=price))

    def test_fok_zero_execution_clears_pending_protection_but_partial_fill_does_not(self):
        with tempfile.TemporaryDirectory() as root:
            path = root + '/vst.sqlite'
            x = BingXDemoExecution('key', 'secret', path, allocation=300, source_client=None, source_address='test')
            cid = hashlib.sha256(b'replay').hexdigest()[:40]
            with sqlite3.connect(path) as db:
                db.execute('insert into bingx_submissions values (?,?)',
                           (cid, json.dumps(dict(timeInForce='FOK', side='BUY', price='100.2'))))
                db.execute('insert into copy_entry_children values (?,?,?,?,?,?,?)',
                           ('replay', 'epoch', 'SOL-USDT', '1', '["existing"]', '{}', 0))
            x._orders = lambda _: [{'orderId': 'existing'}]
            x.check_entry_result('replay', 'SOL-USDT', dict(status='CANCELLED', executedQty='.1'))
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute('select adopted from copy_entry_children').fetchone()[0], 0)
            with self.assertRaises(ReconciliationRequired):
                x.check_entry_result('replay', 'SOL-USDT', dict(status='FILLED', avgPrice='100.21'))
            for _ in range(2):
                with self.assertRaises(EntryUnfilled):
                    x.check_entry_result('replay', 'SOL-USDT', dict(status='CANCELLED', executedQty='0'))
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute('select adopted from copy_entry_children').fetchone()[0], 1)

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

    def test_exit_size_never_exceeds_position_after_entry_rounding(self):
        account, orders, rules = fixture()
        take = {**orders[1], 'orderType': 'Limit', 'isTrigger': False, 'isPositionTpsl': False,
                'sz': '10', 'tif': 'Gtc'}
        contract = compile_contract(account, [take], 'BTC', 300, rules, observed_ms=1000, now_ms=1000,
                                    ratio=D('.3'), target_quantity=D('2.999'),
                                    require_protection=False, copy_executed_entries_only=True)
        self.assertEqual(D(contract.exits[0].quantity), D('2.999'))

    def test_immediate_exit_recovers_by_receipt_without_second_post(self):
        with tempfile.TemporaryDirectory() as root:
            path = root + '/vst.sqlite'
            x = BingXDemoExecution('key', 'secret', path, allocation=300, trading_enabled=True,
                                   source_client=None, source_address='test')
            symbol, epoch, oid = 'BTC-USDT', 'test-epoch', '2124038979964637184'
            params = dict(symbol=symbol, positionSide='SHORT', side='BUY', type='LIMIT',
                          quantity='0.2027', price='57920.0', timeInForce='GTC')
            order = dict(symbol=symbol, positionSide='SHORT', side='BUY', type='LIMIT',
                         origQty='0.2027', executedQty='0.2027', price='57920.0', orderId=int(oid), status='FILLED')
            copy_db = sqlite3.connect(path)
            copier = HyperliquidBingXCopy(copy_db, x, 1)
            with copy_db:
                copy_db.execute('INSERT INTO copied_positions VALUES (?,?,?)', (epoch, '.1', '.2027'))
                copy_db.execute('INSERT INTO copy_contracts VALUES (?,?,?,?)', (epoch, symbol, '.1', '{}'))
            x.verify_account_identity = lambda: None
            x._positions = lambda _: []
            with sqlite3.connect(path) as db, patch.object(x, '_request', return_value={'order': order}) as req:
                x._orders = lambda _: (_ for _ in ()).throw(TimeoutError())
                with self.assertRaises(TimeoutError):
                    x._dispatch_exit(db, epoch, dict(action='create', source_id='1', parameters=params), symbol)
                self.assertEqual(db.execute('SELECT order_id FROM copy_exit_receipts').fetchone(), (oid,))
                x._orders = lambda _: []
                x.synchronize_exits(copier.db)
                self.assertEqual(db.execute('SELECT count(*) FROM copied_positions').fetchone(), (0,))
                self.assertEqual(db.execute('SELECT executed_qty FROM copy_exit_fills').fetchone(), ('0.2027',))
                self.assertEqual(db.execute('SELECT count(*) FROM copy_exit_dispatch WHERE result IS NULL').fetchone(), (0,))
                self.assertEqual([call.args[0] for call in req.call_args_list], ['POST', 'GET', 'GET'])
            copy_db.close()

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

    def test_vst_replacement_requires_both_success_and_matching_open_orders(self):
        a, o, r = fixture()
        contract = compile_contract(a, o, 'BTC', 300, r, observed_ms=1000, now_ms=1000)
        params = contract.exits[0].bingx('BTC-USDT', hedge=True)
        old_id, new_id = '2123992837242052608', '2123992888198651904'
        order = {**params, 'orderId': int(new_id), 'origQty': params['quantity']}
        reply = dict(cancelResult='true', replaceResult='true',
                     cancelResponse={'cancelOrderId': int(old_id)},
                     newOrderResponse={'orderId': int(new_id)})
        cases = [
            ('verified', reply, [order], None),
            ('rejected', {**reply, 'replaceResult': 'false'}, [order], 'cancel/replace failed'),
            ('wrong old ID', {**reply, 'cancelResponse': {'cancelOrderId': 1}}, [order], 'unverified order identities'),
            ('old still open', reply, [order, {'orderId': old_id}], 'Old protection still open'),
            ('wrong size', reply, [{**order, 'origQty': '.5'}], 'replacement unconfirmed'),
        ]
        for label, result, orders, error in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as root:
                path = root + '/vst.sqlite'
                x = BingXDemoExecution('key', 'secret', path, allocation=300, trading_enabled=True,
                                       source_client=None, source_address='test')
                x._request = lambda *a, **kw: result
                x._orders = lambda _: orders
                change = dict(action='replace', source_id='100', order_id=old_id, parameters=params)
                with sqlite3.connect(path) as db:
                    db.execute('INSERT INTO copy_exit_orders VALUES (?,?,?,?)',
                               ('epoch', '100', old_id, json.dumps(params)))
                    if error:
                        with self.assertRaisesRegex(ReconciliationRequired, error):
                            x._dispatch_exit(db, 'epoch', change, 'BTC-USDT')
                    else:
                        x._dispatch_exit(db, 'epoch', change, 'BTC-USDT')
                    saved_id = db.execute('SELECT order_id FROM copy_exit_orders').fetchone()[0]
                    pending = db.execute('SELECT count(*) FROM copy_exit_dispatch WHERE result IS NULL').fetchone()[0]
                    self.assertEqual((saved_id, pending), (old_id, 1) if error else (new_id, 0))

    def test_partial_limit_entry_copies_only_executed_quantity_with_exits(self):
        a,o,r=fixture()
        pending=dict(coin='BTC',oid=500,side='B',sz='20',limitPx='100',
                     reduceOnly=False,isTrigger=False,orderType='Limit',tif='Gtc')
        contract=compile_contract(a,o+[pending],'BTC',300,r,observed_ms=1000,now_ms=1000,
            ratio=D('.3'),target_quantity=D('.105'),copy_executed_entries_only=True)
        self.assertEqual(D(contract.target_quantity),D('.105'))
        self.assertEqual([e.source_id for e in contract.exits],['100','101'])
        # Remaining entry changes do not replace exits or trigger extra entry.
        changed=compile_contract(a,o+[{**pending,'sz':'19'}],'BTC',300,r,
            observed_ms=1000,now_ms=1000,ratio=D('.3'),target_quantity=D('.105'),
            copy_executed_entries_only=True)
        self.assertEqual(contract.fingerprint(),changed.fingerprint())
        with self.assertRaisesRegex(CopyBlocked,'pending_source_entry'):
            compile_contract(a,o+[pending],'BTC',300,r,observed_ms=1000,now_ms=1000)

    def test_opposite_or_unknown_pending_order_remains_blocked(self):
        a,o,r=fixture()
        pending=dict(coin='BTC',oid=500,side='B',sz='20',limitPx='100',
                     reduceOnly=False,isTrigger=False,orderType='Limit',tif='Gtc')
        for change in (dict(side='A'),dict(orderType='Stop Market'),dict(isTrigger=True)):
            with self.subTest(change=change),self.assertRaisesRegex(CopyBlocked,'pending_source_entry'):
                compile_contract(a,o+[{**pending,**change}],'BTC',300,r,
                    observed_ms=1000,now_ms=1000,copy_executed_entries_only=True)

    def test_replay_two_partial_fills_never_copies_unfilled_remainder(self):
        a,o,r=fixture(); seen=[]
        pending=dict(coin='BTC',oid=500,side='B',sz='20',limitPx='100',
                     reduceOnly=False,isTrigger=False,orderType='Limit',tif='Gtc')
        class Execution(FakeExecution):
            strict_sizing=True
            def submit(self,signal,symbol,side,quantity,**kw):
                c=compile_contract(a,o+[pending],'BTC',300,r,observed_ms=1000,now_ms=1000,
                    ratio=D('.3'),target_quantity=quantity,copy_executed_entries_only=True)
                seen.append(c)
                return super().submit(signal,symbol,side,quantity,**kw)
        db=sqlite3.connect(':memory:');db.execute('create table meta(key text primary key,value text)')
        x=Execution(); copier=HyperliquidBingXCopy(db,x,1)
        event=dict(positionId='new-epoch',symbol='BTCUSDT',action='OPEN',side='BUY',
                   updatedAt=time.time_ns()//1000000,price=100)
        market=tuple(map(D,('100','.001','.001','2')))
        copier.apply(dict(event,orderId='first',quantity='.35'),D('0'),D('.3'),market)
        copier.apply(dict(event,orderId='second',quantity='.25'),D('.35'),D('.3'),market)
        self.assertEqual([r[2] for r in x.orders],[D('.105'),D('.075')])
        self.assertEqual(D(db.execute('select quantity from copied_positions').fetchone()[0]),D('.180'))
        self.assertEqual(len(seen),2)
        db.close()

    def test_demo_add_resizes_owned_exit_instead_of_creating_duplicate(self):
        with tempfile.TemporaryDirectory() as root:
            path=root+'/demo.sqlite'
            x=BingXDemoExecution('key','secret',path,allocation=D('300'),trading_enabled=True,
                source_client=None,source_address='test',expected_uid='123')
            a,o,r=fixture();o=[{**o[1],'orderType':'Limit','isTrigger':False,
                'isPositionTpsl':False,'sz':'5','tif':'Gtc'}]
            c=compile_contract(a,o,'BTC',300,r,observed_ms=1000,now_ms=1000,require_protection=False)
            prior={**c.exits[0].bingx(c.symbol),'quantity':'1.2'}
            with sqlite3.connect(path) as db:
                db.execute('insert into copy_exit_orders values (?,?,?,?)',('epoch','101','old',json.dumps(prior)))
                payload={**asdict(c),'entry_brackets_attached':False}
                db.execute('insert into copy_entry_children values (?,?,?,?,?,?,0)',
                    ('add','epoch',c.symbol,'.3',json.dumps(['old']),json.dumps(payload)))
            x.order=lambda *a:{'status':'FILLED','executedQty':'.3'}
            x._orders=lambda *a:[{'orderId':'old'}]
            x.hedge_mode=lambda:False
            changes=[];x._dispatch_exit=lambda db,epoch,change,symbol:changes.append(change)
            x.adopt_entry_children('add')
            self.assertEqual(len(changes),1)
            self.assertEqual(changes[0]['action'],'replace')
            self.assertEqual(changes[0]['order_id'],'old')
            self.assertEqual(D(changes[0]['parameters']['quantity']),D('1.5'))

    def test_fill_batch_is_immutable_after_restart_and_never_crosses_source_orders(self):
        db=sqlite3.connect(':memory:');db.execute('create table copy_plans(id text)')
        def row(i,before,qty,oid=42):
            e=dict(orderId='fill'+str(i),positionId='epoch',symbol='ZECUSDT',side='BUY',
                   action='OPEN',quantity=str(qty),price='748.5',updatedAt=1000)
            return (i,json.dumps(e),json.dumps(dict(oid=oid,startPosition=str(before))))
        rows=[row(1,0,'.35'),row(2,'.35','.05')]
        first=batch_executed_entries(db,rows)
        self.assertEqual(len(first),1)
        self.assertEqual(D(json.loads(first[0][1])['quantity']),D('.40'))
        extended=batch_executed_entries(db,rows+[row(3,'.4','3.27')])
        self.assertEqual(extended[0],first[0])
        self.assertEqual(len(extended),2)
        other=sqlite3.connect(':memory:');other.execute('create table copy_plans(id text)')
        self.assertEqual(len(batch_executed_entries(other,[row(1,0,'.35'),row(2,'.35','.05',43)])),2)
        db.close();other.close()

    def test_already_planned_partial_fill_is_not_rebatched(self):
        db=sqlite3.connect(':memory:');db.execute('create table copy_plans(id text)')
        db.execute('insert into copy_plans values (?)',('first',));db.commit()
        base=dict(positionId='epoch',side='BUY',action='OPEN',quantity='.35',price='748.5',updatedAt=1000)
        rows=[(1,json.dumps(dict(base,orderId='first')),json.dumps(dict(oid=42,startPosition='0'))),
              (2,json.dumps(dict(base,orderId='second')),json.dumps(dict(oid=42,startPosition='.35')))]
        self.assertEqual(batch_executed_entries(db,rows),rows)
        db.close()


if __name__=='__main__':unittest.main()
