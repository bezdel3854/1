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
from copy import deepcopy
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from email.utils import parsedate_to_datetime

TRADER = '1584417045916868615_1627463895052578906'
BASE = 'https://api.kopyon.com/v5/bingx-copy-trading/'
ENDPOINTS = frozenset(('traderProfile', 'openPositions', 'orderHistory'))
CACHE_TTL_MS = {'traderProfile': 6*3600_000, 'openPositions': 5*60_000}
MONTHLY_REQUEST_BUDGET = 9000  # conservative ceiling within the documented free 10k quota


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


class FeedUnavailable(RuntimeError):
    def __init__(self, endpoint, http_status=None, retry_at_ms=0, reason='unavailable'):
        self.endpoint, self.http_status = endpoint, http_status
        self.retry_at_ms, self.reason = retry_at_ms, reason
        super().__init__(f'{endpoint}: {reason}')

    def diagnostic(self):
        return {'endpoint': self.endpoint, 'http_status': self.http_status,
                'retry_at_ms': self.retry_at_ms, 'reason': self.reason}


def retry_delay(value, now_seconds):
    try:
        seconds = float(value)
        if math.isfinite(seconds) and seconds >= 0:
            return max(1, seconds)
    except (TypeError, ValueError):
        pass
    try:
        return max(1, parsedate_to_datetime(value).timestamp()-now_seconds)
    except (TypeError, ValueError, OverflowError):
        return 300


class Feed:
    def __init__(self, key=None, db=None):
        self.key = key
        self.db = db
        self.credential_id = hashlib.sha256((key or 'anonymous').encode()).hexdigest()
        self.cooldown = None
        self.failures = 0
        self.cache = {}
        self.received_ms = {}
        self.usage = {}
        if db is not None:
            row = db.execute("SELECT value FROM meta WHERE key='feed_cooldown'").fetchone()
            if row:
                saved = json.loads(row[0])
                if saved['credential_id'] == self.credential_id:
                    self.cooldown = saved
            for endpoint in CACHE_TTL_MS:
                row = db.execute('SELECT value FROM meta WHERE key=?', ('feed_cache_'+endpoint,)).fetchone()
                if row:
                    saved = json.loads(row[0])
                    if saved['credential_id'] == self.credential_id:
                        self.cache[endpoint] = saved
            row = db.execute("SELECT value FROM meta WHERE key='feed_request_usage'").fetchone()
            if row:
                self.usage = json.loads(row[0])

    def reserve_request(self, endpoint):
        current = datetime.fromtimestamp(time.time(), timezone.utc)
        month = current.strftime('%Y-%m')
        if self.usage.get('month') != month:
            self.usage = {'month': month, 'requests': 0}
        if self.usage['requests'] >= MONTHLY_REQUEST_BUDGET:
            year, next_month = current.year + (current.month == 12), current.month % 12 + 1
            until = int(datetime(year, next_month, 1, tzinfo=timezone.utc).timestamp()*1000)
            raise FeedUnavailable(endpoint, None, until, 'local_monthly_request_budget')
        # Count attempts before sending; failures may also consume provider quota.
        self.usage['requests'] += 1
        if self.db is not None:
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO meta VALUES('feed_request_usage',?)",
                                (canonical(self.usage),))

    def unavailable(self, endpoint, status, seconds, reason):
        until = int(time.time()*1000 + seconds*1000)
        self.cooldown = dict(credential_id=self.credential_id, endpoint=endpoint,
                             http_status=status, retry_at_ms=until, reason=reason)
        if self.db is not None:
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO meta VALUES('feed_cooldown',?)",
                                (canonical(self.cooldown),))
        return FeedUnavailable(endpoint, status, until, reason)

    def get(self, endpoint, **params):
        if endpoint not in ENDPOINTS:
            raise ValueError('Read-only endpoint is not allowlisted')
        if self.cooldown and time.time()*1000 < self.cooldown['retry_at_ms']:
            saved = self.cooldown
            raise FeedUnavailable(saved['endpoint'], saved['http_status'],
                                  saved['retry_at_ms'], saved['reason'])
        cached = self.cache.get(endpoint)
        if not params and cached and 0 <= time.time()*1000-cached['received_ms'] < CACHE_TTL_MS[endpoint]:
            self.received_ms[endpoint] = cached['received_ms']
            return deepcopy(cached['data'])
        cacheable_request = not params and endpoint in CACHE_TTL_MS
        params['traderId'] = TRADER
        headers = {'User-Agent': 'FinMonsterResearchObserver/1.0'}
        if self.key:
            headers['X-API-KEY'] = self.key
        request = urllib.request.Request(BASE + endpoint + '?' + urllib.parse.urlencode(params),
                                         headers=headers, method='GET')
        self.reserve_request(endpoint)
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                payload = json.load(response)
        except HTTPError as exc:
            self.failures += 1
            delay = retry_delay(exc.headers.get('Retry-After'), time.time())
            reason = 'rate_limited' if exc.code == 429 else 'http_error'
            # Preserve only an allowlisted machine code, never arbitrary bodies or credentials.
            try:
                body = json.loads(exc.read(4096))
                code = body.get('error', {}).get('code')
                if code in ('RATE_LIMITED', 'INVALID_API_KEY', 'MISSING_API_KEY', 'QUOTA_EXCEEDED'):
                    reason = code.lower()
            except (TypeError, ValueError, AttributeError):
                pass
            if exc.code in (401, 402, 403):
                delay = max(delay, 3600)
            raise self.unavailable(endpoint, exc.code, delay, reason) from exc
        except (URLError, TimeoutError, ConnectionError) as exc:
            self.failures += 1
            delay = min(300, 60 * 2**min(self.failures-1, 3))
            raise self.unavailable(endpoint, None, delay, 'network_error') from exc
        if not isinstance(payload, dict) or not isinstance(payload.get('data'), dict):
            raise ValueError('Invalid feed envelope')
        if payload.get('error') or payload.get('success') is False:
            raise ValueError('Feed reported an error')
        self.failures = 0
        stamp = int(time.time()*1000)
        self.received_ms[endpoint] = stamp
        data = payload['data']
        cache_valid = (endpoint == 'traderProfile' and data.get('traderId') == TRADER
                       and data.get('name') == 'FinMonster') or (
            endpoint == 'openPositions' and data.get('hidden') is False
            and isinstance(data.get('positions'), list)
            and all(isinstance(p, dict) and all(k in p for k in ('positionId','symbol','side','size'))
                    for p in data['positions']))
        if cacheable_request and cache_valid:
            self.cache[endpoint] = {'credential_id': self.credential_id, 'received_ms': stamp,
                                    'data': deepcopy(data)}
            if self.db is not None:
                with self.db:
                    self.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',
                                    ('feed_cache_'+endpoint, canonical(self.cache[endpoint])))
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
                'positions_received_at_ms': getattr(feed, 'received_ms', {}).get('openPositions', now),
                'profile_received_at_ms': getattr(feed, 'received_ms', {}).get('traderProfile', now),
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
