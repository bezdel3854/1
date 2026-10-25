import argparse
import json
import os
from .config import Config
from .report import research
from .paper import run
from .store import Store


def main():
    ap=argparse.ArgumentParser(description='BingX research/paper ONLY. No real orders.')
    sub=ap.add_subparsers(dest='command',required=True)
    b=sub.add_parser('backtest');b.add_argument('--data',default='data/history');b.add_argument('--out',default='reports/latest')
    p=sub.add_parser('paper');p.add_argument('--db',default=os.environ.get('DB_PATH','runtime/paper.sqlite'));p.add_argument('--once',action='store_true')
    e=sub.add_parser('export');e.add_argument('--db',default=os.environ.get('DB_PATH','runtime/paper.sqlite'));e.add_argument('--out',default='trades.csv')
    a=ap.parse_args()
    if a.command=='backtest':
        result=research(a.data,a.out)
        print(json.dumps(result['runs'],indent=2,allow_nan=False))
    elif a.command=='paper':
        run(a.db,int(os.environ.get('PORT','8080')),a.once)
    else:
        store=Store(a.db,Config())
        try: print(json.dumps({'exported':store.export(a.out),'path':a.out}))
        finally:store.close()

if __name__=='__main__':
    main()
