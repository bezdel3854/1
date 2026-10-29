"""Paper-only worker, health endpoint, durable source and execution journals."""
import argparse
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import threading
import time

from .finmonster_feed import Feed, FeedUnavailable, Observer, canonical
from .finmonster_paper import Market, Paper


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default=os.environ.get('DB_PATH', '/data/finmonster.sqlite'))
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    lock = open(args.db+'.lock', 'a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    observer = Observer(args.db)
    paper, feed, market = Paper(observer.db), Feed(os.environ.get('KOPYON_API_KEY'), observer.db), Market()
    status = {'mode': 'paper', 'healthy': False, 'real_orders_enabled': False,
              'source_key_configured': bool(feed.key)}
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
        print(canonical(status), flush=True)
        if args.once:
            return 0 if status['healthy'] else 1
        time.sleep(max(1, 60-(time.monotonic()-cycle)))


if __name__ == '__main__':
    raise SystemExit(main())
