"""Deterministic container lifecycle test; no external calls or orders.
Runs the actual CLI worker, HTTP health, SIGTERM and durable account restart.
"""
import json
import os
import signal
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from bingx_bot.config import BAR, SYMBOLS
from bingx_bot.data import Candle, Rules, PublicAPIUnavailable
from bingx_bot.paper import run
from bingx_bot.store import Store
from bingx_bot.approaches import runtime_config

PORT=18080
BASE=(1_780_012_800_000//3_600_000)*3_600_000


def health():
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{PORT}/health',timeout=3) as r:
            return r.status,json.load(r)
    except urllib.error.HTTPError as e:
        return e.code,json.load(e)

class FakePublicClient:
    def __init__(self,terminate=False):
        self.clock=BASE+4096*BAR+10_000
        self.calls=0
        self.terminate=terminate
        self.initial_status=None
        self.ready_status=None

    def now(self):
        self.calls+=1
        if self.calls==1:
            self.initial_status=health()[0]
            assert self.initial_status==503,'health must reject incomplete initialization'
        if self.calls==4:
            self.ready_status=health()[0]
            assert self.ready_status==200,'health must accept a completed durable cycle'
            self.clock+=5000
            if self.terminate:os.kill(os.getpid(),signal.SIGTERM)
        return self.clock

    def rules(self):
        return {s:Rules('.0001',.0001,2) for s in SYMBOLS},[]

    def candles(self,s,start,end):
        return [Candle(ts,100,100.1,99.9,100,1_000_000) for ts in range(start,end,BAR)]

    def quote(self,s):return 100,100.01,self.clock
    def funding(self,*args):return []


def main():
    os.environ['MODE']='paper'
    with tempfile.TemporaryDirectory() as d:
        path=Path(d)/'candidate.sqlite'
        first=FakePublicClient(terminate=True)
        with patch('bingx_bot.paper.PublicClient',return_value=first):
            run(path,port=PORT,strategy_name='candidate_120h')
        assert first.ready_status==200
        cfg=runtime_config('candidate_120h')
        store=Store(path,cfg)
        assert store.broker.cash==1000
        assert not store.broker.positions
        assert store.meta['strategy_name']=='candidate_120h'
        assert store.meta['observe_only'] is True
        count=store.db.execute("SELECT count(*) FROM journal WHERE kind='heartbeat'").fetchone()[0]
        assert count==2,count
        store.close()
        # Reuse exact port and database: confirms socket/file lock shutdown and restart.
        second=FakePublicClient()
        with patch('bingx_bot.paper.PublicClient',return_value=second):
            run(path,port=PORT,once=True,strategy_name='candidate_120h')
        store=Store(path,cfg)
        assert store.db.execute("SELECT count(*) FROM journal WHERE kind='heartbeat'").fetchone()[0]==3
        assert store.broker.last_equity==1000
        store.close()
        # Sustained gateway outage must stay alive, return 503, preserve feature
        # warmup, then return 200 only after a committed fresh cycle.
        class OutageClient(FakePublicClient):
            def __init__(self):
                super().__init__();self.remaining=7;self.reads=0
            def now(self):return self.clock
            def candles(self,*args):
                self.reads+=1;return super().candles(*args)
            def quote(self,s):
                if self.remaining:
                    self.remaining-=1
                    raise PublicAPIUnavailable('book','simulated 100410',60)
                return super().quote(s)
        class RecoveryStop:
            stopped=False
            waits=0
            def set(self):self.stopped=True
            def is_set(self):return self.stopped
            def wait(self,seconds):
                if seconds==5:
                    assert health()[0]==200
                    self.set()
                else:
                    self.waits+=1
                    assert seconds>=60 and health()[0]==503
        outage=OutageClient();control=RecoveryStop()
        with patch('bingx_bot.paper.PublicClient',return_value=outage) as constructor:
            run(path,port=PORT,strategy_name='candidate_120h',stop_event=control)
        assert control.waits==7 and constructor.call_count==1 and outage.reads==2
        store=Store(path,cfg)
        assert store.broker.last_equity==1000 and not store.broker.positions
        assert store.db.execute("SELECT count(*) FROM journal WHERE kind='api_unavailable'").fetchone()[0]==7
        assert store.db.execute("SELECT count(*) FROM journal WHERE kind='api_recovered'").fetchone()[0]==1
        store.close()
        print(json.dumps({'container_lifecycle':'PASS','startup_health':503,'ready_health':200,
                          'sigterm':'graceful','restart':'same port and durable account',
                          'seven_outages':'503 then recovery200, one client, one warmup',
                          'mode':'observation only','external_requests':0}))

if __name__=='__main__':main()
