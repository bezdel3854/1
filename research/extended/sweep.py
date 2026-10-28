"""Finite chronological search, canonical shared risk engine, no orders."""
import itertools,json,hashlib,time
from pathlib import Path
from dataclasses import asdict,replace
import numpy as np
import pandas as pd
from bingx_bot.config import Config,SYMBOLS,BAR
from bingx_bot.data import load_dataset
from bingx_bot.engine import backtest
from bingx_bot.strategy import Signal
from bingx_bot.report import metrics,write_csv
from bingx_bot.experiments import daily_returns,block_bootstrap

DAY=86_400_000
stamp=lambda s:int(pd.Timestamp(s,tz='UTC').timestamp()*1000)


def specs():
    out=[]
    def add(family,**params):
        out.append({'family':family,**params})
    for n,pair,stop,trail,r in itertools.product((12,24,72,120),((16,64),(32,128)),(2,3),(3,5),(4,8)):
        add('donchian',n=n,pair=pair,stop=stop,trail=trail,r=r)
    for pair,threshold,stop,trail,r in itertools.product(((8,32),(16,64),(32,128),(64,256)),(.5,1),(2,3),(3,5),(4,8)):
        add('ewmac',pair=pair,threshold=threshold,stop=stop,trail=trail,r=r)
    for pair,rsi,stop,trail,r in itertools.product(((16,64),(32,128)),(40,50),(2,3),(3,5),(2,4)):
        add('pullback',pair=pair,rsi=rsi,stop=stop,trail=trail,r=r)
    for n,z,er,stop,r in itertools.product((20,48),(1.5,2),(.2,.35),(1.5,2.5),(1,2)):
        add('range',n=n,z=z,er=er,stop=stop,trail=0,r=r,pair=(16,64))
    for fraction,pair,stop,trail,r in itertools.product((.3,.5),((16,64),(32,128)),(2,3),(3,5),(4,8)):
        add('volatility',fraction=fraction,pair=pair,stop=stop,trail=trail,r=r)
    for pair,depth,stop,trail,r in itertools.product(((16,64),(32,128)),(.5,1),(2,3),(3,5),(2,4)):
        add('trend_reversion',pair=pair,depth=depth,stop=stop,trail=trail,r=r)
    return [{'id':i,**s} for i,s in enumerate(out)]


def bank(rows):
    df=pd.DataFrame([asdict(c) for c in rows])
    df.index=pd.to_datetime(df.ts+BAR,unit='ms',utc=True)
    # Index is candle CLOSE. A bar closing 01:00 belongs to the hour ending 01:00.
    h=df.resample('1h',closed='right',label='right').agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum','ts':'count'})
    h=h[h.ts==4]
    tr=pd.concat([h.high-h.low,(h.high-h.close.shift()).abs(),(h.low-h.close.shift()).abs()],axis=1).max(axis=1)
    h['atr']=tr.ewm(alpha=1/14,adjust=False).mean()
    for n in (8,16,20,32,48,64,128,256):h['ema'+str(n)]=h.close.ewm(span=n,adjust=False).mean()
    for n in (12,24,72,120):
        h['hi'+str(n)]=h.high.shift().rolling(n).max()
        h['lo'+str(n)]=h.low.shift().rolling(n).min()
    for n in (20,48):
        h['mean'+str(n)]=h.close.rolling(n).mean()
        h['sd'+str(n)]=h.close.rolling(n).std(ddof=0)
    h['er']=h.close.diff(20).abs()/h.close.diff().abs().rolling(20).sum().replace(0,np.nan)
    # Causal completed-hour features forward-filled at decision timestamps.
    f=h.reindex(df.index,method='ffill')
    f['price']=df.close
    f['prev']=df.close.shift()
    f['hour']=df.index.minute==0
    f['low15']=df.low;f['high15']=df.high
    delta=df.close.diff();up=delta.clip(lower=0).ewm(alpha=1/14,adjust=False).mean();dn=(-delta.clip(upper=0)).ewm(alpha=1/14,adjust=False).mean()
    f['rsi']=100-100/(1+up/dn.replace(0,np.nan));f.loc[(dn==0)&(up>0),'rsi']=100;f['rsi']=f.rsi.fillna(50)
    # UTC daily open known at start; yesterday's complete range known only today.
    days=(df.ts//DAY).to_numpy()
    dayframe=df.groupby(days).agg({'open':'first','high':'max','low':'min'})
    previous=(dayframe.high-dayframe.low).shift()
    f['dayopen']=[dayframe.loc[d,'open'] for d in days]
    f['dayrange']=[previous.loc[d] for d in days]
    f['warm']=np.arange(len(df))>=1200
    return {k:f[k].to_numpy() for k in f.columns}


def decisions(f,s):
    price=f['price'];prev=f['prev'];atr=f['atr'];fast=f['ema'+str(s['pair'][0])];slow=f['ema'+str(s['pair'][1])]
    trend=np.sign(fast-slow);strength=np.abs(fast-slow)/atr
    hour=f['hour'];side=np.zeros(len(price),dtype=np.int8)
    close_l=trend<0;close_s=trend>0
    fam=s['family']
    if fam=='donchian':
        side=np.where(hour&(price>f['hi'+str(s['n'])])&(trend>0),1,np.where(hour&(price<f['lo'+str(s['n'])])&(trend<0),-1,0))
    elif fam=='ewmac':side=np.where(hour&(strength>=s['threshold']),trend,0)
    elif fam=='pullback':
        long=(trend>0)&(f['rsi']<s['rsi'])&(price>prev)&(f['er']>=.2)
        short=(trend<0)&(f['rsi']>100-s['rsi'])&(price<prev)&(f['er']>=.2)
        side=np.where(long,1,np.where(short,-1,0))
    elif fam=='range':
        mean=f['mean'+str(s['n'])];sd=f['sd'+str(s['n'])]
        lower=mean-s['z']*sd;upper=mean+s['z']*sd
        old_l=np.roll(lower,1);old_u=np.roll(upper,1)
        side=np.where((f['er']<=s['er'])&(prev<old_l)&(price>lower)&(price<mean),1,
                      np.where((f['er']<=s['er'])&(prev>old_u)&(price<upper)&(price>mean),-1,0))
        close_l=price>=mean;close_s=price<=mean
    elif fam=='volatility':
        upper=f['dayopen']+s['fraction']*f['dayrange'];lower=f['dayopen']-s['fraction']*f['dayrange']
        side=np.where((prev<=upper)&(price>upper)&(trend>0),1,np.where((prev>=lower)&(price<lower)&(trend<0),-1,0))
    elif fam=='trend_reversion':
        side=np.where((trend>0)&(price<fast-s['depth']*atr)&(price>prev),1,
                      np.where((trend<0)&(price>fast+s['depth']*atr)&(price<prev),-1,0))
        close_l=(price>=fast)|(trend<0);close_s=(price<=fast)|(trend>0)
    side=np.where(f['warm']&np.isfinite(atr)&(atr/price>=.0015),side,0)
    distance=s['stop']*atr
    tl=np.maximum(.000001,price-s['trail']*atr) if s['trail'] else None
    ts=price+s['trail']*atr if s['trail'] else None
    return side,distance,close_l,close_s,tl,ts


def replay_factory(arrays):
    symbols=iter(SYMBOLS)
    def make(config):
        data=arrays[next(symbols)]
        class Replay:
            def __init__(self):self.i=0
            def update(self,c):
                i=self.i;self.i+=1
                return Signal(int(data[0][i]),float(data[1][i]),bool(data[2][i]),bool(data[3][i]),
                              float(data[4][i]) if data[4] is not None and np.isfinite(data[4][i]) else None,
                              float(data[5][i]) if data[5] is not None and np.isfinite(data[5][i]) else None)
        return Replay()
    return make


def eval_spec(cs,fs,rs,features,s,start,end,stress=False,delay=0,artifacts=None):
    cfg=replace(Config(),max_bars=672,reward_risk=s['r'])
    if stress:cfg=replace(cfg,fee=.001,slip=.0005)
    arrays={sym:decisions(features[sym],s) for sym in SYMBOLS}
    b,eq=backtest(cs,fs,rs,cfg,start,end,replay_factory(arrays),delay)
    m=metrics(b,eq)
    if artifacts:
        artifacts=Path(artifacts);artifacts.mkdir(parents=True,exist_ok=True)
        write_csv(artifacts/'trades.csv',b.trades,list(b.trades[0]) if b.trades else ['symbol','net'])
        write_csv(artifacts/'equity.csv',eq,['ts','equity'])
    return m,eq


def score(m):return m['return_pct']-1.5*m['max_open_close_drawdown_pct']
def passed(m,n=40,pf=1.10):return m['net_usdt']>0 and m['completed_trades']>=n and (m['profit_factor'] or 0)>=pf and not m['risk_halted']


def search():
    out=Path('reports/extended');out.mkdir(parents=True,exist_ok=True)
    cs,fs,rs,manifest=load_dataset('data/proxy')
    features={s:bank(cs[s]) for s in SYMBOLS}
    attempts=specs();print('specifications',len(attempts),flush=True)
    log=[];pool=[]
    ledger=open(out/'trials.jsonl','w')
    for spec in attempts:
        m,_=eval_spec(cs,fs,rs,features,spec,stamp('2023-01-01'),stamp('2024-01-01'))
        row={'spec':spec,'training':m};log.append(row)
        ledger.write(json.dumps(row)+'\n');ledger.flush()
        if passed(m):pool.append(row)
        if spec['id']%16==0:print('trained',spec['id']+1,'passed',len(pool),flush=True)
    pool=sorted(pool,key=lambda x:score(x['training']),reverse=True)[:24]
    validation=[]
    for row in pool:
        m,_=eval_spec(cs,fs,rs,features,row['spec'],stamp('2024-01-01'),stamp('2025-01-01'))
        row['validation']=m
        if passed(m,30):validation.append(row)
        print('validated',row['spec']['id'],m['return_pct'],flush=True)
    finalists=[]
    for row in sorted(validation,key=lambda x:score(x['validation']),reverse=True)[:6]:
        m,_=eval_spec(cs,fs,rs,features,row['spec'],stamp('2024-01-01'),stamp('2025-01-01'),stress=True)
        row['validation_stress']=m
        if m['net_usdt']>0 and not m['risk_halted']:finalists.append(row)
    # Immutable choice saved BEFORE the 2025 test is evaluated.
    chosen=max(finalists,key=lambda x:score(x['validation_stress'])) if finalists else None
    freeze={'selected':chosen['spec'] if chosen else None,'training_candidates':len(attempts),
            'previous_trials':8,'protocol_sha256':hashlib.sha256(Path('research/extended/PROTOCOL.md').read_bytes()).hexdigest(),
            'frozen_at_ms':int(time.time()*1000),'selection_uses_final_2025':False}
    raw=json.dumps(freeze,sort_keys=True).encode();(out/'frozen.json').write_bytes(raw)
    result={'freeze_sha256':hashlib.sha256(raw).hexdigest(),'freeze':freeze,'profitability_proven':False,
            'source':manifest['source'],'all_training':log,'training_top24_validation':pool,
            'pretest_stress_candidates':validation,'finalist':None}
    if chosen:
        spec=chosen['spec'];final={}
        m,eq=eval_spec(cs,fs,rs,features,spec,stamp('2025-01-01'),stamp('2026-01-01'),artifacts=out/'final_2025')
        final['test_2025']=m
        final['adjusted_bootstrap']=block_bootstrap(daily_returns(eq),trials=len(attempts)+8,repetitions=10000)
        m,_=eval_spec(cs,fs,rs,features,spec,stamp('2025-01-01'),stamp('2026-01-01'),stress=True)
        final['test_2025_stress']=m
        m,_=eval_spec(cs,fs,rs,features,spec,stamp('2025-01-01'),stamp('2026-01-01'),delay=1)
        final['test_2025_latency']=m
        bcs,bfs,brs,_=load_dataset('data/history');bf={s:bank(bcs[s]) for s in SYMBOLS}
        begin=bcs[SYMBOLS[0]][0].ts+14*DAY;finish=bcs[SYMBOLS[0]][-1].ts+BAR
        m,_=eval_spec(bcs,bfs,brs,bf,spec,begin,finish,artifacts=out/'bingx_development')
        final['bingx_development']=m
        reasons=[]
        if not passed(final['test_2025'],40,1.15):reasons.append('final 2025 basic gate failed')
        if final['test_2025_stress']['net_usdt']<=0 or final['test_2025_stress']['risk_halted']:reasons.append('2025 cost stress failed')
        if final['bingx_development']['net_usdt']<=0 or final['bingx_development']['risk_halted']:reasons.append('BingX venue diagnostic failed')
        if (final['adjusted_bootstrap']['lower_return_pct'] or 0)<=0:reasons.append('multiple-trial adjusted confidence insufficient')
        final['rejection_reasons']=reasons;final['eligible_experimental_paper']=not reasons
        result['finalist']={'spec':spec,**final}
    ledger.close()
    (out/'results.json').write_text(json.dumps(result,indent=2,allow_nan=False))
    print('FINAL',json.dumps(result['finalist']),flush=True)
    return result

if __name__=='__main__':search()
