"""Fixed family comparison. All existing bars are development, never fresh OOS."""
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean
import json
import random
import hashlib
import math
from .config import Config, SYMBOLS
from .approaches import NAMES, factory
from .data import load_dataset
from .engine import backtest
from .report import metrics, write_csv, LIMITATIONS


def daily_returns(equity,deposit=1000):
    daily={}
    for row in equity:
        # ts marks bar END; last bar 00:00 belongs to prior trading day.
        daily[(row['ts']-1)//86_400_000]=row['equity']
    previous=deposit
    returns=[]
    for day,value in sorted(daily.items()):
        returns.append(value/previous-1)
        previous=value
    return returns


def block_bootstrap(returns, trials=8, repetitions=2000):
    if len(returns)<20:return {'lower_return_pct':None,'days':len(returns)}
    rng=random.Random(20261025)
    blocks=[returns[i:i+5] for i in range(0,len(returns),5)]
    sampled=[]
    for _ in range(repetitions):
        values=[]
        while len(values)<len(returns):values.extend(rng.choice(blocks))
        sampled.append((math.prod(1+x for x in values[:len(returns)])-1)*100)
    sampled.sort()
    return {'lower_return_pct':sampled[int((.05/trials)*(repetitions-1))],
            'upper_return_pct':sampled[int(.95*(repetitions-1))],
            'familywise_alpha':.05,'trials':trials,'block_days':5,'days':len(returns),
            'repetitions':repetitions,'seed':20261025,
            'warning':'Approximate dependent-return bootstrap; multiple tests accounted by Bonferroni. Not a profitability proof.'}


def correlation(a,b):
    if len(a)!=len(b):raise ValueError('Unaligned return series')
    am,bm=fmean(a),fmean(b)
    cov=sum((x-am)*(y-bm) for x,y in zip(a,b))
    norm=math.sqrt(sum((x-am)**2 for x in a)*sum((y-bm)**2 for y in b))
    return cov/norm if norm else None


def gate(runs,bootstrap):
    full=runs['full']
    reasons=[]
    if full['risk_halted']:reasons.append('global drawdown halt')
    if runs['early']['net_usdt']<=0:reasons.append('early development loss')
    if runs['late']['net_usdt']<=0:reasons.append('late development loss')
    if runs['late_cost_stress']['net_usdt']<=0:reasons.append('late cost-stress loss')
    if full['completed_trades']<50:reasons.append('fewer than 50 full-period trades')
    if full['profit_factor'] is None or full['profit_factor']<1.15:reasons.append('PF below 1.15 or undefined')
    if bootstrap['lower_return_pct'] is None or bootstrap['lower_return_pct']<=0:reasons.append('adjusted bootstrap lower bound not positive')
    return {'eligible_experimental_paper':not reasons,'rejection_reasons':reasons,'profitability_proven':False}


def run_experiments(data='data/history',out='reports/approaches'):
    candles,funding,rules,manifest=load_dataset(data)
    first=candles[SYMBOLS[0]][0].ts
    end=candles[SYMBOLS[0]][-1].ts+900_000
    start=first+7*86_400_000
    split=end-30*86_400_000
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    plan=Path('research/EXPERIMENT_PLAN.md').read_bytes()
    result={'profitability_proven':False,'data_status':'PREVIOUSLY_INSPECTED_DEVELOPMENT_DATA',
            'plan_sha256':hashlib.sha256(plan).hexdigest(),'dataset_sha256':manifest['sha256'],
            'start':start,'split':split,'end':end,'total_candidates':len(NAMES),'candidates':{},
            'limitations':[x for x in LIMITATIONS if not x.startswith('Fixed strategy')]+['No fresh holdout remains after prior use of the dataset.',
             'BTC and ETH are correlated. Component votes and approach returns are not independent.',
             'Famous traders returns cannot be inherited by reproducing simplified rules.']}
    returns={}
    for name in NAMES:
        config=Config() if name=='baseline' else replace(Config(),reward_risk=4,max_bars=192)
        cases=[('full',config,start,end,0),('early',config,start,split,0),('late',config,split,end,0),
               ('late_cost_stress',replace(config,fee=.001,slip=.0005),split,end,0),
               ('late_extra_bar_latency',config,split,end,1)]
        runs={}
        for label,cfg,begin,finish,delay in cases:
            broker,eq=backtest(candles,funding,rules,cfg,begin,finish,factory(name),delay)
            runs[label]=metrics(broker,eq)
            if label=='full':
                returns[name]=daily_returns(eq)
                write_csv(out/f'{name}_trades.csv',broker.trades,list(broker.trades[0]) if broker.trades else ['symbol','net'])
                write_csv(out/f'{name}_equity.csv',eq,['ts','equity'])
        segments=[]
        for begin,finish in [(start,start+28*86_400_000),(start+28*86_400_000,split),(split,end)]:
            broker,eq=backtest(candles,funding,rules,config,begin,finish,factory(name))
            segments.append({'start':begin,'end':finish,**metrics(broker,eq)})
        boot=block_bootstrap(returns[name])
        result['candidates'][name]={'config':asdict(config),'runs':runs,'segments':segments,
                                    'bootstrap':boot,'gate':gate(runs,boot)}
        print(json.dumps({'name':name,'full_return_pct':runs['full']['return_pct'],
              'late_return_pct':runs['late']['return_pct'],'trades':runs['full']['completed_trades'],
              'gate':result['candidates'][name]['gate']}),flush=True)
    result['correlations']={a:{b:correlation(returns[a],returns[b]) for b in NAMES} for a in NAMES}
    eligible=[name for name in NAMES if result['candidates'][name]['gate']['eligible_experimental_paper']]
    result['eligible_experimental_paper']=eligible
    # Conservative overall gate: no auto-selection even if development gates pass.
    result['automatic_promotion']=False
    result['next_validation']='Freeze exact candidate specification, collect NEW data, then forward paper.'
    (out/'results.json').write_text(json.dumps(result,indent=2,allow_nan=False))
    lines=['# Проверка подходов и объединённой системы','',
           '**Это уже просмотренные данные разработки. Плюсовой бэктест не доказывает прибыльность.**','',
           f'Проверено {len(NAMES)} заранее зафиксированных подходов без перебора сетки параметров.',
           'Счёт 1000 USDT, риск до 0,5% на вход, общий риск до 1%, BTC+ETH, комиссии/funding/проскальзывание.',
           '', '| Подход | Весь период | Ранний | Поздний | Поздний: стресс издержек | Сделок | PF | Paper gate |',
           '|---|---:|---:|---:|---:|---:|---:|---|']
    for name,c in result['candidates'].items():
        r=c['runs'];pf=r['full']['profit_factor']
        pf='—' if pf is None else f'{pf:.2f}'
        vals=' | '.join(f"{r[k]['return_pct']:+.2f}%" for k in ['full','early','late','late_cost_stress'])
        lines.append(f"| {name} | {vals} | {r['full']['completed_trades']} | {pf} | {'PASS (experimental)' if c['gate']['eligible_experimental_paper'] else 'FAIL'} |")
    lines+=['','## Решение','',f'Прошли предварительный фильтр: {", ".join(eligible) if eligible else "никто"}.',
            'Автоматическое продвижение не разрешено. Новые стратегии по умолчанию работают в режиме наблюдения.',
            'Сделки CSV, equity и полные метрики сохранены рядом. В results.json также есть месячные сегменты, задержка входа, bootstrap и корреляции.',
            '', '## Причины отклонения','']
    for name,c in result['candidates'].items():lines.append(f"- {name}: {', '.join(c['gate']['rejection_reasons']) or 'только экспериментальный paper, без доказательства прибыли'}.")
    lines+=['','## Что именно объединено','',
            'regime_composite: на выраженном часовом тренде допускаются пробой/откат/направление EWMAC; в диапазоне — возврат в Bollinger; при промежуточном режиме новые входы отключены. Правила выхода следуют текущему режиму, а не сохраняют режим входа. Один общий брокер применяет лимиты к BTC и ETH.',
            'consensus: в тренде нужны два согласованных голоса из трёх, в диапазоне используется возврат к среднему. Веса не обучались на результатах. Сходные голоса не считаются независимыми подтверждениями.',
            '', '## Ограничения','']+['- '+s for s in result['limitations']]
    (out/'REPORT.md').write_text('\n'.join(lines)+'\n')
    return result
