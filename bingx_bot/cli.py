import argparse
import json
import os
from .config import Config
from .report import research
from .paper import run
from .store import Store, existing_config
from .approaches import NAMES
from .experiments import run_experiments


def main():
    ap=argparse.ArgumentParser(description='BingX research/paper ONLY. No real orders.')
    sub=ap.add_subparsers(dest='command',required=True)
    b=sub.add_parser('backtest');b.add_argument('--data',default='data/history');b.add_argument('--out',default='reports/latest')
    p=sub.add_parser('paper');p.add_argument('--db',default=os.environ.get('DB_PATH','runtime/candidate-120h.sqlite'));p.add_argument('--once',action='store_true')
    p.add_argument('--strategy',choices=NAMES,default=os.environ.get('STRATEGY','candidate_120h'))
    p.add_argument('--enable-experimental-entries',action='store_true',help='Allow virtual entries for an unproven experimental approach')
    r=sub.add_parser('research');r.add_argument('--data',default='data/history');r.add_argument('--out',default='reports/approaches')
    e=sub.add_parser('export');e.add_argument('--db',default=os.environ.get('DB_PATH','runtime/candidate-120h.sqlite'));e.add_argument('--out',default='trades.csv')
    a=ap.parse_args()
    if a.command=='backtest':
        result=research(a.data,a.out)
        print(json.dumps(result['runs'],indent=2,allow_nan=False))
    elif a.command=='paper':
        run(a.db,int(os.environ.get('PORT','8080')),a.once,a.strategy,a.enable_experimental_entries)
    elif a.command=='research':
        run_experiments(a.data,a.out)
    else:
        store=Store(a.db,existing_config(a.db))
        try: print(json.dumps({'exported':store.export(a.out),'path':a.out}))
        finally:store.close()

if __name__=='__main__':
    main()
