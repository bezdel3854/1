from dataclasses import dataclass, asdict
import math

SYMBOLS = ('BTC-USDT', 'ETH-USDT')
BAR = 900_000
HOUR = 3_600_000

@dataclass(frozen=True)
class Config:
    deposit: float = 1000.0
    risk: float = .005
    portfolio_risk: float = .01
    leverage: float = 2.0
    notional_cap: float = 1.0  # each position <= 1x equity
    fee: float = .0005  # assumed taker EACH side; not a verified account tier
    slip: float = .0002  # adverse EACH side, plus actual spread in paper
    funding_reserve: float = .001  # budget reserve, not guaranteed funding cap
    daily_loss: float = .02
    max_drawdown: float = .10
    atr_multiple: float = 2.0
    reward_risk: float = 2.0
    max_bars: int = 32
    maintenance: float = .01  # conservative proxy, NOT exchange liquidation tiers
    max_spread: float = .001

    def __post_init__(self):
        if any(not math.isfinite(v) for v in asdict(self).values()):
            raise ValueError('Non-finite config')
        if not (self.deposit > 0 and 0 < self.risk <= .005 and
                self.risk <= self.portfolio_risk <= .01 and 1 <= self.leverage <= 2 and
                0 < self.notional_cap <= 1 and 0 <= self.fee <= .01 and
                0 <= self.slip <= .01 and 0 <= self.funding_reserve <= .01 and
                0 < self.daily_loss <= .02 and 0 < self.max_drawdown <= .1 and
                self.atr_multiple > 0 and self.reward_risk > 0 and self.max_bars > 0 and
                0 < self.maintenance < 1 / self.leverage and 0 < self.max_spread <= .01):
            raise ValueError('Unsafe config')
