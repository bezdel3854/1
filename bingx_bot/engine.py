from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import math
from .config import Config, BAR, SYMBOLS
from .strategy import Strategy

@dataclass
class Position:
    symbol: str
    side: int
    qty: float
    entry: float
    stop: float
    target: float
    opened: int
    entry_fee: float
    margin: float
    budget: float
    funding: float = 0.0

class Broker:
    def __init__(self, config=Config()):
        self.config = config
        self.cash = config.deposit
        self.positions = {}
        self.trades = []
        self.events = []
        self.peak = config.deposit
        self.max_dd = 0.0
        self.day = None
        self.day_start = config.deposit
        self.day_halted = False
        self.halted = False
        self.last_equity = config.deposit

    def equity(self, marks):
        # Wallet includes entry fees/funding; closing fees reserved in net equity.
        return self.cash + sum(p.side*p.qty*(marks[s]-p.entry)-p.qty*marks[s]*self.config.fee
                               for s,p in self.positions.items())

    def risk_check(self, ts, marks):
        eq = self.equity(marks)
        day = datetime.fromtimestamp(ts/1000, timezone.utc).date().isoformat()
        if day != self.day:
            self.day, self.day_start, self.day_halted = day, eq, False
        self.peak = max(self.peak, eq)
        self.max_dd = max(self.max_dd, 1-eq/self.peak)
        self.last_equity = eq
        if eq <= self.day_start*(1-self.config.daily_loss):
            self.day_halted = True
        if self.max_dd >= self.config.max_drawdown or eq <= 0:
            self.halted = True
        return self.halted or self.day_halted

    def event(self, ts, kind, **fields):
        self.events.append({'ts':ts,'kind':kind,**fields})

    def enter(self, symbol, side, reference, distance, ts, rules, marks):
        if side not in (-1,1) or symbol not in SYMBOLS:
            raise ValueError('Unsupported entry')
        if not all(math.isfinite(x) and x > 0 for x in (reference,distance)):
            raise ValueError('Invalid entry price/distance')
        if self.risk_check(ts, marks) or symbol in self.positions:
            return False
        c = self.config
        fill = reference*(1+side*c.slip)
        stop = fill-side*distance
        target = fill+side*distance*c.reward_risk
        if min(stop,target) <= 0:
            return False
        eq = self.equity(marks)
        budget = eq*c.risk
        used_risk = sum(p.budget for p in self.positions.values())
        budget = min(budget,max(0,eq*c.portfolio_risk-used_risk))
        # All-in projected stop loss: distance + adverse stop execution + fees + funding reserve.
        stop_fill = stop*(1-side*c.slip)
        per_unit = abs(fill-stop_fill)+(fill+stop_fill)*c.fee+fill*c.funding_reserve
        qty = budget/per_unit
        used_margin = sum(p.margin for p in self.positions.values())
        available = max(0,self.cash-used_margin)
        qty = min(qty, eq*c.notional_cap/fill, available/(fill/c.leverage+fill*c.fee))
        qty = rules.floor(qty)
        if qty <= 0 or qty < rules.min_qty or qty*fill < rules.min_notional:
            self.event(ts,'entry_rejected',symbol=symbol,reason='size below minimum')
            return False
        margin = qty*fill/c.leverage
        # Reject stops dangerously close to proxy liquidation boundary.
        proxy_distance = fill*(1/c.leverage-c.maintenance)
        if distance >= proxy_distance*.8:
            self.event(ts,'entry_rejected',symbol=symbol,reason='stop near liquidation proxy')
            return False
        fee = qty*fill*c.fee
        self.cash -= fee
        p = Position(symbol,side,qty,fill,stop,target,ts,fee,margin,qty*per_unit)
        self.positions[symbol] = p
        self.event(ts,'entry',**asdict(p))
        return True

    def fund(self, symbol, ts, rate, mark):
        p = self.positions.get(symbol)
        if p and p.opened < ts:
            amount = p.side*p.qty*mark*rate
            self.cash -= amount
            p.funding += amount
            self.event(ts,'funding',symbol=symbol,amount=amount,rate=rate,mark=mark)

    def exit(self, symbol, reference, ts, reason):
        p = self.positions.pop(symbol)
        fill = max(0,reference*(1-p.side*self.config.slip))
        fee = p.qty*fill*self.config.fee
        gross = p.side*p.qty*(fill-p.entry)
        # Isolated-margin proxy caps losses at position collateral, excluding prior fees/funding.
        # Real exchange liquidation tiers/insurance fees are not replicated.
        if gross-fee < -p.margin:
            gross = -p.margin+fee
            reason = 'isolated_margin_loss_proxy'
        self.cash += gross-fee
        trade = {**asdict(p),'closed':ts,'exit':fill,'reason':reason,'gross':gross,
                 'exit_fee':fee,'net':gross-fee-p.entry_fee-p.funding,'cash':self.cash}
        self.trades.append(trade)
        self.event(ts,'exit',**trade)
        return trade

    def flatten(self, marks, ts, reason):
        for s in list(self.positions):
            self.exit(s,marks[s],ts,reason)

    def snapshot(self):
        return {k:v for k,v in self.__dict__.items() if k not in ('config','positions','events','trades')} | {
            'positions':{s:asdict(p) for s,p in self.positions.items()}}

    @classmethod
    def restore(cls, data, config):
        b = cls(config)
        for k,v in data.items():
            if k == 'positions':
                b.positions = {s:Position(**p) for s,p in v.items()}
            elif k in b.__dict__ and k not in ('config','trades','events'):
                setattr(b,k,v)
        return b


def bar_exit(broker, symbol, bar, phase="all"):
    p = broker.positions.get(symbol)
    if not p:
        return
    c = broker.config
    # Trade prices stand in for mark prices: this is a disclosed liquidation approximation.
    liq = p.entry*(1-p.side*(1/c.leverage-c.maintenance))
    if phase != 'intrabar' and ((p.side == 1 and bar.open <= liq) or (p.side == -1 and bar.open >= liq)):
        broker.exit(symbol,bar.open,bar.ts,'liquidation_gap_proxy')
        return
    stop_gap = bar.open <= p.stop if p.side == 1 else bar.open >= p.stop
    target_gap = bar.open >= p.target if p.side == 1 else bar.open <= p.target
    if phase == "intrabar":
        stop_gap = target_gap = False
    if stop_gap:
        broker.exit(symbol,bar.open,bar.ts,'stop_gap')
    elif target_gap:
        broker.exit(symbol,p.target,bar.ts,'take_profit') # no favourable gap improvement
    elif phase != "intrabar" and bar.ts-p.opened >= c.max_bars*BAR:
        broker.exit(symbol,bar.open,bar.ts,'time_exit')
    elif phase != "open":
        hit_stop = bar.low <= p.stop if p.side == 1 else bar.high >= p.stop
        hit_target = bar.high >= p.target if p.side == 1 else bar.low <= p.target
        if hit_stop:
            broker.exit(symbol,p.stop,bar.ts+BAR-1,'stop_both' if hit_target else 'stop')
        elif hit_target:
            broker.exit(symbol,p.target,bar.ts+BAR-1,'take_profit')


def backtest(candles, funding, rules, config=Config(), start=None, end=None):
    times = [c.ts for c in candles[SYMBOLS[0]]]
    if any([c.ts for c in candles[s]] != times for s in SYMBOLS):
        raise ValueError('Unaligned portfolio')
    strategies = {s:Strategy(config) for s in SYMBOLS}
    signals = {s:None for s in SYMBOLS}
    broker = Broker(config)
    funds = {s:{f.ts:f for f in funding[s]} for s in SYMBOLS}
    equity = []
    last_bars = None
    for i,ts in enumerate(times):
        if end is not None and ts >= end:
            break
        bars = {s:candles[s][i] for s in SYMBOLS}
        last_bars = bars
        active = start is None or ts >= start
        if active:
            marks = {s:b.open for s,b in bars.items()}
            broker.risk_check(ts,marks)
            for s in SYMBOLS:
                if ts in funds[s]:
                    broker.fund(s,ts,funds[s][ts].rate,funds[s][ts].mark or bars[s].open)
            # Exits before fresh allocations; deterministic BTC then ETH tie priority.
            for s in SYMBOLS:
                bar_exit(broker,s,bars[s],"open")
            if broker.risk_check(ts,marks):
                broker.flatten(marks,ts,'risk_halt')
            for s in SYMBOLS:
                signal = signals[s]
                if signal and signal.side:
                    broker.enter(s,signal.side,bars[s].open,signal.distance,ts,rules[s],marks)
            for s in SYMBOLS:
                bar_exit(broker,s,bars[s],"intrabar")
            closes = {s:b.close for s,b in bars.items()}
            if broker.risk_check(ts+BAR-1,closes):
                broker.flatten(closes,ts+BAR-1,'risk_halt')
            equity.append({'ts':ts+BAR,'equity':broker.equity(closes)})
        for s in SYMBOLS:
            signals[s] = strategies[s].update(bars[s])
    if equity:
        last_ts = equity[-1]['ts']
        broker.flatten({s:last_bars[s].close for s in SYMBOLS},last_ts,'end_of_test')
        equity[-1]['equity'] = broker.cash
        broker.risk_check(last_ts-1,{s:last_bars[s].close for s in SYMBOLS})
    return broker,equity
