"""Read-only prospective observations. No exchange credentials or order methods.

Run: python -m research.trader_review.finmonster_feed --db /data/finmonster.sqlite
Add --once for one check. This does NOT simulate fills or establish profitability.
"""
import argparse
import hashlib
import json
import math
import os
import sqlite3
import time
import urllib.parse
import urllib.request

TRADER = '1584417045916868615_1627463895052578906'
BASE = 'https://api.kopyon.com/v5/bingx-copy-trading/'
ENDPOINTS = frozenset(('traderProfile', 'openPositions', 'orderHistory'))


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


class Feed:
    def __init__(self, key=None):
        self.key = key

    def get(self, endpoint, **params):
        if endpoint not in ENDPOINTS:
            raise ValueError('Read-only endpoint is not allowlisted')
        params['traderId'] = TRADER
        headers = {'User-Agent': 'FinMonsterResearchObserver/1.0'}
        if self.key:
            headers['X-API-KEY'] = self.key
        request = urllib.request.Request(BASE + endpoint + '?' + urllib.parse.urlencode(params),
                                         headers=headers, method='GET')
        with urllib.request.urlopen(request, timeout=25) as response:
            payload = json.load(response)
        if not isinstance(payload, dict) or not isinstance(payload.get('data'), dict):
            raise ValueError('Invalid feed envelope')
        if payload.get('error') or payload.get('success') is False:
            raise ValueError('Feed reported an error')
        return payload['data']


class Observer:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS observations(
          order_id TEXT NOT NULL, digest TEXT NOT NULL, source_ms INTEGER NOT NULL,
          first_seen_ms INTEGER NOT NULL, category TEXT NOT NULL, payload TEXT NOT NULL,
          PRIMARY KEY(order_id,digest));
        CREATE TABLE IF NOT EXISTS polls(
          observed_ms INTEGER PRIMARY KEY, duration_ms INTEGER NOT NULL, payload TEXT NOT NULL);
        ''')

    def poll(self, feed, now=None):
        started = time.time_ns() // 1_000_000
        profile = feed.get('traderProfile')
        if profile.get('traderId') != TRADER or profile.get('name') != 'FinMonster':
            raise ValueError('Trader identity mismatch')
        positions = feed.get('openPositions')
        if positions.get('hidden') is not False or not isinstance(positions.get('positions'), list):
            raise ValueError('Positions are hidden or invalid; no successful poll recorded')
        for position in positions['positions']:
            if not all(k in position for k in ('positionId', 'symbol', 'side', 'size')):
                raise ValueError('Incomplete position')
        baseline = self.db.execute("SELECT value FROM meta WHERE key='baseline_ms'").fetchone()
        anchor = self.db.execute("SELECT value FROM meta WHERE key='anchor'").fetchone()
        orders, cursors, cursor, reached = [], set(), None, False
        for _ in range(20):
            page = feed.get('orderHistory', rows=50, **({'cursor': cursor} if cursor else {}))
            rows = page.get('orders')
            if not isinstance(rows, list):
                raise ValueError('Invalid order page')
            orders.extend(rows)
            reached = bool(anchor and any(str(o.get('orderId')) == anchor[0] for o in rows))
            cursor = page.get('nextCursor')
            if not baseline or reached or not cursor:
                break
            if cursor in cursors:
                raise ValueError('Pagination cursor loop')
            cursors.add(cursor)
        if baseline and anchor and not reached:
            raise ValueError('History gap: previous anchor absent; refusing to advance')
        if not orders:
            raise ValueError('Empty history: cannot establish or advance baseline')
        # First-seen time is after receipt, never the request start time.
        now = time.time_ns() // 1_000_000 if now is None else now
        prepared = []
        for order in orders:
            if not all(k in order for k in ('orderId', 'positionId', 'symbol', 'side',
                                             'action', 'status', 'updatedAt', 'quantity', 'price')):
                raise ValueError('Incomplete order')
            stamp = order['updatedAt']
            if not isinstance(stamp, int) or isinstance(stamp, bool) or stamp <= 0 or stamp > now + 60_000:
                raise ValueError('Invalid or future source timestamp')
            for field in ('quantity', 'price'):
                value = order[field]
                if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
                    raise ValueError('Invalid order amount')
            if order['side'] not in ('BUY', 'SELL') or order['action'] not in ('OPEN', 'CLOSE', 'BOTH'):
                raise ValueError('Unknown order direction/action')
            # All existing history is a baseline, never a new entry at an old price.
            category = 'baseline' if not baseline or stamp <= int(baseline[0]) else 'observed'
            body = canonical(order)
            prepared.append((str(order['orderId']), hashlib.sha256(body.encode()).hexdigest(),
                             stamp, now, category, body))
        before = self.db.total_changes
        with self.db:
            self.db.executemany('INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?,?)', prepared)
            added = self.db.total_changes - before
            self.db.execute("INSERT OR IGNORE INTO meta VALUES('baseline_ms',?)", (str(now),))
            newest = max(orders, key=lambda o: (o['updatedAt'], str(o['orderId'])))
            self.db.execute("INSERT OR REPLACE INTO meta VALUES('anchor',?)", (str(newest['orderId']),))
            self.db.execute('INSERT INTO polls VALUES(?,?,?)',
                            (now, time.time_ns() // 1_000_000 - started,
                             canonical({'profile': profile, 'positions': positions, 'orders': orders})))
        return {'observed_at_ms': now, 'new_order_versions': added,
                'positions': len(positions['positions']), 'latest_source_ms': newest['updatedAt'],
                'latest_event_age_ms': now - newest['updatedAt'],
                'mode': 'observation_only', 'paper_trading_started': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--interval', type=int, default=60)
    args = parser.parse_args()
    if args.interval < 30:
        parser.error('Minimum poll interval is 30 seconds')
    observer, feed = Observer(args.db), Feed(os.environ.get('KOPYON_API_KEY'))
    while True:
        try:
            result = observer.poll(feed)
        except Exception as exc:
            # No credentials/URLs/response bodies in error logs.
            print(canonical({'mode': 'observation_only', 'healthy': False,
                             'error_type': type(exc).__name__}), flush=True)
            if args.once:
                raise SystemExit(1)
        else:
            print(canonical(result), flush=True)
        if args.once:
            return
        time.sleep(args.interval)


if __name__ == '__main__':
    main()
