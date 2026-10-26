"""Frozen 120h candidate, NOT proven profitable. Pure stdlib paper implementation.
Chosen on 2023/2024 proxy data before revealing 2025; spec id 56.
"""
from collections import deque
from .config import BAR,HOUR
from .data import Candle
from .strategy import Signal,Indicators

SPEC={'family':'donchian','id':56,'n':120,'pair':[32,128],'stop':2,'trail':3,'r':4}

class FrozenCandidate:
    warmup_bars=4096  # live initialization; 1024 completed hours for EMA128

    def __init__(self,config):
        self.config=config
        self.hours=deque(maxlen=120)
        self.part=[]
        self.h=Indicators()
        self.fast=self.slow=None
        self.last_ts=None
        self.count=0

    def update(self,c):
        if self.last_ts is not None and c.ts-self.last_ts!=BAR:raise ValueError('Candidate history gap')
        self.last_ts=c.ts;self.count+=1
        if c.ts%HOUR==0:self.part=[]
        self.part.append(c)
        complete=(c.ts+BAR)%HOUR==0
        high=max((x.high for x in self.hours),default=float('inf'))
        low=min((x.low for x in self.hours),default=0)
        ready=len(self.hours)==120
        if complete:
            if len(self.part)==4 and self.part[0].ts%HOUR==0:
                h=Candle(self.part[0].ts,self.part[0].open,max(x.high for x in self.part),
                         min(x.low for x in self.part),c.close,sum(x.volume for x in self.part))
                self.h.update(h)
                self.fast=h.close if self.fast is None else self.fast+2/33*(h.close-self.fast)
                self.slow=h.close if self.slow is None else self.slow+2/129*(h.close-self.slow)
                self.hours.append(h)
            self.part=[]
        if self.count<1201 or self.fast is None:return Signal()
        direction=1 if self.fast>self.slow else -1 if self.fast<self.slow else 0
        side=0
        if complete and ready and self.h.atr/c.close>=.0015:
            if c.close>high and direction>0:side=1
            elif c.close<low and direction<0:side=-1
        return Signal(side,2*self.h.atr,direction<0,direction>0,
                      max(.000001,c.close-3*self.h.atr),c.close+3*self.h.atr)
