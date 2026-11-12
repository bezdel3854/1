"""Paper-only source execution copying. No strategy stops or risk-based sizing.

Virtual quantities may be fractional below venue minimums. This is an execution
simulation, not a claim that the same quantities could be traded live.
"""
from copy import deepcopy
import json
import math
import time

from bingx_bot.config import Config
from bingx_bot.engine import Broker, Position
from .finmonster_feed import canonical
from .hyperliquid_feed import finite
from .finmonster_paper import Paper, POLICY as OLD_POLICY, symbol_for

PREVIOUS_POLICY = {**OLD_POLICY, 'version': 3, 'profit_share_assumption': 0.,
                   'source': 'hyperliquid-public-fills'}
BINGX_POLICY = {'version': 4, 'source': 'hyperliquid-public-fills',
          'sizing': 'fixed_account_equity_ratio', 'source_exits_only': True,
          'virtual_fractional_quantities': True, 'profit_share_assumption': 0.}

POLICY = {**BINGX_POLICY, 'version': 5, 'execution_prices': 'source_fills',
          'execution_fees': 'scaled_source_fees', 'valuation': 'hyperliquid_mark'}


class CopyBroker(Broker):
    def equity(self, marks):
        return self.cash + sum(p.side*p.qty*(marks[s]-p.entry)
                               for s,p in self.positions.items())

    def exit(self, symbol, fill, fee, ts):
        p = self.positions.pop(symbol)
        gross = p.side*p.qty*(fill-p.entry)
        self.cash += gross-fee
        trade = {**vars(p), 'closed': ts, 'exit': fill, 'reason': 'source_close',
                 'gross': gross, 'exit_fee': fee,
                 'net': gross-fee-p.entry_fee-p.funding, 'cash': self.cash}
        self.event(ts, 'exit', **trade)
        return trade


class SourceCopyPaper(Paper):
    def __init__(self, db, public_client, address):
        super().__init__(db)
        self.client, self.address = public_client, address

    def marks(self):
        meta, contexts = self.client.get('metaAndAssetCtxs')
        return {asset['name']+'-USDT': finite(context['markPx'], True)
                for asset, context in zip(meta['universe'], contexts)
                if context.get('markPx') is not None}

    def funding(self, start, end):
        if start >= end:
            return []
        rows = self.client.get('userFunding', user=self.address,
                               startTime=start+1, endTime=end)
        if not isinstance(rows, list) or len(rows) >= 500:
            raise ValueError('Invalid or capped Hyperliquid funding history')
        return sorted(((r['time'], r['delta']) for r in rows if start < r['time'] <= end),
                      key=lambda event:event[0])

    def step(self, now=None, allow_entries=True):
        now = time.time_ns()//1000000 if now is None else now
        old = self.state()
        if old and (old.get('mode') != 'paper' or old.get('real_orders_enabled') is not False
                    or old.get('retired')):
            raise ValueError('Only an active paper account may migrate')
        if old and old['policy'] not in (PREVIOUS_POLICY, BINGX_POLICY, POLICY):
            raise ValueError('Unrecognized paper policy')
        migrating = bool(old and old['policy'] != POLICY)
        b = CopyBroker.restore(old['broker'], Config()) if old else CopyBroker()
        tracked = deepcopy(old['tracked']) if old else {}
        rows = self.db.execute('''SELECT o.payload FROM observations o
          WHERE o.category='observed' AND NOT EXISTS
          (SELECT 1 FROM paper_orders p WHERE p.order_id=o.order_id)
          ORDER BY o.source_ms,o.rowid''').fetchall()
        orders = [json.loads(body) for body, in rows]
        if orders and not allow_entries:
            raise ValueError('Source unavailable; pending fills retained')
        if orders:
            raw_fills = {ident:json.loads(body) for ident,body in self.db.execute(
                'SELECT id,payload FROM hl_fills WHERE time>=?', (orders[0]['updatedAt'],))}
        else:
            raw_fills = {}

        def fill_terms(order, paper_qty):
            raw = raw_fills[order['orderId'].removeprefix('hl:').rsplit(':',1)[0]]
            if raw['feeToken'] != 'USDC':
                raise ValueError('Unsupported Hyperliquid fee currency')
            return (finite(raw['px'], True),
                    finite(raw['fee'])*paper_qty/finite(raw['sz'], True))

        # Apply actual source funding in execution order. Amount scales to the
        # paper quantity present at settlement, including carried positions.
        funding_from = old.get('funding_through_ms', old['updated_at_ms']) if old else now
        funding = iter(self.funding(funding_from, now) if b.positions or orders else [])
        next_funding = next(funding, None)

        def apply_funding(through):
            nonlocal next_funding
            while next_funding and next_funding[0] <= through:
                stamp, delta = next_funding
                symbol = delta['coin']+'-USDT'
                position = b.positions.get(symbol)
                if position:
                    source_size = finite(delta['szi'])
                    if source_size*position.side <= 0:
                        raise ValueError('Funding direction contradicts paper position')
                    amount = finite(delta['usdc'])*position.qty/abs(source_size)
                    b.cash += amount
                    position.funding -= amount
                    b.event(stamp, 'funding', symbol=symbol, cash_delta=amount)
                next_funding = next(funding, None)

        ratio = old.get('account_copy_ratio') if old else None
        if ratio is None:
            account = self.client.get('clearinghouseState',user=self.address)
            source_equity = float(account['marginSummary']['accountValue'])
            own_equity = b.equity(self.marks() if b.positions else {})
            if not math.isfinite(source_equity) or source_equity <= 0 or own_equity <= 0:
                raise ValueError('Positive account equity required for copy scale')
            ratio = own_equity/source_equity
        if not math.isfinite(ratio) or ratio <= 0: raise ValueError('Invalid copy scale')
        cutoff = old.get('copy_cutover_ms', now if migrating else None) if old else None
        journal, decisions = [], {}
        if migrating:
            # Keep existing position quantities and their established ratios. No
            # synthetic rebalancing or reconstruction of missed historical fills.
            journal.append(('policy_migration', {'from_version':old['policy']['version'],'to_version':POLICY['version'],
                'cash_preserved':b.cash,'historical_fills_replayed':False,
                'account_copy_ratio':ratio,'copy_cutover_ms':cutoff}))
        for o in orders:
            ident = str(o['orderId'])
            if ident in decisions or o['status'] != 'Filled': continue
            stamp = o['updatedAt']
            apply_funding(stamp)
            if not math.isfinite(o['quantity']) or o['quantity'] <= 0:
                raise ValueError('Invalid source quantity')
            if o['side'] not in ('BUY','SELL'): raise ValueError('Invalid source side')
            s = symbol_for(o['symbol']); side = 1 if o['side']=='BUY' else -1
            same = s in tracked and tracked[s]['position_id']==str(o['positionId'])
            result = {'order':o,'result':'skipped'}
            if o['action']=='CLOSE' and same:
                p = b.positions[s]
                if side == p.side: raise ValueError('Contradictory source close')
                remaining = tracked[s]['source_remaining']
                close_qty = min(p.qty,o['quantity']*tracked[s]['copy_ratio'])
                fraction = 1. if o['quantity'] >= remaining else close_qty/p.qty
                original = deepcopy(p)
                for field in ('qty','entry_fee','margin','budget','funding'):
                    setattr(p,field,getattr(p,field)*fraction)
                fill, fee = fill_terms(o, p.qty)
                trade = b.exit(s,fill,fee,stamp)
                trade['net_after_share'] = trade['net']
                journal.append(('trade',trade))
                if fraction == 1: tracked.pop(s)
                else:
                    for field in ('qty','entry_fee','margin','budget','funding'):
                        setattr(original,field,getattr(original,field)*(1-fraction))
                    b.positions[s] = original
                    tracked[s]['source_remaining'] -= o['quantity']
                result['result'] = 'close' if fraction==1 else 'partial_close'
            elif o['action']=='OPEN':
                if same and side != b.positions[s].side:
                    raise ValueError('Contradictory source increase')
                if same: tracked[s]['source_remaining'] += o['quantity']
                if cutoff is not None and o['updatedAt'] <= cutoff:
                    result['reason'] = 'pre_copy_cutover_no_replay'
                elif s in b.positions and not same:
                    raise ValueError('Conflicting source position')
                else:
                    scale = tracked[s]['copy_ratio'] if same else ratio
                    qty = o['quantity']*scale
                    fill, fee = fill_terms(o, qty)
                    if same:
                        p=b.positions[s]; total=p.qty+qty
                        p.entry=(p.qty*p.entry+qty*fill)/total
                        p.qty=total;p.entry_fee+=fee;p.margin+=qty*fill/b.config.leverage
                    else:
                        b.positions[s]=Position(s,side,qty,fill,0.,0.,stamp,fee,
                                                qty*fill/b.config.leverage,0.)
                        tracked[s]={'position_id':str(o['positionId']),
                            'source_remaining':o.get('sourcePositionAfter',o['quantity']),
                            'copy_ratio':scale}
                    b.cash-=fee
                    result.update(result='increase' if same else 'entry',
                        requested_paper_qty=qty,filled_paper_qty=qty,limited_by_risk_or_size=False)
                    b.event(stamp,result['result'],symbol=s,qty=qty,fill=fill,copy_ratio=scale)
            else: result['reason']='untracked_close_or_ambiguous_action'
            decisions[ident]=result
        apply_funding(now)
        try:
            equity = b.equity(self.marks() if b.positions else {})
        except (OSError, KeyError, ValueError, TypeError):
            # Missing valuation must not discard already recorded executions.
            equity = None
        current={'policy':POLICY,'broker':b.snapshot(),'tracked':tracked,'retired':False,
                 'mode':'paper','real_orders_enabled':False,'updated_at_ms':now,
                 'account_copy_ratio':ratio,'copy_cutover_ms':cutoff,'funding_through_ms':now}
        with self.db:
            for ident,result in decisions.items():
                self.db.execute('INSERT INTO paper_orders VALUES(?,?)',(ident,canonical(result)))
                journal.append(('source_decision',result))
            journal.extend(('broker_event',e) for e in b.events)
            for kind,event in journal:
                self.db.execute('INSERT INTO paper_journal(ts,kind,payload) VALUES(?,?,?)',
                                (now,kind,canonical(event)))
            self.db.execute("INSERT OR REPLACE INTO meta VALUES('paper_state',?)",(canonical(current),))
        return {'mode':'paper','cash':b.cash,'equity':equity,
                'positions':len(b.positions),'source_decisions':len(decisions),
                'real_orders_enabled':False,'copy_policy':POLICY,'account_copy_ratio':ratio}
