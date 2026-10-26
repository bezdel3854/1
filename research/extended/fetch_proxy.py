"""Official Binance USD-M archive used ONLY as cross-venue research proxy."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
import calendar,csv,gzip,hashlib,io,json,time,urllib.request,zipfile
from bingx_bot.data import Candle, Funding, validate
from bingx_bot.config import SYMBOLS, BAR

ROOT='https://data.binance.vision/data/futures/um/monthly'

def fetch(url, path):
    for attempt in range(4):
        try:
            if path.exists():raw=path.read_bytes()
            else:
                raw=urllib.request.urlopen(url,timeout=25).read()
                check=urllib.request.urlopen(url+'.CHECKSUM',timeout=25).read().decode().split()[0]
                if hashlib.sha256(raw).hexdigest()!=check:raise ValueError('Archive checksum mismatch')
                path.write_bytes(raw)
                path.with_suffix('.sha256').write_text(check)
            if hashlib.sha256(raw).hexdigest()!=path.with_suffix('.sha256').read_text().strip():raise ValueError('Cached checksum mismatch')
            return raw
        except Exception:
            if attempt==3:raise
            time.sleep(2**attempt)


def capture(root='data/proxy-archive'):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    months=[(2022,12)]+[(y,m) for y in (2023,2024,2025) for m in range(1,13)]
    jobs=[]
    for symbol in SYMBOLS:
        code=symbol.replace('-','')
        for year,month in months:
            for kind in ('klines','fundingRate'):
                name=f'{code}-15m-{year}-{month:02d}.zip' if kind=='klines' else f'{code}-fundingRate-{year}-{month:02d}.zip'
                url=f'{ROOT}/{kind}/{code}/'+('15m/' if kind=='klines' else '')+name
                jobs.append((symbol,kind,url,root/name))
    results={s:{'klines':[],'fundingRate':[]} for s in SYMBOLS}
    sources=[]
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures={pool.submit(fetch,url,path):(symbol,kind,url,path) for symbol,kind,url,path in jobs}
        for i,f in enumerate(as_completed(futures)):
            symbol,kind,url,path=futures[f];raw=f.result()
            z=zipfile.ZipFile(io.BytesIO(raw));names=z.namelist()
            if len(names)!=1:raise ValueError('Unexpected archive members')
            rows=list(csv.DictReader(io.StringIO(z.read(names[0]).decode())))
            results[symbol][kind].extend(rows)
            sources.append({'url':url,'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)})
            if i%20==0:print('archives',i+1,'/',len(jobs),flush=True)
    out=Path('data/proxy');out.mkdir(parents=True,exist_ok=True)
    hashes={};summary={}
    for s in SYMBOLS:
        rows=sorted(results[s]['klines'],key=lambda x:int(x['open_time']))
        cs=[Candle(int(x['open_time']),*(float(x[k]) for k in ('open','high','low','close','volume'))) for x in rows]
        validate(cs)
        funding=[];rounding=0
        for x in sorted(results[s]['fundingRate'],key=lambda x:int(x['calc_time'])):
            t=int(x['calc_time']);rounded=t//BAR*BAR
            if t-rounded>1000:raise ValueError('Unexpected funding settlement phase')
            rounding+=t!=rounded
            funding.append(Funding(rounded,float(x['last_funding_rate'])))
        if len({x.ts for x in funding})!=len(funding):raise ValueError('Duplicate funding')
        # BingX size/fee constraints retained as a counterfactual execution assumption.
        rules={'step':'.0001' if s=='BTC-USDT' else '.01','min_qty':.0001 if s=='BTC-USDT' else .01,
               'min_notional':2,'source':'BingX current snapshot applied to Binance research proxy'}
        data={'candles':[c.__dict__ for c in cs],'funding':[f.__dict__ for f in funding],'rules':rules}
        raw=json.dumps(data,sort_keys=True).encode();(out/f'{s}.json.gz').write_bytes(gzip.compress(raw,mtime=0))
        hashes[s]=hashlib.sha256(raw).hexdigest();summary[s]={'candles':len(cs),'funding':len(funding),'settlements_rounded_down_within_one_second':rounding}
    manifest={'source':'Binance USD-M PUBLIC ARCHIVE, NOT BingX execution data','sha256':hashes,
              'retrieved_at_ms':int(time.time()*1000),'metadata':{'start':cs[0].ts,'end':cs[-1].ts+BAR,
              'purpose':'training 2023 / validation 2024 / sealed final 2025','summary':summary,
              'funding_mark_proxy':'bar open; settlement timestamps rounded down by <=1 second',
              'execution_warning':'Cross-venue proxy; cannot establish BingX profitability','archives':sorted(sources,key=lambda x:x['url'])}}
    (out/'manifest.json').write_text(json.dumps(manifest,indent=2))
    print(json.dumps(summary),flush=True)

if __name__=='__main__':capture()
