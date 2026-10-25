"""Fixed baseline, not optimized. Completed 1h candles filter 15m signals."""
from dataclasses import dataclass
from .data import Candle
from .config import HOUR

@dataclass(frozen=True)
class Signal:
    side: int = 0
    distance: float = 0

class Indicators:
    def __init__(self):
        self.count = 0
        self.fast = self.slow = self.last = None
        self.up = self.down = self.atr = 0.0

    def update(self, c):
        old = 0 if self.fast is None else self.fast - self.slow
        if self.last is None:
            self.fast = self.slow = c.close
            self.atr = c.high-c.low
        else:
            self.fast += 2/21 * (c.close-self.fast)
            self.slow += 2/51 * (c.close-self.slow)
            d = c.close-self.last
            self.up += (max(d,0)-self.up)/14
            self.down += (max(-d,0)-self.down)/14
            tr = max(c.high-c.low, abs(c.high-self.last),abs(c.low-self.last))
            self.atr += (tr-self.atr)/14
        self.last = c.close
        self.count += 1
        rsi = 50 if self.up == self.down == 0 else (100 if self.down == 0 else 100-100/(1+self.up/self.down))
        cross = 1 if old <= 0 < self.fast-self.slow else (-1 if old >= 0 > self.fast-self.slow else 0)
        return cross, rsi

class Strategy:
    def __init__(self, config):
        self.config = config
        self.m15, self.h1 = Indicators(), Indicators()
        self.hour = []
        self.last_ts = None

    def update(self, c):
        if self.last_ts is not None and c.ts-self.last_ts != 900_000:
            raise ValueError('Strategy history gap')
        self.last_ts = c.ts
        # Drop a partial first hour. Never use a still-forming hour.
        if c.ts % HOUR == 0:
            self.hour = []
        self.hour.append(c)
        if (c.ts+900_000) % HOUR == 0:
            if len(self.hour) == 4 and self.hour[0].ts % HOUR == 0:
                h = Candle(self.hour[0].ts,self.hour[0].open,max(x.high for x in self.hour),
                           min(x.low for x in self.hour),c.close,sum(x.volume for x in self.hour))
                self.h1.update(h)
            self.hour = []
        cross, rsi = self.m15.update(c)
        side = 0
        if self.m15.count >= 200 and self.h1.count >= 100:
            if cross == 1 and 50 <= rsi <= 70 and self.h1.fast > self.h1.slow:
                side = 1
            elif cross == -1 and 30 <= rsi <= 50 and self.h1.fast < self.h1.slow:
                side = -1
        return Signal(side, self.m15.atr*self.config.atr_multiple)
