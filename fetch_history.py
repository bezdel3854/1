"""Reproducible public-data capture; no keys. End exclusive UTC."""
from datetime import datetime, timezone
from bingx_bot.data import PublicClient, save_dataset
from bingx_bot.config import SYMBOLS

if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--start',default='2025-10-25')
    ap.add_argument('--end',default='2026-10-25')
    ap.add_argument('--out',default='data/history')
    a=ap.parse_args()
    stamp=lambda d:int(datetime.fromisoformat(d).replace(tzinfo=timezone.utc).timestamp()*1000)
    start,end=stamp(a.start),stamp(a.end)
    client=PublicClient()
    if start>=end or end>client.now()//900_000*900_000:
        raise ValueError('Invalid range / unfinished candles')
    rules,raw=client.rules()
    candles,funding={},{}
    for symbol in SYMBOLS:
        print('Fetching',symbol,flush=True)
        candles[symbol]=client.candles(symbol,start,end)
        print('Candles',len(candles[symbol]),flush=True)
        funding[symbol]=client.funding(symbol,start,end)
        print('Funding',len(funding[symbol]),flush=True)
    save_dataset(a.out,candles,funding,rules,{'start':start,'end':end,'contracts_snapshot':raw})
    print('Saved',a.out,flush=True)
