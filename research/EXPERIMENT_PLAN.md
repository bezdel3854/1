# Fixed experiment plan — 25 October 2026

Registered before computing new-strategy returns. The earlier EMA/RSI baseline and all its 89-day data have already been inspected: this dataset is DEVELOPMENT DATA, including the former 30-day holdout. No result on it can be called a fresh out-of-sample discovery.

Universe: BingX BTC-USDT + ETH-USDT perpetuals; common 1,000 USDT; risk <=0.5% per entry and <=1% total; 2x isolated proxy; no martingale, averaging down or pyramiding. Same fees/funding/data and execution engine. Only completed 15m and 1h bars.

Fixed candidates (no grid search or iterative tuning after seeing results):

1. baseline EMA20/50 RSI (existing control, unchanged).
2. donchian: 20 completed-hour channel breakout, direction EMA16/64 1h; 10-hour channel exit, 3-hour-ATR trail.
3. ewmac: volatility-normalized EMA16/64 completed-hour direction, threshold abs(fast-slow)/ATR14 >=0.5; exit sign reversal, 3-hour-ATR trail.
4. pullback: EMA16/64 hourly trend with efficiency ratio >=0.30; 15m pullback through EMA20 and recovery; 2-hour-ATR stop, hourly trend exit/trail. An independent adaptation, not a replication of Raschke's proprietary or Holy Grail system.
5. range_reversion: hourly efficiency <=0.20; 15m Bollinger20/2 excursion followed by re-entry, RSI <45 for long / >55 short; exit at 15m mean, stop 2 ATR15m with minimum 0.75 ATR1h.
6. volatility_breakout: current UTC day open +/-0.5 prior UTC-day range; close crossing, hourly direction filter; 2 ATR1h stop, hourly reversal exit/trail. This is our Williams-inspired adaptation, NOT his championship account.
7. regime_composite: when efficiency >=0.30, use hourly trend and 15m pullbacks/breakouts; when <=0.20, use range reversion; intermediate regime remains flat for new entries. Disagreement prevents entry. No allocation fitted to outcomes.
8. consensus: at least two of donchian/ewmac/pullback agree, none opposes; range_reversion only in range regime. Fixed consensus weights; no claim of independent votes.

For candidates 2–8: 2 ATR1h default initial stop (range may use its own shorter distance), reward/risk 4, max hold 192x15m (48h), trailing based exclusively on previous completed bars and active next bar. Baseline retains its original exit config.

Evaluation: warmup 7 days; chronological early 52-day / late 30-day diagnostic; full development period; three forward segments resetting capital; cost stress fee 10bp / slippage 5bp; one-bar entry delay; non-overlapping 5-day block bootstrap of daily net equity returns with fixed seed and familywise lower quantile across eight tried models (diagnostic, not proof).

Paper promotion gate fixed in advance: no global risk halt; positive early and late diagnostic returns; positive stressed late return; >=50 closed full-period trades; PF>=1.15; positive familywise-adjusted lower bootstrap bound. Passing these gates only makes an experimental paper candidate. Profitability still requires new data and forward paper evidence. If no candidate qualifies, keep new-strategy paper mode observation-only by default.

All tried candidates, rejected candidates, source limitations and correlations will be reported. No cherry-picked winner will be called profitable.
