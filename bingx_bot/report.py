import csv
import json
from pathlib import Path
from dataclasses import asdict, replace
from datetime import datetime, timezone
from .engine import backtest
from .config import Config, SYMBOLS
from .data import load_dataset

LIMITATIONS = [
 'Research baseline; no evidence of sustainable profitability.',
 'OHLC simulation: cannot observe intra-candle event order; stop wins when stop and target both touch.',
 'Stops are not guaranteed: adverse gaps/slippage/funding can exceed the 0.5% planned budget.',
 'Liquidation uses 2x isolated-margin proxy with 1% maintenance and trade prices, not actual mark/tier/insurance rules.',
 'Drawdown is measured on bar opens/closes, not continuous intrabar equity; may understate real drawdown.',
 'Funding is actual settled rate and mark when provided. Funding charged before bar exits; entry at settlement excluded.',
 'Contract minimums are current snapshots, not historical rule versions; taker fee 5bp is assumed per side.',
 'Fixed slippage does not model historical order-book liquidity or latency. Paper is sampled quotes, not exchange execution.',
 'Fixed strategy and chronological holdout; no optimized parameters, no live performance or multi-year validation.',
]

def metrics(broker, equity):
    t = broker.trades
    wins = sum(x['net'] > 0 for x in t)
    gain = sum(max(0,x['net']) for x in t)
    loss = -sum(min(0,x['net']) for x in t)
    return {'initial_usdt':broker.config.deposit, 'final_usdt':broker.cash,
            'net_usdt':broker.cash-broker.config.deposit,
            'return_pct':(broker.cash/broker.config.deposit-1)*100,
            'completed_trades':len(t),'winrate_pct':wins/len(t)*100 if t else None,
            'profit_factor':gain/loss if loss else None,
            'max_open_close_drawdown_pct':broker.max_dd*100,
            'fees_usdt':sum(x['entry_fee']+x['exit_fee'] for x in t),
            'funding_paid_net_usdt':sum(x['funding'] for x in t),
            'per_symbol':{s:{'trades':sum(x['symbol']==s for x in t),
                             'net_usdt':sum(x['net'] for x in t if x['symbol']==s)} for s in SYMBOLS},
            'risk_halted':broker.halted,'equity_samples':len(equity)}

def write_csv(path, rows, fields):
    with open(path,'w',newline='') as f:
        writer=csv.DictWriter(f,lineterminator="\n",fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def research(data_path, out, config=Config()):
    candles,funding,rules,manifest = load_dataset(data_path)
    first = candles[SYMBOLS[0]][0].ts
    end = candles[SYMBOLS[0]][-1].ts+900_000
    warmup = 7*86400000
    if end-first < 60*86400000:
        raise ValueError('Research needs >=60d for warmup and chronological holdout')
    cutoff = end-30*86400000  # selected before seeing returns
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    result={'config':asdict(config),'data_manifest':manifest,'start_ms':first,'end_ms':end,
            'holdout_start_ms':cutoff,'limitations':LIMITATIONS,'profitability_proven':False,'runs':{}}
    cases = [('full',config,first+warmup,end),('development',config,first+warmup,cutoff),
             ('holdout',config,cutoff,end),
             ('holdout_cost_stress',replace(config,fee=.001,slip=.0005),cutoff,end)]
    for name,cfg,start,finish in cases:
        broker,eq=backtest(candles,funding,rules,cfg,start,finish)
        result['runs'][name]=metrics(broker,eq)
        write_csv(out/f'{name}_trades.csv',broker.trades,list(broker.trades[0]) if broker.trades else ['symbol','net'])
        write_csv(out/f'{name}_equity.csv',eq,['ts','equity'])
    # Independent monthly forward segments, fixed parameters, reset capital each segment.
    result['forward_segments']=[]
    start=first+warmup
    while start+14*86400000 <= end:
        finish=min(start+30*86400000,end)
        broker,eq=backtest(candles,funding,rules,config,start,finish)
        result['forward_segments'].append({'start_ms':start,'end_ms':finish,**metrics(broker,eq)})
        start=finish
    (out/'results.json').write_text(json.dumps(result,indent=2,allow_nan=False))
    def date(ts):return datetime.fromtimestamp(ts/1000,timezone.utc).isoformat()
    lines=['# Проверка стратегии BingX','',f'Данные BingX: {date(first)} — {date(end)} (конец не включён).',
           f'Отложенная проверка: {date(cutoff)} — {date(end)}. Параметры фиксированы до расчёта.',
           '', '**Прибыльность не доказана. Реальные ордера не размещались.**','',
           '| Проверка | Сделок | Итог USDT | Доходность | Просадка open/close | PF |',
           '|---|---:|---:|---:|---:|---:|']
    for name,m in result['runs'].items():
        pf='—' if m['profit_factor'] is None else f"{m['profit_factor']:.3f}"
        lines.append(f"| {name} | {m['completed_trades']} | {m['final_usdt']:.2f} | {m['return_pct']:.2f}% | {m['max_open_close_drawdown_pct']:.2f}% | {pf} |")
    lines+=['','## Ограничения','']+['- '+x for x in LIMITATIONS]
    (out/'REPORT.md').write_text('\n'.join(lines)+'\n')
    return result
