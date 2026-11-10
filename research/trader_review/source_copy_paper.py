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
from .finmonster_paper import Paper, POLICY as OLD_POLICY, symbol_for

PREVIOUS_POLICY = {**OLD_POLICY, 'version': 3, 'profit_share_assumption': 0.,
                   'source': 'hyperliquid-public-fills'}
POLICY = {'version': 4, 'source': 'hyperliquid-public-fills',
          'sizing': 'fixed_account_equity_ratio', 'source_exits_only': True,
          'virtual_fractional_quantities': True, 'profit_share_assumption': 0.}


class CopyBroker(Broker):
    def exit(self, symbol, reference, ts, reason):
        # Do not replace source exits with the old isolated-margin loss cap.
        p = self.positions.pop(symbol)
        fill = reference * (1-p.side*self.config.slip)
        fee = p.qty*fill*self.config.fee
        gross = p.side*p.qty*(fill-p.entry)
        self.cash += gross-fee
        trade = {**vars(p), 'closed': ts, 'exit': fill, 'reason': reason,
                 'gross': gross, 'exit_fee': fee,
                 'net': gross-fee-p.entry_fee-p.funding, 'cash': self.cash}
        self.event(ts, 'exit', **trade)
        return trade


class SourceCopyPaper(Paper):
    def __init__(self, db, public_client, address):
        super().__init__(db)
        self.client, self.address = public_client, address

    def step(self, market, now=None, allow_entries=True):
        fixed_clock = now is not None
        now = time.time_ns()//1000000 if now is None else now
        def clock(): return now if fixed_clock else time.time_ns()//1000000
        old = self.state()
        if old and (old.get('mode') != 'paper' or old.get('real_orders_enabled') is not False
                    or old.get('retired')):
            raise ValueError('Only an active paper account may migrate')
        if old and old['policy'] not in (PREVIOUS_POLICY, POLICY):
            raise ValueError('Unrecognized paper policy')
        migrating = bool(old and old['policy'] != POLICY)
        b = CopyBroker.restore(old['broker'], Config()) if old else CopyBroker()
        tracked = deepcopy(old['tracked']) if old else {}
        rows = self.db.execute('''SELECT o.order_id,o.payload FROM observations o
          WHERE o.category='observed' AND NOT EXISTS
          (SELECT 1 FROM paper_orders p WHERE p.order_id=o.order_id)
          ORDER BY o.source_ms,o.order_id,o.first_seen_ms''').fetchall()
        orders = [json.loads(body) for _, body in rows]
        symbols = set(b.positions)
        symbols.update(symbol_for(o['symbol']) for o in orders if o['status']=='Filled')
        b.symbols = tuple(sorted(symbols)) or ('BTC-USDT', 'ETH-USDT')
        market.scope(b.symbols)
        marks = {}
        def quote(s):
            bid, ask, stamp = market.quote(s)
            if not all(math.isfinite(x) for x in (bid,ask,stamp)) or not 0 < bid <= ask:
                raise ValueError('Invalid quote')
            if not -1000 <= clock()-stamp <= 10000:
                raise ValueError('Stale quote')
            marks[s] = (bid+ask)/2
            return bid,ask
        for s in b.positions:
            quote(s)
            since = tracked[s]['funding_checked_ms']
            for f in market.funding(s,since,now+1):
                if f.mark is None: raise ValueError('Missing funding mark')
                b.fund(s,f.ts,f.rate,f.mark)
            tracked[s]['funding_checked_ms'] = now+1
        ratio = old.get('account_copy_ratio') if old else None
        if ratio is None:
            account = self.client.get('clearinghouseState',user=self.address)
            source_equity = float(account['marginSummary']['accountValue'])
            own_equity = b.equity(marks)
            if not math.isfinite(source_equity) or source_equity <= 0 or own_equity <= 0:
                raise ValueError('Positive account equity required for copy scale')
            ratio = own_equity/source_equity
        if not math.isfinite(ratio) or ratio <= 0: raise ValueError('Invalid copy scale')
        cutoff = old.get('copy_cutover_ms', now if migrating else None) if old else None
        journal, decisions = [], {}
        if migrating:
            # Keep existing position quantities and their established ratios. No
            # synthetic rebalancing or reconstruction of missed historical fills.
            journal.append(('policy_migration', {'from_version':3,'to_version':4,
                'cash_preserved':b.cash,'historical_fills_replayed':False,
                'account_copy_ratio':ratio,'copy_cutover_ms':cutoff}))
        for o in orders:
            ident = str(o['orderId'])
            if ident in decisions or o['status'] != 'Filled': continue
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
                bid,ask = quote(s)
                trade = b.exit(s,bid if p.side==1 else ask,clock(),'source_close')
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
                if not allow_entries:
                    # Retry after source recovery; do not permanently consume fills.
                    raise ValueError('Source unavailable; pending fills retained')
                if cutoff is not None and o['updatedAt'] <= cutoff:
                    result['reason'] = 'pre_copy_cutover_no_replay'
                elif s in b.positions and not same:
                    raise ValueError('Conflicting source position')
                else:
                    bid,ask = quote(s); fill = (ask if side==1 else bid)*(1+side*b.config.slip)
                    scale = tracked[s]['copy_ratio'] if same else ratio
                    qty = o['quantity']*scale; fee = qty*fill*b.config.fee
                    if same:
                        p=b.positions[s]; total=p.qty+qty
                        p.entry=(p.qty*p.entry+qty*fill)/total
                        p.qty=total;p.entry_fee+=fee;p.margin+=qty*fill/b.config.leverage
                    else:
                        b.positions[s]=Position(s,side,qty,fill,0.,0.,clock(),fee,
                                                qty*fill/b.config.leverage,0.)
                        tracked[s]={'position_id':str(o['positionId']),
                            'source_remaining':o.get('sourcePositionAfter',o['quantity']),
                            'funding_checked_ms':clock(),'copy_ratio':scale}
                    b.cash-=fee
                    result.update(result='increase' if same else 'entry',
                        requested_paper_qty=qty,filled_paper_qty=qty,limited_by_risk_or_size=False)
                    b.event(clock(),result['result'],symbol=s,qty=qty,fill=fill,copy_ratio=scale)
            else: result['reason']='untracked_close_or_ambiguous_action'
            decisions[ident]=result
        current={'policy':POLICY,'broker':b.snapshot(),'tracked':tracked,'retired':False,
                 'mode':'paper','real_orders_enabled':False,'updated_at_ms':now,
                 'account_copy_ratio':ratio,'copy_cutover_ms':cutoff}
        with self.db:
            for ident,result in decisions.items():
                self.db.execute('INSERT INTO paper_orders VALUES(?,?)',(ident,canonical(result)))
                journal.append(('source_decision',result))
            journal.extend(('broker_event',e) for e in b.events)
            for kind,event in journal:
                self.db.execute('INSERT INTO paper_journal(ts,kind,payload) VALUES(?,?,?)',
                                (now,kind,canonical(event)))
            self.db.execute("INSERT OR REPLACE INTO meta VALUES('paper_state',?)",(canonical(current),))
        return {'mode':'paper','cash':b.cash,'equity':b.equity(marks),
                'positions':len(b.positions),'source_decisions':len(decisions),
                'real_orders_enabled':False,'copy_policy':POLICY,'account_copy_ratio':ratio}
