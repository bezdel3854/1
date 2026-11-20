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
            raise RuntimeError('Interrupted demo seed requires reconciliation; no automatic repeat')
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
    ratio = execution.contract_capital()/Decimal(account['marginSummary']['accountValue'])
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
        if equity < args.allocation:raise RuntimeError('Insufficient ACTUAL VST exchange equity')
        copier=HyperliquidBingXCopy(observer.db,execution,time.time_ns()//1_000_000)
        print(json.dumps({'mode':'BINGX_VST','api_origin':execution.api_origin,
                          'actual_exchange_equity':str(equity),'allocation_from_vst':str(args.allocation),
                          'source':args.address}),flush=True)
        if args.seed_current:seed_snapshot(source,copier,execution)
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
