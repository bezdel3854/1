"""Live public quotes -> LOCAL simulated fills. Never exchange order endpoints."""
import json
import os
import signal
import threading
import time
import random
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from .config import BAR, SYMBOLS, Config
from .data import PublicClient, PublicAPIUnavailable
from .strategy import Strategy
from .store import Store
from .engine import apply_trail
from .approaches import factory, runtime_config

class PaperWorker:
    def __init__(self, store, client, strategy_name='baseline', observe_only=None):
        self.store, self.client = store, client
        self.strategy_name = strategy_name
        factory(strategy_name)  # validate even before first network request
        saved = store.meta.get('strategy_name','baseline' if store.meta else None)
        if saved is not None and saved != strategy_name:
            raise ValueError('Strategy changed: use a separate paper database')
        self.observe_only = strategy_name != 'baseline' if observe_only is None else observe_only
        store.meta['strategy_name'] = strategy_name
        store.meta['observe_only'] = self.observe_only
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
            # A failure on ETH must not leave BTC advanced and replay its bars
            # on retry. Publish feature state only after BOTH reads succeed.
            strategies = deepcopy(self.strategies)
            signals = dict(self.signals)
            for s in SYMBOLS:
                # On restart replay enough fixed warmup, then incrementally update.
                warmup = 4096 if self.strategy_name == 'candidate_120h' else 1000
                start = close-warmup*BAR if self.strategy_ts is None else self.strategy_ts
                rows = self.client.candles(s,start,close)
                strategy = strategies.get(s)
                if strategy is None:
                    strategy = Strategy(self.store.config) if self.strategy_name == 'baseline' else factory(self.strategy_name)(self.store.config)
                    strategies[s] = strategy
                for c in rows:
                    signals[s] = strategy.update(c)
            self.strategies, self.signals = strategies, signals
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
        last_signal = int(self.store.meta.get('last_signal',0))
        fresh_signal = close > last_signal and 0 <= now-close <= 60_000
        for s,p in list(broker.positions.items()):
            if fresh_signal:
                apply_trail(broker,s,self.signals[s])
            price = marks[s]
            reason = None
            if p.side*(price-p.stop) <= 0:
                reason = 'paper_stop_sampled'
            elif p.side*(price-p.target) >= 0:
                reason = 'paper_target_sampled'
            elif now-p.opened >= broker.config.max_bars*BAR:
                reason = 'paper_time_exit'
            sig = self.signals[s]
            if reason is None and fresh_signal and ((p.side == 1 and sig.close_long) or (p.side == -1 and sig.close_short)):
                reason = 'paper_strategy_exit'
            if reason:
                broker.exit(s,price,now,reason)
        stopped = broker.risk_check(now,marks)
        kill = os.path.exists(str(self.store.path.parent/'STOP'))
        if stopped or kill:
            broker.flatten(marks,now,'manual_stop' if kill else 'risk_halt')
        # Never replay historical entries after restart/outage; only a fresh close (<60 sec).
        if close > last_signal:
            for s in SYMBOLS:
                broker.event(now,'decision',symbol=s,strategy=self.strategy_name,side=self.signals[s].side,observe_only=self.observe_only,close=close)
            if fresh_signal and not stopped and not kill and not self.observe_only:
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
        return {'mode':'paper','strategy':self.strategy_name,'observe_only':self.observe_only,'ts':now,'equity':broker.last_equity,
                'positions':len(broker.positions),'halted':broker.halted,'manual_stop':kill}


def run(path, port=None, once=False, strategy_name="baseline", enable_experimental_entries=False, *, stop_event=None):
    if os.environ.get('MODE','paper') != 'paper':
        raise ValueError('Only MODE=paper is supported')
    config = runtime_config(strategy_name)
    store = Store(path,config)
    observe = strategy_name != "baseline" and not enable_experimental_entries
    worker = PaperWorker(store,PublicClient(),strategy_name,observe)
    status = {'ok':False,'mode':'paper','last_success':0,'error':None}
    stop = stop_event if stop_event is not None else threading.Event()
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
    transient_failures = 0
    try:
        while not stop.is_set():
            try:
                result = worker.cycle()
                status.update(ok=True,last_success=time.time(),error=None)
                if transient_failures:
                    store.broker.event(result['ts'],'api_recovered',failures=transient_failures)
                    store.commit()
                    print(json.dumps({'mode':'paper','event':'api_recovered','failures':transient_failures}),flush=True)
                print(json.dumps(result),flush=True)
                failures = 0
                transient_failures = 0
            except PublicAPIUnavailable as exc:
                # All public calls precede account mutations. Keep the client
                # limiter and completed feature state, not a new 8192-bar warmup.
                status.update(ok=False,error=type(exc).__name__)
                transient_failures += 1
                delay = max(exc.retry_after,min(300,60*2**min(transient_failures-1,3)+random.uniform(0,5)))
                now = int(time.time()*1000)
                store.broker.event(now,'api_unavailable',endpoint=exc.endpoint,
                                   retry_seconds=delay,failures=transient_failures)
                store.commit()
                print(json.dumps({'mode':'paper','event':'api_unavailable','endpoint':exc.endpoint,
                                  'retry_seconds':delay,'failures':transient_failures}),flush=True)
                if once:raise
                # Interruptible by SIGTERM; no stale-price fills during cooldown.
                stop.wait(delay)
                continue
            except Exception as exc:
                # Reload only durable state; no replay of uncommitted cash/funding.
                # Strategy may be partially updated: rebuild on retry.
                print(json.dumps({'mode':'paper','error':str(exc)}),flush=True)
                status.update(ok=False,error=type(exc).__name__)
                failures += 1
                if once or failures >= 5:
                    raise
                store.close()
                store = Store(path,config)
                worker = PaperWorker(store,PublicClient(),strategy_name,observe)
            if once:
                break
            stop.wait(5 if failures == 0 else min(30,2**failures))
    finally:
        store.close()
        if server:
            server.shutdown()
            server.server_close()
