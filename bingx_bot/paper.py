"""Live public quotes -> LOCAL simulated fills. Never exchange order endpoints."""
import json
import os
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from .config import BAR, SYMBOLS, Config
from .data import PublicClient
from .strategy import Strategy
from .store import Store

class PaperWorker:
    def __init__(self, store, client):
        self.store, self.client = store, client
        self.rules = None
        self.last_rules = 0
        self.strategies = {}
        self.signals = {}
        self.strategy_ts = None

    def cycle(self):
        now = self.client.now()
        close = now//BAR*BAR
        # All data for both symbols must be present before a state mutation.
        if not self.rules or now-self.last_rules > 24*3600_000:
            self.rules,_ = self.client.rules()
            self.last_rules = now
        if self.strategy_ts != close:
            for s in SYMBOLS:
                # On restart replay enough fixed warmup, then incrementally update.
                start = close-1000*BAR if self.strategy_ts is None else self.strategy_ts
                rows = self.client.candles(s,start,close)
                strategy = self.strategies.setdefault(s,Strategy(self.store.config))
                for c in rows:
                    self.signals[s] = strategy.update(c)
            self.strategy_ts = close
        quotes = {s:self.client.quote(s) for s in SYMBOLS}
        now = self.client.now()  # recheck after network latency
        for bid,ask,ts in quotes.values():
            if not -5000 <= now-ts <= 30_000:
                raise ValueError('Stale/future quote: no paper execution')
        broker = self.store.broker
        marks = {s:(q[0] if broker.positions.get(s) and broker.positions[s].side == 1 else q[1])
                 for s,q in quotes.items()}
        cursor = int(self.store.meta.get('funding_cursor',now))
        funding = {}
        funding_end = now
        for s in SYMBOLS:
            funding[s] = self.client.funding(s,cursor+1,now+1) if cursor < now else []
            if any(f.mark is None for f in funding[s]):
                raise ValueError('Missing actual settlement mark')
        now = self.client.now()
        for bid,ask,ts in quotes.values():
            if not -5000 <= now-ts <= 30_000:
                raise ValueError('Quote expired while fetching funding')
        # Every state mutation below will be committed atomically, or worker exits.
        broker.risk_check(now,marks)
        for s in SYMBOLS:
            for f in funding[s]:
                broker.fund(s,f.ts,f.rate,f.mark)
        self.store.meta['funding_cursor'] = min(now, max(cursor, funding_end))
        for s,p in list(broker.positions.items()):
            price = marks[s]
            reason = None
            if p.side*(price-p.stop) <= 0:
                reason = 'paper_stop_sampled'
            elif p.side*(price-p.target) >= 0:
                reason = 'paper_target_sampled'
            elif now-p.opened >= broker.config.max_bars*BAR:
                reason = 'paper_time_exit'
            if reason:
                broker.exit(s,price,now,reason)
        stopped = broker.risk_check(now,marks)
        kill = os.path.exists(str(self.store.path.parent/'STOP'))
        if stopped or kill:
            broker.flatten(marks,now,'manual_stop' if kill else 'risk_halt')
        # Never replay historical entries after restart/outage; only a fresh close (<60 sec).
        last_signal = int(self.store.meta.get('last_signal',0))
        if close > last_signal:
            if 0 <= now-close <= 60_000 and not stopped and not kill:
                for s in SYMBOLS:
                    bid,ask,_ = quotes[s]
                    sig = self.signals[s]
                    if sig.side and (ask-bid)/((ask+bid)/2) <= broker.config.max_spread:
                        broker.enter(s,sig.side,ask if sig.side == 1 else bid,
                                     sig.distance,now,self.rules[s],marks)
            else:
                broker.event(now,'signal_window_skipped',close=close,age_ms=now-close)
            self.store.meta['last_signal'] = close
        marks = {s:(quotes[s][0] if s in broker.positions and broker.positions[s].side == 1 else quotes[s][1]) for s in SYMBOLS}
        broker.risk_check(now,marks)
        broker.event(now,'heartbeat',equity=broker.last_equity,positions=len(broker.positions),
                     halted=broker.halted,daily_halted=broker.day_halted,manual_stop=kill)
        self.store.meta['heartbeat'] = now
        self.store.commit()
        return {'mode':'paper','ts':now,'equity':broker.last_equity,
                'positions':len(broker.positions),'halted':broker.halted,'manual_stop':kill}


def run(path, port=None, once=False):
    if os.environ.get('MODE','paper') != 'paper':
        raise ValueError('Only MODE=paper is supported')
    store = Store(path,Config())
    worker = PaperWorker(store,PublicClient())
    status = {'ok':False,'mode':'paper','last_success':0,'error':None}
    stop = threading.Event()
    signal.signal(signal.SIGTERM,lambda *_:stop.set())
    signal.signal(signal.SIGINT,lambda *_:stop.set())
    server = None
    if port:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                healthy = time.time()-status['last_success'] < 120 and status['ok']
                self.send_response(200 if healthy else 503)
                self.send_header('Content-Type','application/json')
                self.end_headers()
                # Minimal status, no account information or state mutation routes.
                self.wfile.write(json.dumps({'ok':healthy,'mode':'paper','error':status['error']}).encode())
            def log_message(self,*args):
                pass
        server = ThreadingHTTPServer(('0.0.0.0',port),Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
    failures = 0
    try:
        while not stop.is_set():
            try:
                result = worker.cycle()
                status.update(ok=True,last_success=time.time(),error=None)
                print(json.dumps(result),flush=True)
                failures = 0
            except Exception as exc:
                # Reload only durable state; no replay of uncommitted cash/funding.
                # Strategy may be partially updated: rebuild on retry.
                print(json.dumps({'mode':'paper','error':str(exc)}),flush=True)
                status.update(ok=False,error=type(exc).__name__)
                failures += 1
                if once or failures >= 5:
                    raise
                store.close()
                store = Store(path,Config())
                worker = PaperWorker(store,PublicClient())
            if once:
                break
            stop.wait(5 if failures == 0 else min(30,2**failures))
    finally:
        store.close()
        if server:
            server.shutdown()
