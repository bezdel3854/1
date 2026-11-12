"""Independent 1000-USDT paper account; Hyperliquid public data only."""
import argparse
import fcntl
import os
from pathlib import Path
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from .finmonster_feed import Observer, canonical
from .finmonster_worker import record_source_health
from .hyperliquid_feed import FillObserver
from .source_copy_paper import SourceCopyPaper


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',default=os.environ.get('HL_DB_PATH','/data/hyperliquid-paper.sqlite'))
    parser.add_argument('--address',default=os.environ.get('HL_TRADER_ADDRESS'))
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--interval',type=int,default=15)
    args=parser.parse_args()
    if not args.address or args.interval < 15: parser.error('Public address and interval >=15 required')
    if os.environ.get('MODE','paper') != 'paper': parser.error('Only paper mode supported')
    if Path(args.db).name in ('okeyyt.sqlite','finmonster.sqlite'): parser.error('Independent database required')
    Path(args.db).parent.mkdir(parents=True,exist_ok=True)
    lock=open(args.db+'.lock','a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    observer=Observer(args.db,args.address.lower(),'Hyperliquid '+args.address.lower())
    source=FillObserver(observer.db,args.address)
    paper=SourceCopyPaper(observer.db,source.client,args.address)
    status={'mode':'paper','real_orders_enabled':False,'healthy':False,
            'trader_address':args.address.lower(),'source_name':'hyperliquid','execution_venue':'hyperliquid-paper'}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ('/health','/status','/ready'):self.send_error(404);return
            body=dict(status);body['healthy']=bool(body['healthy'] and time.time()-body.get('last_cycle',0)<180)
            self.send_response(200 if self.path!='/health' or body['healthy'] else 503)
            self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(canonical(body).encode())
        def log_message(self,*args):pass
    if not args.once:
        server=ThreadingHTTPServer(('0.0.0.0',int(os.environ.get('PORT','8080'))),Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
    failures=0
    while True:
        started=time.monotonic();ok=False
        try:
            status['source']=source.poll();ok=True;failures=0;status.pop('source_error',None)
        except Exception as exc:
            failures+=1;status['source_error']={'reason':type(exc).__name__}
        status['source_health']=record_source_health(observer.db,ok,status.get('source_error'))
        try:
            status.update(paper.step(allow_entries=ok),healthy=ok,last_cycle=time.time())
            if status['equity'] is None:
                status.update(healthy=False,market_error='Hyperliquid marks unavailable')
            else:
                status.pop('market_error',None)
        except Exception as exc:
            status.update(healthy=False,last_cycle=time.time(),market_error=f'{type(exc).__name__}: {exc}')
        print(canonical(status),flush=True)
        if args.once:return 0 if status['healthy'] else 1
        delay=min(300,args.interval*2**min(failures,5))
        time.sleep(max(1,delay-(time.monotonic()-started)))

if __name__=='__main__':raise SystemExit(main())
