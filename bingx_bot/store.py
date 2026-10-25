"""Atomic state + append-only journal; one writer enforced by OS file lock."""
import csv
import fcntl
import json
import sqlite3
from pathlib import Path
from dataclasses import asdict
from .engine import Broker

class Store:
    def __init__(self, path, config):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.lock = open(str(path)+'.lock','a')
        try:
            fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise RuntimeError('Another paper worker owns this database')
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS journal (id INTEGER PRIMARY KEY, ts INTEGER, kind TEXT, payload TEXT)')
        self.config = config
        row = self.db.execute('SELECT payload FROM state WHERE id=1').fetchone()
        if row:
            state = json.loads(row[0])
            if state['config'] != asdict(config):
                self.close()
                raise ValueError('Config changed: use a separate paper database')
            self.broker = Broker.restore(state['broker'],config)
            self.meta = state['meta']
        else:
            self.broker = Broker(config)
            self.meta = {}

    def commit(self):
        payload = {'config':asdict(self.config),'broker':self.broker.snapshot(),'meta':self.meta}
        with self.db:
            for event in self.broker.events:
                self.db.execute('INSERT INTO journal(ts,kind,payload) VALUES (?,?,?)',
                                (event['ts'],event['kind'],json.dumps(event,allow_nan=False)))
            self.db.execute('INSERT OR REPLACE INTO state VALUES(1,?)',
                            (json.dumps(payload,allow_nan=False),))
        self.broker.events.clear()
        self.broker.trades.clear() # retained in journal; avoid unbounded worker RAM

    def export(self, path):
        trades = [json.loads(row[0]) for row in self.db.execute("SELECT payload FROM journal WHERE kind='exit' ORDER BY id")]
        with open(path,'w',newline='') as f:
            writer = csv.DictWriter(f,lineterminator="\n",fieldnames=list(trades[0]) if trades else ['ts','kind','net'])
            writer.writeheader()
            writer.writerows(trades)
        return len(trades)

    def close(self):
        if hasattr(self,'db'):
            self.db.close()
        if not self.lock.closed:
            fcntl.flock(self.lock,fcntl.LOCK_UN)
            self.lock.close()
