import copy
import hashlib
import hmac
import io
from dataclasses import replace
from decimal import Decimal as D
import json
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from research.trader_review.full_copy_contract import (
    CopyBlocked, compile_contract, exit_sync_plan, scaled_quantity,
    settings_plan, verify_target_settings, json_number)
from research.trader_review.strict_bingx_copy import StrictBingXExecution, source_snapshot
from research.trader_review.bingx_execution import BingXAPIError, ReconciliationRequired, HyperliquidBingXCopy, BingXExecution
from tests.test_bingx_freshness import FakeExecution


def fixture():
    account = {'marginSummary': {'accountValue': '1000'}, 'assetPositions': [
        {'position': {'coin': 'BTC', 'szi': '10', 'marginUsed': '500',
                      'leverage': {'type': 'cross', 'value': 2}}}]}
    orders = [{'oid': 100+i, 'coin': 'BTC', 'side': 'A', 'sz': '0',
               'isPositionTpsl': True, 'reduceOnly': True, 'isTrigger': True,
               'orderType': kind, 'triggerPx': price, 'limitPx': price,
               'children': [], 'tif': None}
              for i, (kind, price) in enumerate([('Stop Market', '90'), ('Take Profit Market', '110')])]
    rules = dict(price='100', quantity_step='.001', minimum_quantity='.001',
                 minimum_notional='2', price_tick='.1')
    return account, orders, rules


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.account, self.orders, self.rules = fixture()

    def compile(self, **kw):
        return compile_contract(self.account, self.orders, 'BTC', '85', self.rules,
                                observed_ms=1000, now_ms=1000, **kw)

    def test_full_contract_copies_settings_levels_and_proportional_size(self):
        c = self.compile()
        self.assertEqual((c.leverage, c.margin_type, D(c.target_quantity)), (2, 'CROSSED', D('.850')))
        brackets = c.entry_brackets()
        self.assertEqual(json.loads(brackets['stopLoss']),
                         {'type': 'STOP_MARKET', 'stopPrice': 90.0, 'workingType': 'MARK_PRICE', 'stopGuaranteed': False})
        self.assertIsInstance(json.loads(brackets['takeProfit'])['stopPrice'], float)

    def test_no_inflation_to_minimum_notional(self):
        with self.assertRaisesRegex(CopyBlocked, 'below_exchange_minimum'):
            scaled_quantity('1', '.001', '.001', '.001', '2', '100')

    def test_missing_stop_or_take_profit_blocks_entry(self):
        for remove, reason in [(0, 'no_stop_loss'), (1, 'no_take_profit')]:
            with self.subTest(remove=remove):
                self.orders = fixture()[1]; self.orders.pop(remove)
                with self.assertRaisesRegex(CopyBlocked, reason): self.compile()

    def test_partial_stop_must_cover_whole_position(self):
        self.orders[0].update(sz='5', isPositionTpsl=False)
        with self.assertRaisesRegex(CopyBlocked, 'does_not_cover'): self.compile()

    def test_ladder_is_planned_but_not_claimed_as_atomic_entry_pair(self):
        self.orders[0].update(sz='5', isPositionTpsl=False)
        self.orders.append({**self.orders[0], 'oid': 102, 'triggerPx': '89'})
        c = self.compile()
        self.assertEqual(len(c.exits), 3)
        with self.assertRaisesRegex(CopyBlocked, 'single_full_position'): c.entry_brackets()

    def test_isolated_added_margin_is_not_replaced_by_leverage_alone(self):
        p = self.account['assetPositions'][0]['position']
        p.update(entryPx='100', marginUsed='565', unrealizedPnl='15',
                 leverage={'type': 'isolated', 'value': 2, 'rawUsd': '-450'})
        with self.assertRaisesRegex(CopyBlocked, 'isolated_collateral'):
            self.compile()
        self.assertTrue(self.compile(for_exit_sync=True).isolated_collateral_adjusted)

    def test_standard_isolated_margin_ignores_unrealized_pnl_on_either_side(self):
        for side, qty, raw_usd, unrealized_pnl, stops in (
            ('A', '10', '-500', '15', ('90', '110')),
            ('B', '-10', '1500', '-15', ('110', '90')),
        ):
            with self.subTest(side=side):
                p = self.account['assetPositions'][0]['position']
                p.update(szi=qty, entryPx='100', marginUsed=str(500 + D(unrealized_pnl)),
                         unrealizedPnl=unrealized_pnl,
                         leverage={'type': 'isolated', 'value': 2, 'rawUsd': raw_usd})
                for order, stop in zip(self.orders, stops):
                    order.update(side=side, triggerPx=stop)
                contract = self.compile(target_quantity='.8')
                self.assertEqual(contract.margin_type, 'ISOLATED')
                self.assertEqual(D(contract.target_margin), D('40'))
                self.assertFalse(contract.isolated_collateral_adjusted)

    def test_unknown_isolated_collateral_prevents_entry(self):
        self.account['assetPositions'][0]['position']['leverage']['type'] = 'isolated'
        with self.assertRaisesRegex(CopyBlocked, 'isolated_collateral'):
            self.compile()

    def test_already_crossed_stop_and_profit_block_entry(self):
        for index, price, reason in [(0, '101', 'stop_already'), (1, '99', 'take_profit_already')]:
            with self.subTest(index=index):
                self.orders = fixture()[1];self.orders[index]['triggerPx'] = price
                with self.assertRaisesRegex(CopyBlocked, reason): self.compile()

    def test_short_side_and_mark_price(self):
        self.account['assetPositions'][0]['position']['szi'] = '-10'
        for order, price in zip(self.orders, ['110', '90']):order.update(side='B', triggerPx=price)
        c = self.compile()
        self.assertEqual(c.position_side, 'SHORT')
        self.assertEqual(c.exits[0].bingx(c.symbol, hedge=True)['positionSide'], 'SHORT')
        self.assertNotIn('reduceOnly', c.exits[0].bingx(c.symbol, hedge=True))

    def test_wrong_side_invalid_tick_nan_stale_and_duplicate_conflict(self):
        cases = [lambda: self.orders[0].update(side='B'),
                 lambda: self.orders[0].update(triggerPx='90.01'),
                 lambda: self.rules.update(price='NaN'),
                 lambda: self.orders.append({**self.orders[0], 'triggerPx': '89'})]
        for mutate in cases:
            self.account,self.orders,self.rules=fixture();mutate()
            with self.assertRaises(CopyBlocked):self.compile()
        with self.assertRaisesRegex(CopyBlocked, 'stale_or_future'):
            compile_contract(*fixture()[:2], 'BTC', 85, fixture()[2], observed_ms=1, now_ms=6000)

    def test_unactivated_parent_children_do_not_certify_stop(self):
        self.orders = [{**self.orders[1], 'children': [self.orders[0]]}]
        with self.assertRaisesRegex(CopyBlocked, 'no_stop_loss'):self.compile()

    def test_unknown_orders_and_pending_entries_are_not_silently_ignored(self):
        self.orders.append({**self.orders[0], 'oid': 200, 'reduceOnly': False})
        with self.assertRaisesRegex(CopyBlocked, 'pending_source_entry'):self.compile()

    def test_limit_exit_semantics_remain_limit(self):
        self.orders[1].update(orderType='Limit', isTrigger=False, limitPx='110', tif='Alo', sz='10', isPositionTpsl=False)
        c=self.compile();o=c.exits[1].bingx(c.symbol)
        self.assertEqual((o['type'],o['timeInForce']),('LIMIT','PostOnly'))
        self.assertNotIn('stopPrice',o)
        with self.assertRaises(CopyBlocked):c.entry_brackets()

    def test_settings_require_flat_account_and_confirmed_readback(self):
        c=self.compile();lev={'longLeverage':20,'maxLongLeverage':50};margin={'marginType':'ISOLATED'}
        changes=settings_plan(c,lev,margin,hedge=False,target_has_position=False,target_has_orders=False)
        self.assertEqual([p for _,p,_ in changes],['trade/marginType','trade/leverage'])
        self.assertEqual(changes[1][2]['side'],'BOTH')
        with self.assertRaisesRegex(CopyBlocked,'existing_exposure'):
            settings_plan(c,lev,margin,hedge=False,target_has_position=True,target_has_orders=False)
        with self.assertRaisesRegex(CopyBlocked,'mismatch'):verify_target_settings(c,lev,margin)
        with self.assertRaisesRegex(CopyBlocked,'exceeds_target'):
            settings_plan(c,{'longLeverage':20,'maxLongLeverage':1},margin,hedge=False,target_has_position=False,target_has_orders=False)

    def test_exit_changes_resize_replace_and_cancel_only_owned(self):
        c=self.compile();owned={o.source_id:{'order_id':str(i),'parameters':o.bingx(c.symbol)} for i,o in enumerate(c.exits)}
        self.assertFalse(exit_sync_plan(c,owned))
        new=replace(c,exits=(replace(c.exits[0],stop_price='91'),c.exits[1]))
        p=exit_sync_plan(new,owned)
        self.assertEqual((p[0]['action'],p[0]['cancelReplaceMode']),('replace','STOP_ON_FAILURE'))
        owned['obsolete']={'order_id':'77','parameters':{}}
        self.assertEqual(exit_sync_plan(c,owned)[0]['order_id'],'77')
        owned['100']['ambiguous']=True
        with self.assertRaisesRegex(CopyBlocked,'ambiguous'):exit_sync_plan(c,owned)

    def test_numeric_conversion_refuses_precision_loss(self):
        with self.assertRaises(CopyBlocked):json_number('10000000000000000.1')

    def test_cross_mark_margin_fluctuation_is_not_an_order_amendment(self):
        c=self.compile()
        self.account['assetPositions'][0]['position']['marginUsed']='500.1'
        self.assertEqual(c.fingerprint(),self.compile().fingerprint())

    def test_snapshot_refuses_source_position_race(self):
        class Source:
            count=0
            def get(inner,kind,**kwargs):
                if kind=='frontendOpenOrders':return copy.deepcopy(self.orders)
                inner.count+=1;a=copy.deepcopy(self.account)
                if inner.count==2:a['assetPositions'][0]['position']['szi']='11'
                return a
        with self.assertRaisesRegex(CopyBlocked,'source_changed'):source_snapshot(Source(),'test')


class StrictExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=self.tmp.name+'/copy.sqlite'
        self.x=StrictBingXExecution('test-key','test-secret',self.path,trading_enabled=True,
                                  source_client=None,source_address='public-test-address',expected_uid='123')
        a,o,r=fixture();self.contract=compile_contract(a,o,'BTC',85,r,observed_ms=1000,now_ms=1000)
        self.x._contract=lambda *a,**kw:self.contract
        self.x.equity=lambda:D('85')
        self.x.hedge_mode=lambda:False
        self.orders=[];self.requests=[];self.leverage=20;self.margin='ISOLATED'
        self.x._request=self.request
        self.event=dict(orderId='entry',positionId='epoch',quantity=10,price=100)
        self.x._rules=lambda *a:{'price':'100'}
        self.x.set_context(self.event,D('0'),D('.085'))
        self.deadline=time.time_ns()//1_000_000+30000

    def tearDown(self):self.tmp.cleanup()

    def request(self,method,path,**kw):
        self.requests.append((method,path,kw))
        if path=='uid':return {'uid':'123'}
        if path=='user/positions':return []
        if path=='trade/openOrders':return {'orders':self.orders.copy()}
        if path=='trade/leverage':
            if method=='POST':self.leverage=kw['leverage'];return {}
            return {'longLeverage':self.leverage,'shortLeverage':self.leverage,'maxLongLeverage':100,'maxShortLeverage':100}
        if path=='trade/marginType':
            if method=='POST':self.margin=kw['marginType'];return {}
            return {'marginType':self.margin}
        if path=='trade/order':
            if method=='GET':return {'status':'FILLED','executedQty':'.850','side':'SELL'}
            if method=='POST' and kw['type']=='MARKET':
                if kw.get('reduceOnly')=='true':return {'orderId':'close'}
                for i,k in enumerate(['stopLoss','takeProfit']):
                    p=json.loads(kw[k]);self.orders.append(dict(orderId=str(i+1),symbol='BTC-USDT',
                        positionSide='BOTH',side='SELL',type=p['type'],origQty=kw['quantity'],
                        stopPrice=p['stopPrice'],workingType=p['workingType'],reduceOnly=True))
                return {'orderId':'parent'}
        if path=='trade/cancelReplace':
            old=next(o for o in self.orders if o['orderId']==kw['cancelOrderId'])
            self.orders.remove(old);oid=str(int(kw['cancelOrderId'])+100)
            self.orders.append({**old,'orderId':oid,'origQty':kw['quantity'],
                                'stopPrice':kw.get('stopPrice',old.get('stopPrice'))})
            return {'cancelResult':'SUCCESS','newOrderResult':'SUCCESS','newOrderId':oid}
        raise AssertionError((method,path,kw))

    def test_leverage_margin_and_atomic_brackets_before_entry_with_no_duplicate_on_retry(self):
        self.x.submit('entry','BTC-USDT','BUY',D('.850'),deadline_ms=self.deadline)
        mutations=[(m,p) for m,p,_ in self.requests if m=='POST']
        self.assertEqual(mutations,[('POST','trade/marginType'),('POST','trade/leverage'),('POST','trade/order')])
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM copy_exit_orders').fetchone()[0],2)
        self.x.submit('entry','BTC-USDT','BUY',D('.850'),deadline_ms=self.deadline)
        self.assertEqual(sum(m=='POST' and p=='trade/order' for m,p,_ in self.requests),1)

    def test_isolated_entry_sets_and_reads_back_margin_before_sending_brackets(self):
        self.margin = 'CROSSED'
        self.contract = replace(self.contract, margin_type='ISOLATED')
        self.x.submit('entry', 'BTC-USDT', 'BUY', D('.850'), deadline_ms=self.deadline)
        settings = [(method, params) for method, path, params in self.requests if path == 'trade/marginType']
        self.assertEqual([method for method, _ in settings], ['GET', 'POST', 'GET'])
        self.assertEqual(settings[1][1]['marginType'], 'ISOLATED')
        entry = next(params for method, path, params in self.requests if (method, path) == ('POST', 'trade/order'))
        self.assertIn('stopLoss', entry)
        self.assertIn('takeProfit', entry)

    def test_live_allocation_blocks_entry_before_settings_changes(self):
        self.x.live_allocation = D('100')
        self.x.equity = lambda: D('185')
        original = self.x._request
        other_position = dict(symbol='ETH-USDT', positionAmt='1', initialMargin='60')
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE copied_positions(epoch TEXT, quantity TEXT)')
            db.execute('INSERT INTO copied_positions VALUES (?,?)', ('existing', '1'))
            db.execute('INSERT INTO copy_contracts VALUES (?,?,?,?)',
                       ('existing', 'ETH-USDT', '.1', '{}'))
        def request(method, path, **kwargs):
            if path == 'user/positions' and 'symbol' not in kwargs:
                return [other_position]
            return original(method, path, **kwargs)
        self.x._request = request

        self.assertEqual(self.x.contract_capital(), D('100'))
        with self.assertRaisesRegex(CopyBlocked, 'entry_exceeds_live_margin_allocation'):
            self.x.submit('entry', 'BTC-USDT', 'BUY', D('.850'), deadline_ms=self.deadline)
        self.assertFalse(any(method == 'POST' for method, _, _ in self.requests))

    def test_live_identity_fallback_requires_full_matching_uid(self):
        for uid in ('123', '1**3', '456'):
            with self.subTest(uid=uid), patch.object(self.x, '_request', side_effect=[
                BingXAPIError(100400, 'Endpoint unavailable'),
                [{'asset': 'USDT', 'userId': uid}],
            ]) as api:
                if uid == '123':
                    self.x.verify_account_identity()
                else:
                    with self.assertRaisesRegex(CopyBlocked, 'account_uid_mismatch'):
                        self.x.verify_account_identity()
                api.assert_called_with('GET', 'user/balance', version=3)

    def test_isolated_collateral_change_blocks_adds_but_keeps_stop_sync(self):
        self.contract = replace(self.contract, margin_type='ISOLATED')
        self.x.submit('entry', 'BTC-USDT', 'BUY', D('.850'), deadline_ms=self.deadline)
        self.x._positions = lambda _: [{'positionAmt': '.850'}]
        self.contract = replace(
            self.contract, isolated_collateral_adjusted=True,
            exits=(replace(self.contract.exits[0], stop_price='91'), self.contract.exits[1]))

        with self.position_state() as state:
            self.x.synchronize_exits(state)
        self.assertEqual(self.x.open_blocker, 'isolated_collateral_requires_separate_verified_transfer')
        self.assertTrue(any(str(o.get('stopPrice')) == '91' for o in self.orders))

    def test_missing_source_protection_or_inflated_minimum_prevents_order(self):
        self.x._contract=lambda *a,**kw:(_ for _ in ()).throw(CopyBlocked('source_has_no_stop_loss'))
        with self.assertRaises(CopyBlocked):self.x.submit('entry','BTC-USDT','BUY',D('.850'),deadline_ms=self.deadline)
        self.assertFalse(any(m=='POST' for m,_,_ in self.requests))
        self.requests.clear()
        with self.assertRaisesRegex(CopyBlocked,'increase_source_risk'):
            self.x.submit('entry','BTC-USDT','BUY',D('1'),deadline_ms=self.deadline)
        self.assertFalse(any(m in ('POST','DELETE') for m,_,_ in self.requests))

    def test_failed_settings_readback_never_submits_entry(self):
        original=self.x._request
        def wrong(method,path,**kw):
            if path=='trade/leverage' and method=='POST':return {}
            return original(method,path,**kw)
        self.x._request=wrong
        with self.assertRaisesRegex(CopyBlocked,'mismatch'):
            self.x.submit('entry','BTC-USDT','BUY',D('.850'),deadline_ms=self.deadline)
        self.assertFalse(any(m=='POST' and p=='trade/order' for m,p,_ in self.requests))

    def test_unknown_manual_orders_are_not_adopted(self):
        self.orders=[{'orderId':'manual'}]
        with self.assertRaisesRegex(CopyBlocked,'unowned_target_orders'):
            self.x.submit('entry','BTC-USDT','BUY',D('.850'),deadline_ms=self.deadline)

    def test_conditional_timeout_is_fenced_across_restart(self):
        params=self.contract.exits[0].bingx('BTC-USDT')
        self.x._request=lambda *a,**kw:(_ for _ in ()).throw(TimeoutError('ambiguous'))
        with sqlite3.connect(self.path) as db:
            with self.assertRaises(TimeoutError):
                self.x._dispatch_exit(db,'epoch',{'action':'create','source_id':'100','parameters':params},'BTC-USDT')
        restarted=StrictBingXExecution('test-key','test-secret',self.path,trading_enabled=True,source_client=None,source_address='test')
        restarted.verify_account_identity=lambda:None
        state=sqlite3.connect(':memory:');state.execute('CREATE TABLE copied_positions(epoch TEXT,quantity TEXT)')
        with patch.object(restarted,'_request') as network:
            with self.assertRaisesRegex(ReconciliationRequired,'Ambiguous protection'):restarted.synchronize_exits(state)
            network.assert_not_called()
        state.close()

    def test_reduction_does_not_require_new_source_protection(self):
        self.x._contract=lambda *a:(_ for _ in ()).throw(AssertionError('must not read new-entry protections'))
        self.x.submit('close','BTC-USDT','SELL',D('.850'),reduce_only=True)
        post=next(kw for m,p,kw in self.requests if m=='POST' and p=='trade/order')
        self.assertEqual(post['reduceOnly'],'true');self.assertNotIn('stopLoss',post)

    def position_state(self,quantity='.850'):
        db=sqlite3.connect(':memory:')
        db.execute('CREATE TABLE copied_positions(epoch TEXT,quantity TEXT)')
        db.execute('INSERT INTO copied_positions VALUES (?,?)',('epoch',quantity));db.commit()
        return db

    def test_protection_move_is_verified_and_saved_by_actual_order_id(self):
        self.x.submit('entry','BTC-USDT','BUY',D('.850'),deadline_ms=self.deadline)
        self.x._positions=lambda *a:[{'positionAmt':'.850'}]
        self.contract=replace(self.contract,exits=(replace(self.contract.exits[0],stop_price='91'),self.contract.exits[1]))
        state=self.position_state()
        self.x.synchronize_exits(state)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT order_id FROM copy_exit_orders WHERE source_id='100'").fetchone()[0],'101')
            self.assertEqual(db.execute('SELECT count(*) FROM copy_exit_dispatch WHERE result IS NULL').fetchone()[0],0)
        self.assertEqual(sum(p=='trade/cancelReplace' for _,p,_ in self.requests),1)
        self.x.synchronize_exits(state)
        self.assertEqual(sum(p=='trade/cancelReplace' for _,p,_ in self.requests),1)
        state.close()

    def test_removed_source_stop_keeps_existing_protection_and_blocks_increases(self):
        self.x.submit('entry','BTC-USDT','BUY',D('.850'),deadline_ms=self.deadline)
        self.x._positions=lambda *a:[{'positionAmt':'.850'}]
        self.x._contract=lambda *a,**kw:(_ for _ in ()).throw(CopyBlocked('source_has_no_stop_loss'))
        self.requests.clear();state=self.position_state();self.x.synchronize_exits(state)
        self.assertEqual(self.x.open_blocker,'source_has_no_stop_loss')
        self.assertFalse(any(m in ('POST','DELETE') for m,_,_ in self.requests));state.close()

    def test_local_protection_fill_reconciles_position_without_reopening(self):
        self.x.submit('entry','BTC-USDT','BUY',D('.850'),deadline_ms=self.deadline)
        state=self.position_state();self.x._positions=lambda *a:[]
        original=self.x._request
        def query(method,path,**kw):
            if method=='GET' and path=='trade/order' and 'orderId' in kw:
                return {'executedQty':'.850' if kw['orderId']=='1' else '0','side':'SELL','status':'FILLED' if kw['orderId']=='1' else 'NEW'}
            return original(method,path,**kw)
        self.x._request=query
        self.x.synchronize_exits(state)
        self.assertEqual(state.execute('SELECT count(*) FROM copied_positions').fetchone()[0],0)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM locally_exited_epochs').fetchone()[0],1)
        self.x.set_context(dict(orderId='add',positionId='epoch',quantity=1,price=100),D('10'),D('.085'))
        with self.assertRaisesRegex(CopyBlocked,'do_not_reopen'):
            self.x.submit('add','BTC-USDT','BUY',D('.085'),deadline_ms=self.deadline)
        state.close()

    def test_manual_position_change_is_not_called_a_protective_fill(self):
        self.x.submit('entry','BTC-USDT','BUY',D('.850'),deadline_ms=self.deadline)
        self.x._positions=lambda *a:[{'positionAmt':'.4'}]
        original=self.x._request
        def query(method,path,**kw):
            if method=='GET' and path=='trade/order' and 'orderId' in kw:
                return {'executedQty':'0','side':'SELL','status':'NEW'}
            return original(method,path,**kw)
        self.x._request=query;state=self.position_state()
        with self.assertRaisesRegex(ReconciliationRequired,'not explained'):self.x.synchronize_exits(state)
        self.assertEqual(state.execute('SELECT quantity FROM copied_positions').fetchone()[0],'.850');state.close()

    def test_price_basis_guard_prevents_chasing_entry(self):
        self.x._rules=lambda *a:{'price':'101'}
        with self.assertRaisesRegex(CopyBlocked,'entry_price_deviation'):
            self.x.submit('entry','BTC-USDT','BUY',D('.850'),deadline_ms=self.deadline)
        self.assertFalse(any(m=='POST' for m,_,_ in self.requests))

    def test_wrong_account_uid_prevents_all_order_and_setting_mutations(self):
        self.x.expected_uid='456'
        with self.assertRaisesRegex(CopyBlocked,'account_uid_mismatch'):
            self.x.submit('entry','BTC-USDT','BUY',D('.850'),deadline_ms=self.deadline)
        self.assertFalse(any(m in ('POST','DELETE') for m,_,_ in self.requests))

    def test_manual_other_coin_position_prevents_mixing_copy_risk(self):
        original=self.x._request
        def manual(method,path,**kw):
            if path=='user/positions' and 'symbol' not in kw:
                return [{'symbol':'SOL-USDT','positionAmt':'1'}]
            return original(method,path,**kw)
        self.x._request=manual
        with self.assertRaisesRegex(CopyBlocked,'manual_or_unowned_position'):
            self.x.submit('entry','BTC-USDT','BUY',D('.850'),deadline_ms=self.deadline)
        self.assertFalse(any(m in ('POST','DELETE') for m,_,_ in self.requests))


class StrictSizingLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:');self.db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)')
        self.x=FakeExecution();self.x.strict_sizing=True
        self.copy=HyperliquidBingXCopy(self.db,self.x,1)
        self.market=tuple(map(D,('100','.001','.001','2')))

    def tearDown(self):self.db.close()

    def event(self,name,action,qty):
        return dict(orderId=name,positionId='epoch',symbol='BTCUSDT',action=action,
                    side='BUY' if action=='OPEN' else 'SELL',quantity=qty,
                    updatedAt=time.time_ns()//1_000_000-1000,price=100)

    def test_small_but_valid_proportional_open_is_not_raised_to_ten_dollars(self):
        result=self.copy.apply(self.event('open','OPEN',1),D('0'),D('.085'),self.market)
        self.assertEqual(result,D('.085'));self.assertEqual(self.x.orders[0][2],D('.085'))

    def test_too_small_open_is_blocked_without_any_order(self):
        with self.assertRaisesRegex(CopyBlocked,'below_exchange_minimum'):
            self.copy.apply(self.event('open','OPEN','.1'),D('0'),D('.085'),self.market)
        self.assertFalse(self.x.orders)

    def test_local_partial_protection_is_not_closed_twice_by_later_source_exit(self):
        self.db.execute("INSERT INTO copied_positions VALUES ('epoch','.085','.425')")
        self.copy.apply(self.event('source-half-close','CLOSE',5),D('10'),D('0'),self.market)
        self.assertFalse(self.x.orders)
        self.assertEqual(self.db.execute('SELECT count(*) FROM copied_signals').fetchone()[0],1)
        self.copy.apply(self.event('source-full-close','CLOSE',5),D('5'),D('0'),self.market)
        self.assertEqual(self.x.orders[0][2],D('.425'))
        self.assertEqual(self.db.execute('SELECT count(*) FROM copied_positions').fetchone()[0],0)


class SigningTests(unittest.TestCase):
    def test_signature_uses_unencoded_json_but_request_uses_percent_encoding(self):
        x=BingXExecution('key','secret',':memory:')
        sl=json.dumps({'type':'STOP_MARKET','stopPrice':90.1,'workingType':'MARK_PRICE'})
        response=io.BytesIO(json.dumps({'code':0,'data':{}}).encode())
        with patch('research.trader_review.bingx_execution.time.time_ns',return_value=123000000), \
             patch('research.trader_review.bingx_execution.urlopen',return_value=response) as request:
            x._request('POST','trade/order/test',symbol='BTC-USDT',stopLoss=sl)
            req=request.call_args.args[0];q=parse_qs(urlsplit(req.full_url).query)
            canonical='stopLoss='+sl+'&symbol=BTC-USDT&timestamp=123'
            expected=hmac.digest(b'secret',canonical.encode(),'sha256').hex()
            self.assertEqual(q['signature'],[expected]);self.assertIn('%7B',req.full_url)
            self.assertNotIn('+',req.full_url);self.assertEqual(q['stopLoss'],[sl])

    def test_signing_refuses_query_parameter_injection_before_network(self):
        x=BingXExecution('key','secret',':memory:')
        with patch('research.trader_review.bingx_execution.urlopen') as request:
            with self.assertRaisesRegex(ValueError,'parameter value'):
                x._request('GET','trade/order',symbol='BTC-USDT&secret=leak')
            request.assert_not_called()


if __name__=='__main__':unittest.main()
