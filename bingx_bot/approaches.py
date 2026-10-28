"""Transparent experimental adaptations, not replicas of traders' private accounts."""
from collections import deque
from statistics import fmean, pstdev
from .config import BAR, HOUR
from .data import Candle
from .strategy import Strategy, Signal, Indicators

EXPERIMENT_NAMES = ('baseline','donchian','ewmac','pullback','range_reversion',
         'volatility_breakout','regime_composite','consensus')
NAMES = EXPERIMENT_NAMES + ('candidate_120h',)

class Context:
    def __init__(self):
        self.m = Indicators()
        self.h = Indicators()
        self.fast = self.slow = None
        self.hours = deque(maxlen=100)
        self.closes = deque(maxlen=21)
        self.part = []
        self.last = None
        self.prev = None
        self.prev_mean = self.prev_sd = None
        self.day = None
        self.day_open = self.day_high = self.day_low = None
        self.previous_day_range = None
        self.full_day = False

    def update(self,c):
        if self.last is not None and c.ts-self.last != BAR:
            raise ValueError('Experimental strategy gap')
        self.last=c.ts
        day=c.ts//86_400_000
        if day != self.day:
            self.previous_day_range = self.day_high-self.day_low if self.full_day else None
            self.day=day
            self.day_open=self.day_high=self.day_low=c.open
            self.full_day = c.ts % 86_400_000 == 0
        self.day_high=max(self.day_high,c.high)
        self.day_low=min(self.day_low,c.low)
        is_hour=(c.ts+BAR)%HOUR==0
        old_hours=list(self.hours)
        if c.ts%HOUR==0:
            self.part=[]
        self.part.append(c)
        if is_hour:
            if len(self.part)==4 and self.part[0].ts%HOUR==0:
                h=Candle(self.part[0].ts,self.part[0].open,max(x.high for x in self.part),
                         min(x.low for x in self.part),c.close,sum(x.volume for x in self.part))
                self.h.update(h)
                self.fast=h.close if self.fast is None else self.fast+2/17*(h.close-self.fast)
                self.slow=h.close if self.slow is None else self.slow+2/65*(h.close-self.slow)
                self.hours.append(h)
            self.part=[]
        _,rsi=self.m.update(c)
        self.closes.append(c.close)
        prices=list(self.closes)[-20:]
        mean=fmean(prices)
        sd=pstdev(prices)
        er=0.0
        hs=list(self.hours)
        if len(hs)>=21:
            ps=[x.close for x in hs[-21:]]
            travel=sum(abs(b-a) for a,b in zip(ps,ps[1:]))
            er=abs(ps[-1]-ps[0])/travel if travel else 0.0
        # An hour just closed: its channel excludes that hour itself.
        channel=old_hours if is_hour else hs
        out={'hour':is_hour,'hours':channel,'rsi':rsi,'mean':mean,'sd':sd,'er':er,
             'previous':self.prev,'previous_mean':self.prev_mean,'previous_sd':self.prev_sd}
        self.prev=c
        self.prev_mean,self.prev_sd=mean,sd
        return out

class Approach:
    def __init__(self,config,name):
        if name not in EXPERIMENT_NAMES[1:]:raise ValueError('Unknown approach')
        self.config,self.name=config,name
        self.ctx=Context()

    def update(self,c):
        x=self.ctx; f=x.update(c)
        if x.h.count<100 or x.m.count<200:
            return Signal()
        atr=x.h.atr
        direction=1 if x.fast>x.slow else -1 if x.fast<x.slow else 0
        strength=abs(x.fast-x.slow)/atr if atr else 0
        prev=f['previous']
        er=f['er']
        h=f['hours']
        don=0
        if f['hour'] and len(h)>=20:
            hi=max(v.high for v in h[-20:]);lo=min(v.low for v in h[-20:])
            don=1 if c.close>hi else -1 if c.close<lo else 0
            if don != direction:don=0
        trend=direction if f['hour'] and strength>=.5 else 0
        pull=0
        if prev and er>=.30 and strength>=.5:
            if direction==1 and c.low<=x.m.fast<c.close and c.close>prev.close:pull=1
            elif direction==-1 and c.high>=x.m.fast>c.close and c.close<prev.close:pull=-1
        revert=0
        pm,ps=f['previous_mean'],f['previous_sd']
        if prev and pm is not None and ps and er<=.20 and f['sd']>0:
            if prev.close<pm-2*ps and c.close>f['mean']-2*f['sd'] and f['rsi']<45:revert=1
            elif prev.close>pm+2*ps and c.close<f['mean']+2*f['sd'] and f['rsi']>55:revert=-1
        vb=0
        if prev and x.previous_day_range:
            up=x.day_open+.5*x.previous_day_range
            down=x.day_open-.5*x.previous_day_range
            if prev.close<=up<c.close and direction==1:vb=1
            elif prev.close>=down>c.close and direction==-1:vb=-1
        side=0
        is_reversion=False
        if self.name=='donchian':side=don
        elif self.name=='ewmac':side=trend
        elif self.name=='pullback':side=pull
        elif self.name=='volatility_breakout':side=vb
        elif self.name=='range_reversion':side=revert;is_reversion=True
        elif self.name=='regime_composite':
            if er>=.30:side=don or pull or trend
            elif er<=.20:side=revert;is_reversion=True
        elif self.name=='consensus':
            if er>=.30:
                votes=[don,trend,pull]
                for d in (1,-1):
                    if votes.count(d)>=2 and -d not in votes:side=d
            elif er<=.20:side=revert;is_reversion=True
        distance=max(2*x.m.atr,.75*atr) if is_reversion else 2*atr
        # Shared completed-hour trend stop/exit; range module exits at its current mean.
        close_long=direction<0
        close_short=direction>0
        if self.name=='donchian' and len(h)>=10:
            close_long=c.close<min(v.low for v in h[-10:])
            close_short=c.close>max(v.high for v in h[-10:])
        if self.name=='range_reversion' or (self.name in ('regime_composite','consensus') and er<=.20):
            close_long=c.close>=f['mean']
            close_short=c.close<=f['mean']
        return Signal(side,distance,close_long,close_short,
                      max(.000001,c.close-3*atr),c.close+3*atr)


def factory(name):
    if name not in NAMES:raise ValueError('Unknown approach')
    if name=='baseline':return Strategy
    if name=='candidate_120h':
        from .frozen_candidate import FrozenCandidate
        return FrozenCandidate
    return lambda config:Approach(config,name)


def runtime_config(name):
    from dataclasses import replace
    from .config import Config
    if name not in NAMES:raise ValueError('Unknown approach')
    if name=='candidate_120h':return replace(Config(),reward_risk=4,max_bars=672)
    return Config() if name=='baseline' else replace(Config(),reward_risk=4,max_bars=192)
