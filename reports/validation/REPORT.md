# Проверка стратегии BingX

Данные BingX: 2026-07-28T00:00:00+00:00 — 2026-10-25T00:00:00+00:00 (конец не включён).
Отложенная проверка: 2026-09-25T00:00:00+00:00 — 2026-10-25T00:00:00+00:00. Параметры фиксированы до расчёта.

**Прибыльность не доказана. Реальные ордера не размещались.**

| Проверка | Сделок | Итог USDT | Доходность | Просадка open/close | PF |
|---|---:|---:|---:|---:|---:|
| full | 56 | 899.94 | -10.01% | 10.12% | 0.368 |
| development | 56 | 899.94 | -10.01% | 10.12% | 0.368 |
| holdout | 45 | 902.27 | -9.77% | 10.03% | 0.224 |
| holdout_cost_stress | 36 | 900.27 | -9.97% | 10.05% | 0.117 |

## Ограничения

- Research baseline; no evidence of sustainable profitability.
- OHLC simulation: cannot observe intra-candle event order; stop wins when stop and target both touch.
- Stops are not guaranteed: adverse gaps/slippage/funding can exceed the 0.5% planned budget.
- Liquidation uses 2x isolated-margin proxy with 1% maintenance and trade prices, not actual mark/tier/insurance rules.
- Drawdown is measured on bar opens/closes, not continuous intrabar equity; may understate real drawdown.
- Funding is actual settled rate and mark when provided. Funding charged before bar exits; entry at settlement excluded.
- Contract minimums are current snapshots, not historical rule versions; taker fee 5bp is assumed per side.
- Fixed slippage does not model historical order-book liquidity or latency. Paper is sampled quotes, not exchange execution.
- Fixed strategy and chronological holdout; no optimized parameters, no live performance or multi-year validation.
