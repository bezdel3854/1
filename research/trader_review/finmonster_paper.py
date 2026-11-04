"""Prospective FinMonster paper experiment with an independent risk policy."""
from copy import deepcopy
from dataclasses import asdict
import json
import re
import time

from bingx_bot.config import Config
from bingx_bot.data import PublicClient
from bingx_bot.engine import Broker
from .finmonster_feed import canonical

LEGACY_POLICY = {'version': 1, 'stop_fraction': .02, 'max_entry_age_ms': 120000,
          'max_price_drift': .01, 'profit_share_assumption': .16,
          'config': asdict(Config())}
POLICY = {**LEGACY_POLICY, 'version': 2, 'copy_increases': 'initial_quantity_ratio'}


def symbol_for(raw):
    if not isinstance(raw, str) or not re.fullmatch(r'[A-Z0-9]+USDT', raw):
        raise ValueError('Unsupported source symbol')
    return raw[:-4] + '-USDT'


def partial_exit(broker, symbol, fraction, reference, now, reason):
    if not 0 < fraction <= 1:
        raise ValueError('Invalid close fraction')
    original = deepcopy(broker.positions[symbol])
    part = broker.positions[symbol]
    for field in ('qty', 'entry_fee', 'margin', 'budget', 'funding'):
        setattr(part, field, getattr(original, field) * fraction)
    trade = broker.exit(symbol, reference, now, reason)
    share = max(0., trade['net']) * POLICY['profit_share_assumption']
    broker.cash -= share
    trade.update(profit_share_assumption=share, net_after_share=trade['net'] - share)
    if fraction < 1:
        for field in ('qty', 'entry_fee', 'margin', 'budget', 'funding'):
            setattr(original, field, getattr(original, field) * (1-fraction))
        broker.positions[symbol] = original
    return trade


def copy_increase(broker, symbol, source_qty, copy_ratio, reference, now, rules, marks):
    """Copy the source increment at the established quantity ratio, within shared caps.

    Each execution has the original 0.5% risk allowance; all open executions share
    the existing 1% portfolio budget. Keep the common stop and include all costs.
    """
    p = broker.positions[symbol]
    c = broker.config
    if broker.risk_check(now, marks):
        return False
    fill = reference*(1+p.side*c.slip)
    if p.side*(fill-p.stop) <= 0:
        return False
    stop_fill = p.stop*(1-p.side*c.slip)
    per_unit = max(0.,p.side*(fill-stop_fill))+(fill+stop_fill)*c.fee+fill*c.funding_reserve
    eq = broker.equity(marks)
    remaining_risk = max(0.,eq*c.portfolio_risk-sum(x.budget for x in broker.positions.values()))
    available = max(0.,broker.cash-sum(x.margin for x in broker.positions.values()))
    qty = min(source_qty*copy_ratio, min(eq*c.risk,remaining_risk)/per_unit,
              max(0.,eq*c.notional_cap/fill-p.qty), available/(fill/c.leverage+fill*c.fee))
    qty = rules.floor(qty)
    if qty <= 0 or qty < rules.min_qty or qty*fill < rules.min_notional:
        return False
    old_qty = p.qty
    fee = qty*fill*c.fee
    p.entry = (old_qty*p.entry+qty*fill)/(old_qty+qty)
    p.qty += qty
    p.margin += qty*fill/c.leverage
    p.entry_fee += fee
    p.budget += qty*per_unit
    broker.cash -= fee
    broker.event(now,'increase',symbol=symbol,side=p.side,qty=qty,fill=fill,
                 entry_fee=fee,copy_ratio=copy_ratio,requested_qty=source_qty*copy_ratio,
                 total_qty=p.qty,average_entry=p.entry,common_stop=p.stop)
    return True


class Market:
    def __init__(self):
        self.client = None
        self.cached_rules = {}

    def scope(self, symbols):
        # Keep cooldown and pacing across dynamic symbol changes.
        from bingx_bot.data import symbol_scope
        if self.client is None:
            self.client = PublicClient(symbols=symbols)
        else:
            self.client.symbols = symbol_scope(symbols)

    def quote(self, symbol):
        return self.client.quote(symbol)

    def rules(self, symbol):
        if symbol not in self.cached_rules:
            self.cached_rules.update(self.client.rules()[0])
        return self.cached_rules[symbol]

    def funding(self, symbol, start, end):
        return self.client.funding(symbol, start, end)


class Paper:
    def __init__(self, db):
        self.db = db
        db.executescript('''
        CREATE TABLE IF NOT EXISTS paper_orders(order_id TEXT PRIMARY KEY, result TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS paper_journal(id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts INTEGER NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL);
        ''')

    def state(self):
        row = self.db.execute("SELECT value FROM meta WHERE key='paper_state'").fetchone()
        return json.loads(row[0]) if row else None

    def step(self, market, now=None, allow_entries=True, retire=False):
        injected_clock = now is not None
        now = time.time_ns() // 1000000 if now is None else now
        def clock():
            return now if injected_clock else time.time_ns() // 1000000
        previous = self.state()
        migrating = bool(previous and previous['policy'] == LEGACY_POLICY)
        if previous and previous['policy'] not in (POLICY, LEGACY_POLICY):
            raise ValueError('Paper policy changed: a separate account is required')
        if previous and previous.get('retired') and not retire:
            raise ValueError('Retired paper account cannot resume')
        allow_entries = allow_entries and not retire
        records = self.db.execute('''SELECT o.order_id,o.payload FROM observations o
          WHERE o.category='observed' AND NOT EXISTS
          (SELECT 1 FROM paper_orders p WHERE p.order_id=o.order_id)
          ORDER BY o.source_ms,o.order_id,o.first_seen_ms''').fetchall()
        orders = [json.loads(body) for _, body in records]
        symbols = set(previous['broker']['positions']) if previous else set()
        symbols.update(symbol_for(o['symbol']) for o in orders if o['status'] == 'Filled')
        symbols = tuple(sorted(symbols)) or ('BTC-USDT', 'ETH-USDT')
        broker = Broker.restore(previous['broker'], Config()) if previous else Broker()
        broker.symbols = symbols
        tracked = deepcopy(previous['tracked']) if previous else {}
        processed, journal, quotes, marks = {}, [], {}, {}
        if migrating:
            journal.append(('policy_migration', {'from_version': 1, 'to_version': 2,
                'reason': 'enable_user_requested_proportional_increases',
                'historical_fills_replayed': False, 'cash_preserved': broker.cash}))
        for symbol, item in tracked.items():
            item.setdefault('copy_ratio',broker.positions[symbol].qty/item['source_remaining'])

        def quote(s):
            bid, ask, stamp = market.quote(s)
            # Use actual receipt time in live mode as network work takes time.
            if not 0 < bid <= ask or not -1000 <= clock()-stamp <= 10000:
                raise ValueError('Invalid or stale executable quote')
            quotes[s], marks[s] = (bid, ask), (bid+ask)/2
            return bid, ask

        market.scope(symbols)
        for s, position in list(broker.positions.items()):
            since = tracked[s]['funding_checked_ms']
            for funding in market.funding(s, since, now+1):
                if funding.mark is None:
                    raise ValueError('Funding settlement mark is missing')
                broker.fund(s, funding.ts, funding.rate, funding.mark)
            tracked[s]['funding_checked_ms'] = now+1
            bid, ask = quote(s)
            executable = bid if position.side == 1 else ask
            if retire or position.side*(executable-position.stop) <= 0:
                trade = partial_exit(broker, s, 1, executable, clock(),
                                     'trader_replaced' if retire else 'observed_stop')
                journal.append(('trade', trade)); tracked.pop(s)
        if broker.risk_check(now, marks):
            for s, position in list(broker.positions.items()):
                bid, ask = quotes[s]
                trade = partial_exit(broker, s, 1, bid if position.side == 1 else ask, now, 'risk_halt')
                journal.append(('trade', trade)); tracked.pop(s)
        for order in ([] if retire else orders):
            identifier = str(order['orderId'])
            if identifier in processed or order['status'] != 'Filled':
                continue
            s = symbol_for(order['symbol'])
            action = order['action']
            result = {'order': order, 'result': 'skipped'}
            if action == 'CLOSE' and s in tracked and str(order['positionId']) == tracked[s]['position_id']:
                position = broker.positions[s]
                expected_side = 'SELL' if position.side == 1 else 'BUY'
                if order['side'] != expected_side:
                    raise ValueError('Close direction contradicts tracked position')
                remaining = tracked[s]['source_remaining']
                fraction = min(1, order['quantity']/remaining)
                bid, ask = quote(s)
                trade = partial_exit(broker, s, fraction, bid if position.side == 1 else ask, clock(), 'source_close')
                journal.append(('trade', trade))
                if fraction == 1:
                    tracked.pop(s)
                else:
                    tracked[s]['source_remaining'] -= order['quantity']
                result['result'] = 'partial_close' if fraction < 1 else 'close'
            elif action == 'OPEN':
                age = now-order['updatedAt']
                increasing = (s in tracked and str(order['positionId']) == tracked[s]['position_id'])
                if increasing:
                    expected = 'BUY' if broker.positions[s].side == 1 else 'SELL'
                    if order['side'] != expected:
                        raise ValueError('Increase direction contradicts tracked position')
                    # Track every actual source increase, even when our risk/freshness
                    # rules prevent a fill, so later reductions use the true denominator.
                    tracked[s]['source_remaining'] += order['quantity']
                if not allow_entries:
                    result['reason'] = 'source_unhealthy'
                elif not 0 <= age <= POLICY['max_entry_age_ms']:
                    result['reason'] = 'stale_source_event'
                elif s in broker.positions and not increasing:
                    result['reason'] = 'different_source_position'
                elif not increasing and any(o['status'] == 'Filled' and o['action'] in ('CLOSE', 'BOTH')
                         and str(o['positionId']) == str(order['positionId']) for o in orders):
                    result['reason'] = 'position_already_reduced_before_observation'
                else:
                    try:
                        rules = market.rules(s)
                    except KeyError:
                        result['reason'] = 'unsupported_contract'
                        processed[identifier] = result
                        continue
                    bid, ask = quote(s)
                    side = 1 if order['side'] == 'BUY' else -1
                    reference = ask if side == 1 else bid
                    if (ask-bid)/marks[s] > Config().max_spread:
                        result['reason'] = 'spread'
                    elif abs(reference/order['price']-1) > POLICY['max_price_drift']:
                        result['reason'] = 'price_drift'
                    elif clock()-order['updatedAt'] > POLICY['max_entry_age_ms']:
                        result['reason'] = 'stale_after_quote'
                    elif increasing:
                        before_qty = broker.positions[s].qty
                        ratio = tracked[s]['copy_ratio']
                        result['requested_paper_qty'] = order['quantity']*ratio
                        if copy_increase(broker,s,order['quantity'],ratio,reference,clock(),rules,marks):
                            result['result'] = 'increase'
                            result['filled_paper_qty'] = broker.positions[s].qty-before_qty
                            result['limited_by_risk_or_size'] = (result['filled_paper_qty']+1e-12
                                                              < result['requested_paper_qty'])
                        else:
                            result['reason'] = 'increase_risk_or_contract_minimum'
                    elif broker.enter(s, side, reference, reference*POLICY['stop_fraction'],
                                      clock(), rules, marks):
                        source_remaining = (order.get('sourcePositionAfter', order['quantity'])
                            if POLICY.get('source') == 'hyperliquid-public-fills' else order['quantity'])
                        tracked[s] = dict(position_id=str(order['positionId']),
                                          source_remaining=source_remaining, funding_checked_ms=clock(),
                                          copy_ratio=broker.positions[s].qty/source_remaining)
                        result['result'] = 'entry'
                    else:
                        result['reason'] = 'risk_or_contract_minimum'
            else:
                result['reason'] = 'untracked_close_or_ambiguous_action'
            processed[identifier] = result
        current = {'policy': POLICY, 'broker': broker.snapshot(), 'tracked': tracked,
                   'updated_at_ms': now, 'mode': 'paper', 'real_orders_enabled': False,
                   'retired': retire}
        with self.db:
            for identifier, result in processed.items():
                self.db.execute('INSERT INTO paper_orders VALUES(?,?)', (identifier, canonical(result)))
                journal.append(('source_decision', result))
            for event in broker.events:
                journal.append(('broker_event', event))
            for kind, event in journal:
                self.db.execute('INSERT INTO paper_journal(ts,kind,payload) VALUES(?,?,?)',
                                (now, kind, canonical(event)))
            self.db.execute("INSERT OR REPLACE INTO meta VALUES('paper_state',?)", (canonical(current),))
        return {'mode': 'paper', 'cash': broker.cash, 'equity': broker.equity(marks),
                'positions': len(broker.positions), 'source_decisions': len(processed),
                'real_orders_enabled': False}
