"""Copy to BingX's real VST exchange, using a fixed allocation of its funds.

No local/synthetic balance or PnL. This executable has no live-host option.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import time
from decimal import Decimal

from .bingx_execution import HyperliquidBingXCopy
from .bingx_copy_worker import cycle
from .finmonster_feed import Observer
from .full_copy_contract import CopyBlocked
from .hyperliquid_feed import FillObserver, PublicInfo, ADDRESS
from .strict_bingx_copy import StrictBingXExecution, source_snapshot


class BingXDemoExecution(StrictBingXExecution):
    required_source_protection = False

    def __init__(self, *args, allocation, **kwargs):
        if kwargs.pop('environment', 'demo') != 'demo':
            raise ValueError('Demo worker cannot use real funds')
        self.allocation = Decimal(allocation)
        if not self.allocation.is_finite() or self.allocation < 10:
            raise ValueError('Invalid allocation')
        super().__init__(*args, environment='demo', **kwargs)

    @property
    def api_origin(self):
        return 'https://open-api-vst.bingx.com'

    def equity(self):
        rows = self._request('GET', 'user/balance', version=3)
        return next(Decimal(str(r['equity'])) for r in rows if r['asset'] in ('VST','USDT'))

    def contract_capital(self):
        # A spending allocation from actual VST funds, never a fake account balance.
        return min(self.equity(), self.allocation)

    def copy_ratio(self, account, capital):
        summary = account['marginSummary']
        # An underwater source may use more margin than its current equity.
        # Scale DOWN to fit actual allocated demo funds; leave 1% for fees.
        denominator = max(Decimal(summary['accountValue']),
                          Decimal(summary['totalMarginUsed']))
        if denominator <= 0:
            raise CopyBlocked('invalid_source_capital')
        return min(capital, self.contract_capital()) * Decimal('.99') / denominator

    def opening_parameters(self, signal_id, symbol, side, quantity, deadline_ms):
        # Cross mode uses the exchange wallet as collateral. Bound the total
        # position sizing to this allocation rather than its 100,000 VST balance.
        positions = self._request('GET', 'user/positions')
        used = sum((abs(Decimal(str(p['positionAmt']))) * Decimal(str(p['markPrice'])) /
                    Decimal(str(p['leverage'])) for p in positions), Decimal(0))
        rules = self._rules(symbol)
        account = self.source_client.get('clearinghouseState', user=self.source_address)
        source = next(p['position'] for p in account['assetPositions']
                      if p['position']['coin'] == symbol.removesuffix('-USDT'))
        additional = quantity * Decimal(str(rules['price'])) / Decimal(str(source['leverage']['value']))
        if used + additional > self.contract_capital():
            raise CopyBlocked('aggregate_demo_margin_exceeds_300_allocation')
        return super().opening_parameters(signal_id, symbol, side, quantity, deadline_ms)

    def entry_brackets(self, contract):
        try:
            return contract.entry_brackets()
        except CopyBlocked as error:
            if str(error) != 'entry_requires_single_full_position_market_sl_and_tp':
                raise
            # Demo may reproduce an absent or partial source protection. Its
            # visible exits are placed and verified after the demo entry.
            return {}


def seed_snapshot(source, copier, execution):
    db = copier.db
    state = db.execute("SELECT value FROM meta WHERE key='demo_seed_state'").fetchone()
    if state:
        if state[0] != 'complete':
            # Recovery is allowed only with positive evidence that no order was
            # ever dispatched and no exchange exposure exists. Preserve plans.
            for table in ('bingx_submissions', 'copied_positions', 'copy_exit_dispatch',
                          'copy_exit_orders'):
                if db.execute('SELECT count(*) FROM ' + table).fetchone()[0]:
                    raise RuntimeError('Interrupted demo seed requires reconciliation; no automatic repeat')
            if any(Decimal(str(p['positionAmt'])) for p in execution._request('GET','user/positions')):
                raise CopyBlocked('demo_seed_recovery_found_exchange_position')
            if execution._orders(None):
                raise CopyBlocked('demo_seed_recovery_found_exchange_orders')
            with db:
                db.execute('CREATE TABLE IF NOT EXISTS demo_abandoned_preflight '
                           '(reconciled_ms INTEGER, table_name TEXT, payload TEXT)')
                for table in ('copy_contracts', 'copy_entry_children'):
                    for row in db.execute('SELECT * FROM ' + table).fetchall():
                        db.execute('INSERT INTO demo_abandoned_preflight VALUES (?,?,?)',
                                   (time.time_ns()//1_000_000,table,json.dumps(row)))
                    db.execute('DELETE FROM ' + table)
                db.execute("INSERT OR REPLACE INTO meta VALUES ('demo_unsent_seed_reconciled',?)",
                           (str(time.time_ns()//1_000_000),))
                db.execute("DELETE FROM meta WHERE key='demo_seed_state'")
        else:
            return
    source.poll()
    account, _, stamp = source_snapshot(source.client, source.address)
    source.poll()
    cursor = json.loads(db.execute("SELECT value FROM meta WHERE key='hl_cursor'").fetchone()[0])
    positions = [p['position'] for p in account['assetPositions'] if Decimal(p['position']['szi'])]
    for p in positions:
        expected = cursor.get('positions',{}).get(p['coin'])
        if expected is not None and abs(Decimal(str(expected))-Decimal(p['szi'])) > Decimal('0.00000001'):
            raise CopyBlocked('source_changed_before_initial_demo_snapshot')
    with db:
        db.execute("INSERT INTO meta VALUES ('demo_seed_state','in_progress')")
        db.execute("INSERT OR REPLACE INTO meta VALUES ('demo_initial_snapshot_ms',?)",(str(stamp),))
    ratio = execution.copy_ratio(account, execution.contract_capital())
    now = time.time_ns()//1_000_000
    for p in positions:
        coin = p['coin']
        if not coin.isalnum() or coin.upper()!=coin:
            raise CopyBlocked('unsupported_source_position_in_initial_snapshot')
        qty = abs(Decimal(p['szi']))
        epoch = f'{coin}:demo-snapshot:{stamp}'
        event = dict(orderId=f'demo-snapshot:{stamp}:{coin}',positionId=epoch,symbol=coin+'USDT',
                     action='OPEN',side='BUY' if Decimal(p['szi'])>0 else 'SELL',quantity=str(qty),
                     price=str(Decimal(p['positionValue'])/qty),updatedAt=now,
                     source_kind='initial_current_position_snapshot')
        rules = execution._rules(coin+'-USDT')
        market=tuple(Decimal(str(rules[k])) for k in ['price','quantity_step','minimum_quantity','minimum_notional'])
        execution.set_context(event,Decimal(0),ratio)
        copier.apply(event,Decimal(0),ratio,market)
        cursor.setdefault('epochs',{})[coin]=epoch
        cursor.setdefault('positions',{})[coin]=float(p['szi'])
    cursor['baseline_ms']=now
    with db:
        db.execute("INSERT OR REPLACE INTO meta VALUES ('hl_cursor',?)",(json.dumps(cursor),))
        db.execute("UPDATE meta SET value='complete' WHERE key='demo_seed_state'")


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',required=True,type=Path)
    parser.add_argument('--address',required=True)
    parser.add_argument('--allocation',type=Decimal,default=Decimal('300'))
    parser.add_argument('--uid',required=True)
    parser.add_argument('--seed-current',action='store_true')
    parser.add_argument('--once',action='store_true')
    args=parser.parse_args()
    if args.seed_current:
        parser.error('Existing source positions must not be copied; wait for a new entry')
    if not ADDRESS.fullmatch(args.address) or not args.uid.isdigit():parser.error('Invalid source/UID')
    if args.db.name!='hyperliquid-bingx-demo.sqlite':parser.error('Dedicated demo DB name required')
    args.db.parent.mkdir(parents=True,exist_ok=True)
    with args.db.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        observer=Observer(args.db,args.address.lower(),'Hyperliquid BingX VST copy')
        source=FillObserver(observer.db,args.address)
        execution=BingXDemoExecution(os.environ['BINGX_API_KEY'],os.environ['BINGX_API_SECRET'],args.db,
            allocation=args.allocation,trading_enabled=True,source_client=PublicInfo(),
            source_address=args.address,expected_uid=args.uid)
        execution.verify_account_identity()
        equity=execution.equity()
        if equity < 10:raise RuntimeError('Insufficient ACTUAL VST exchange equity')
        copier=HyperliquidBingXCopy(observer.db,execution,time.time_ns()//1_000_000)
        print(json.dumps({'mode':'BINGX_VST','api_origin':execution.api_origin,
                          'actual_exchange_equity':str(equity),'allocation_from_vst':str(args.allocation),
                          'source':args.address}),flush=True)
        funding = observer.db.execute("SELECT value FROM meta WHERE key='demo_funding_verified'").fetchone()
        if not funding:
            while True:
                actual = execution.equity()
                if actual == args.allocation:
                    if execution._request('GET','user/positions') or execution._orders(None):
                        raise CopyBlocked('initial_demo_funding_requires_empty_exchange_account')
                    # Do not replay fills that occurred while funding was pending.
                    source.poll()
                    start = time.time_ns()//1_000_000
                    with observer.db:
                        observer.db.execute("INSERT OR REPLACE INTO meta VALUES ('demo_funding_verified',?)",
                            (json.dumps({'actual_equity':str(actual),'time_ms':start}),))
                        observer.db.execute('UPDATE copy_cutover SET ms=? WHERE id=1',(start,))
                    copier.cutover_ms = start
                    print(json.dumps({'mode':'BINGX_VST','status':'funding_confirmed_waiting_for_new_entry',
                                      'actual_exchange_equity':str(actual)}),flush=True)
                    break
                print(json.dumps({'mode':'BINGX_VST','status':'paused_until_actual_300_vst_funding',
                                  'actual_exchange_equity':str(actual),'required_equity':str(args.allocation)}),flush=True)
                if args.once:return
                time.sleep(15)
        if args.seed_current:
            try:
                seed_snapshot(source,copier,execution)
            except Exception as error:
                print(json.dumps({'mode':'BINGX_VST','status':'seed_blocked',
                                  'error':str(error)}),flush=True)
                # Keep diagnostics accessible, while sending no further orders.
                if args.once:raise
                while True:time.sleep(30)
        while True:
            try:
                cycle(source,copier,execution,min_equity=Decimal('10'),copy_allocation=args.allocation)
                print(json.dumps({'mode':'BINGX_VST','poll_ms':time.time_ns()//1_000_000,
                    'actual_exchange_equity':str(execution.equity()),
                    'allocation_from_vst':str(args.allocation),
                    'managed_positions':observer.db.execute('SELECT count(*) FROM copied_positions').fetchone()[0]}),flush=True)
            except Exception as error:
                print(json.dumps({'mode':'BINGX_VST','status':'blocked','error':str(error)}),flush=True)
                if args.once:raise
            if args.once:return
            time.sleep(5)


if __name__=='__main__':main()
