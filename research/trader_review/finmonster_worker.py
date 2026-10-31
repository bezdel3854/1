"""Paper-only worker, health endpoint, durable source and execution journals."""
import argparse
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import threading
import time

from .finmonster_feed import Feed, FeedUnavailable, Observer, TRADER, canonical
import json
from .finmonster_paper import Market, Paper


def retire_previous(path, new_db, market):
    """Close the previous virtual account and carry over request accounting."""
    previous_lock = open(path+'.lock', 'a')
    fcntl.flock(previous_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        old = Observer(path)
        try:
            paper = Paper(old.db)
            if not (paper.state() or {}).get('retired'):
                paper.step(market, allow_entries=False, retire=True)
            month = time.strftime('%Y-%m', time.gmtime())
            usage = []
            for db in (old.db, new_db):
                row = db.execute("SELECT value FROM meta WHERE key='feed_request_usage'").fetchone()
                saved = json.loads(row[0]) if row else {}
                if saved.get('month') == month: usage.append(saved.get('requests', 0))
            with new_db:
                new_db.execute("INSERT OR REPLACE INTO meta VALUES('feed_request_usage',?)",
                               (canonical({'month': month, 'requests': max(usage, default=0)}),))
            state = paper.state()
            return {'retired': True, 'cash': state['broker']['cash'], 'positions': 0}
        finally:
            old.db.close()
    finally:
        previous_lock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default=os.environ.get('DB_PATH', '/data/finmonster.sqlite'))
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    lock = open(args.db+'.lock', 'a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    trader_id = os.environ.get('TRADER_ID', TRADER)
    trader_name = os.environ.get('TRADER_NAME', 'FinMonster')
    observer = Observer(args.db, trader_id, trader_name)
    market = Market()
    previous_path = os.environ.get('PREVIOUS_DB_PATH')
    previous_result = None
    if previous_path:
        if Path(previous_path).resolve() == Path(args.db).resolve() or not Path(previous_path).is_file():
            raise ValueError('Previous account must exist and differ from the new account')
        previous_result = retire_previous(previous_path, observer.db, market)
    paper = Paper(observer.db)
    feed = Feed(os.environ.get('KOPYON_API_KEY'), observer.db, trader_id, trader_name)
    status = {'mode': 'paper', 'healthy': False, 'real_orders_enabled': False,
              'source_key_configured': bool(feed.key),
              'trader_id': trader_id, 'trader_name': trader_name,
              'previous_account': previous_result}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ('/ready', '/health', '/status'):
                self.send_error(404); return
            body = dict(status)
            body['healthy'] = bool(body.get('healthy') and time.time()-body.get('last_cycle', 0) < 180)
            # Deployment readiness means process/database initialized. Runtime
            # source health stays explicit and independent at /health.
            self.send_response(200 if self.path in ('/ready', '/status') or body['healthy'] else 503)
            self.send_header('Content-Type', 'application/json'); self.end_headers()
            self.wfile.write(canonical(body).encode())
        def log_message(self, *args): pass
    if not args.once:
        server = ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('PORT', 8080))), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
    while True:
        cycle = time.monotonic()
        source_ok = False
        try:
            observed = observer.poll(feed)
            source_ok = True
            observed.pop('paper_trading_started', None)
            status['source'] = observed
        except Exception as exc:
            status['source_error'] = exc.diagnostic() if isinstance(exc, FeedUnavailable) else {
                'reason': type(exc).__name__}
        try:
            result = paper.step(market, allow_entries=source_ok)
            status.update(result, healthy=source_ok, last_cycle=time.time())
            if source_ok: status.pop('source_error', None)
            status.pop('market_error', None)
        except Exception as exc:
            status.update(healthy=False, last_cycle=time.time(), market_error=type(exc).__name__)
        if 'source' in status:
            status['source']['latest_event_age_ms'] = int(time.time()*1000)-status['source']['latest_source_ms']
            status['source']['snapshot_age_ms'] = int(time.time()*1000)-status['source']['observed_at_ms']
            status['source']['positions_age_ms'] = int(time.time()*1000)-status['source']['positions_received_at_ms']
        status['source_requests_this_month'] = feed.usage.get('requests', 0)
        status['source_monthly_request_budget'] = 9000
        print(canonical(status), flush=True)
        if args.once:
            return 0 if status['healthy'] else 1
        time.sleep(max(1, 60-(time.monotonic()-cycle)))


if __name__ == '__main__':
    raise SystemExit(main())
