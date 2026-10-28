# Extended search protocol — fixed before new returns

Authority: user authorized further strategy search; real orders remain forbidden. No increase of risk, leverage, or drawdown limits. No martingale or doubling.

Prior trials: eight models on 89 BingX days, all inspected and failed. Those bars remain development/venue diagnostics and are NOT an independent holdout.

New data: official Binance USD-M 15m BTCUSDT/ETHUSDT public archives, Dec 2022–Dec 2025; 1h features constructed solely from completed 15m bars. This is a CROSS-VENUE RESEARCH PROXY, not BingX execution history. Original ZIPs checked against provider SHA256. Funding historical rates included; marks proxied by contemporaneous candle open, settlement phase <=1s rounded down. These approximations must be disclosed.

Chronological protocol:
- 2022 December indicator warmup only.
- 2023 training, 2024 validation.
- 2025 sealed final year, evaluated only for the single frozen finalist (or none if train/validation fail). Do not change parameters after revealing it.
- Old BingX 89-day dataset checked only after finalist frozen; remains previously inspected venue diagnostic.

Finite search space: six strategy families, EMA/filter lookbacks, ATR initial/trailing widths, and targets as enumerated by `specs()` in `sweep.py`. No time-of-week, individual dates, or asset-specific parameter fitting. Same parameters BTC and ETH. Costs 5bp taker +2bp adverse each side; stress 10bp +5bp. 0.5% planned entry risk, 1% shared risk, 2x proxy leverage, daily stop 2%, peak drawdown stop 10%; 7-day maximum hold. This is not unlimited searching until a positive number appears.

Screening uses the full existing canonical risk/execution engine and fixed cached feature rules, never a frictionless vector PnL shortcut. All attempted parameter vectors and results logged. Training stage requires positive return, >=40 trades, PF>=1.10, no global halt. Rank top 24 by return minus 1.5*drawdown (using training only). Evaluate these on 2024 validation; require positive net, >=30 trades, PF>=1.10, no global halt. Check 2024 doubled-cost stress for up to top 6 validation candidates, require positive net and no halt. Freeze the best stress-score candidate once. No re-selection after final year or BingX diagnostic.

A frozen finalist must pass 2025 net positive, PF>=1.15, >=40 trades, no global risk halt, and positive doubled-cost result; then also positive no-halt BingX development diagnostic. Final reporting also shows concentration by asset, quarterly/yearly returns, latency stress and a 5-day block bootstrap lower bound adjusted for the FULL trial count including the previous 8. A point estimate or ordinary bootstrap interval after a large sweep is not proof of profit. Do not label selection-on-development as fresh validation.

If no finalist passes, retain observation-only mode and publish the failed search, not a fabricated profitable bot. If one passes the mechanical gates but adjusted statistical confidence or forward data is insufficient, label it experimental, never proven or guaranteed.
