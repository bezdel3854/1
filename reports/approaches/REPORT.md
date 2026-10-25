# Проверка подходов и объединённой системы

**Это уже просмотренные данные разработки. Плюсовой бэктест не доказывает прибыльность.**

Проверено 8 заранее зафиксированных подходов без перебора сетки параметров.
Счёт 1000 USDT, риск до 0,5% на вход, общий риск до 1%, BTC+ETH, комиссии/funding/проскальзывание.

| Подход | Весь период | Ранний | Поздний | Поздний: стресс издержек | Сделок | PF | Paper gate |
|---|---:|---:|---:|---:|---:|---:|---|
| baseline | -10.01% | -10.01% | -9.77% | -9.97% | 56 | 0.37 | FAIL |
| donchian | -1.34% | +4.61% | -5.84% | -7.44% | 107 | 0.95 | FAIL |
| ewmac | -0.10% | -0.10% | -10.15% | -10.05% | 85 | 1.00 | FAIL |
| pullback | -0.07% | -0.43% | +0.37% | -1.05% | 76 | 1.00 | FAIL |
| range_reversion | -9.32% | -9.32% | -9.64% | -9.91% | 101 | 0.41 | FAIL |
| volatility_breakout | -5.04% | -2.63% | -2.46% | -3.89% | 82 | 0.75 | FAIL |
| regime_composite | -9.91% | -9.91% | -9.88% | -9.94% | 92 | 0.31 | FAIL |
| consensus | -9.89% | -9.89% | -9.70% | -9.98% | 99 | 0.36 | FAIL |

## Решение

Прошли предварительный фильтр: никто.
Автоматическое продвижение не разрешено. Новые стратегии по умолчанию работают в режиме наблюдения.
Сделки CSV, equity и полные метрики сохранены рядом. В results.json также есть месячные сегменты, задержка входа, bootstrap и корреляции.

## Причины отклонения

- baseline: global drawdown halt, early development loss, late development loss, late cost-stress loss, PF below 1.15 or undefined, adjusted bootstrap lower bound not positive.
- donchian: late development loss, late cost-stress loss, PF below 1.15 or undefined, adjusted bootstrap lower bound not positive.
- ewmac: global drawdown halt, early development loss, late development loss, late cost-stress loss, PF below 1.15 or undefined, adjusted bootstrap lower bound not positive.
- pullback: early development loss, late cost-stress loss, PF below 1.15 or undefined, adjusted bootstrap lower bound not positive.
- range_reversion: global drawdown halt, early development loss, late development loss, late cost-stress loss, PF below 1.15 or undefined, adjusted bootstrap lower bound not positive.
- volatility_breakout: early development loss, late development loss, late cost-stress loss, PF below 1.15 or undefined, adjusted bootstrap lower bound not positive.
- regime_composite: global drawdown halt, early development loss, late development loss, late cost-stress loss, PF below 1.15 or undefined, adjusted bootstrap lower bound not positive.
- consensus: global drawdown halt, early development loss, late development loss, late cost-stress loss, PF below 1.15 or undefined, adjusted bootstrap lower bound not positive.

## Что именно объединено

regime_composite: на выраженном часовом тренде допускаются пробой/откат/направление EWMAC; в диапазоне — возврат в Bollinger; при промежуточном режиме новые входы отключены. Правила выхода следуют текущему режиму, а не сохраняют режим входа. Один общий брокер применяет лимиты к BTC и ETH.
consensus: в тренде нужны два согласованных голоса из трёх, в диапазоне используется возврат к среднему. Веса не обучались на результатах. Сходные голоса не считаются независимыми подтверждениями.

## Ограничения

- Research baseline; no evidence of sustainable profitability.
- OHLC simulation: cannot observe intra-candle event order; stop wins when stop and target both touch.
- Stops are not guaranteed: adverse gaps/slippage/funding can exceed the 0.5% planned budget.
- Liquidation uses 2x isolated-margin proxy with 1% maintenance and trade prices, not actual mark/tier/insurance rules.
- Drawdown is measured on bar opens/closes, not continuous intrabar equity; may understate real drawdown.
- Funding is actual settled rate and mark when provided. Funding charged before bar exits; entry at settlement excluded.
- Contract minimums are current snapshots, not historical rule versions; taker fee 5bp is assumed per side.
- Fixed slippage does not model historical order-book liquidity or latency. Paper is sampled quotes, not exchange execution.
- No fresh holdout remains after prior use of the dataset.
- BTC and ETH are correlated. Component votes and approach returns are not independent.
- Famous traders returns cannot be inherited by reproducing simplified rules.
