"""Post-freeze BingX historical replay. NOT actual live/paper fills or proof of profit."""
from dataclasses import asdict
from datetime import datetime,timezone
from pathlib import Path
import gzip,hashlib,json
from bingx_bot.config import BAR,SYMBOLS
from bingx_bot.data import PublicClient,save_dataset
from bingx_bot.approaches import factory,runtime_config
from bingx_bot.engine import backtest
from bingx_bot.report import metrics,write_csv


def check():
    frozen=json.loads(Path('reports/extended/frozen.json').read_text())
    begin=(frozen['frozen_at_ms']//BAR+1)*BAR
    first=begin-4096*BAR
    api=PublicClient();now=api.now();end=now//BAR*BAR
    if end<=begin:raise ValueError('No new closed candles after frozen specification')
    rules,contracts=api.rules();candles={};funding={}
    for s in SYMBOLS:
        candles[s]=api.candles(s,first,end)
        funding[s]=api.funding(s,first,end)
        print(json.dumps({'symbol':s,'candles':len(candles[s]),'funding':len(funding[s])}),flush=True)
    dest=Path('data/post-freeze')
    save_dataset(dest,candles,funding,rules,{'purpose':'newly captured post-freeze diagnostic; not live fills',
        'frozen_at_ms':frozen['frozen_at_ms'],'evaluation_start':begin,'evaluation_end':end,
        'contracts_snapshot':[x for x in contracts if x['symbol'] in SYMBOLS]})
    for p in dest.glob('*-USDT.json'):
        p.with_suffix('.json.gz').write_bytes(gzip.compress(p.read_bytes(),mtime=0));p.unlink()
    from .forward_snapshot import pack
    pack(snapshot=dest)
    cfg=runtime_config('candidate_120h')
    broker,equity=backtest(candles,funding,rules,cfg,begin,end,factory('candidate_120h'))
    out=Path('reports/post-freeze');out.mkdir(parents=True,exist_ok=True)
    m=metrics(broker,equity)
    result={'mode':'historical replay after freeze, NOT live fills','strategy':'candidate_120h',
            'parameter_changes':False,'source':'BingX public USDT perpetual data',
            'frozen_at_ms':frozen['frozen_at_ms'],'start':begin,'end':end,'captured_at_ms':now,
            'hours':(end-begin)/3_600_000,'config':asdict(cfg),'metrics':m,
            'profitability_proven':False,'paper_entries_promoted':False,
            'limitation':'Short new-data diagnostic; replay assumes next-open fills and forces any remaining position closed at the end. No live exchange or paper fills occurred.'}
    (out/'results.json').write_text(json.dumps(result,indent=2,allow_nan=False))
    write_csv(out/'trades.csv',broker.trades,list(broker.trades[0]) if broker.trades else ['symbol','net'])
    write_csv(out/'equity.csv',equity,['ts','equity'])
    def fmt(t):return datetime.fromtimestamp(t/1000,timezone.utc).isoformat()
    lines=['# Новые BingX-данные после фиксации кандидата','',
           '**Это историческое воспроизведение на новых данных, не реальные или paper-исполнения.**','',
           f"Параметры фиксированы: {fmt(frozen['frozen_at_ms'])}. Проверка: {fmt(begin)} — {fmt(end)}.",
           f"Длина: {result['hours']:.2f} часа. Начальный счёт: 1000 USDT. Итог: {m['final_usdt']:.2f} USDT ({m['return_pct']:+.3f}%). Закрытых сделок: {m['completed_trades']}.",
           '', 'Учтены комиссии, funding, проскальзывание и прежний контроль риска. Открытая позиция в конце закрывается моделью для оценки; это не наблюдавшаяся фактическая сделка.',
           'Выборка слишком короткая для подтверждения доходности. Параметры не менялись, новые виртуальные входы автоматически не включались. Railway-сервиса пока нет.']
    (out/'REPORT.md').write_text('\n'.join(lines)+'\n')
    print(json.dumps(result,indent=2,allow_nan=False),flush=True)
    return result

if __name__=='__main__':check()
