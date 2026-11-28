"""Persist authenticated parent/child order links; never place trading orders."""
import gzip
import io
import json
import logging
import re
import sqlite3
import threading
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .bingx_execution import BingXAPIError, ReconciliationRequired


def order_id(value):
    if type(value) not in (str, int) or not re.fullmatch(r'[1-9][0-9]*', str(value)):
        raise ValueError('Invalid exchange order identity')
    return str(value)


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS copy_exchange_links '
               '(order_id TEXT PRIMARY KEY,parent_id TEXT NOT NULL,symbol TEXT NOT NULL, '
               'payload TEXT NOT NULL,seen_ms INTEGER NOT NULL)')


def record_event(path, message):
    if message.get('e') not in ('TRADE_UPDATE', 'ORDER_TRADE_UPDATE'):
        return
    order = message['o']
    oid = order_id(order['i'])
    with sqlite3.connect(path, timeout=5) as db:
        initialize(db)
        prior = db.execute('SELECT parent_id,symbol FROM copy_exchange_links WHERE order_id=?', (oid,)).fetchone()
        link = order.get('li')
        if link in (None, '', 0, '0') and not prior:
            return  # An absent parent is valid, but cannot prove child ownership.
        parent = order_id(link) if link not in (None, '', 0, '0') else prior[0]
        symbol = order['s']
        if prior and prior != (parent, symbol):
            raise ValueError('Conflicting exchange parent identity')
        if not re.fullmatch(r'[A-Z0-9]+-USDT', symbol):
            return  # Other quote currencies are outside this copy worker.
        db.execute('INSERT OR REPLACE INTO copy_exchange_links VALUES (?,?,?,?,?)',
                   (oid, parent, symbol, json.dumps(message, sort_keys=True), time.time_ns() // 1_000_000))


def decode_message(raw):
    if isinstance(raw, bytes):
        if raw.startswith(b'\x1f\x8b'):
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
                raw = stream.read(1_048_577)
        if len(raw) > 1_048_576:
            raise ValueError('Oversized exchange event')
        raw = raw.decode('utf-8')
    if raw == 'Ping':
        return 'Ping'
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError('Invalid exchange event')
    return result


class AccountOrderStream:
    def __init__(self, execution, path):
        if execution.environment != 'live':
            raise ValueError('Live account stream cannot use a demo account')
        self.execution, self.path = execution, path
        self.connected = False
        self.last_seen = 0.0
        self.error_type = None
        self.last_event_type = None
        self.lock = threading.Lock()
        with sqlite3.connect(path) as db:
            initialize(db)

    def health(self):
        with self.lock:
            age = time.monotonic() - self.last_seen
            return dict(ready=self.connected and age < 45,
                        last_message_age_ms=int(age * 1000) if self.last_seen else None,
                        error_type=self.error_type, last_event_type=self.last_event_type)

    def require_ready(self):
        if not self.health()['ready']:
            raise ReconciliationRequired('Account event stream unavailable; retain entry until fresh or expired')

    def _auth(self, method, key=None):
        # Listen-key management is account-data subscription only, not trading.
        url = self.execution.api_origin + '/openApi/user/auth/userDataStream'
        if key is not None:
            url += '?' + urlencode({'listenKey': key})
        request = Request(url, method=method, headers={
            'X-BX-APIKEY': self.execution.api_key, 'X-SOURCE-KEY': 'BX-AI-SKILL'})
        with urlopen(request, timeout=10) as response:
            payload = json.load(response)
        if not isinstance(payload, dict):
            raise RuntimeError('Account stream authentication failed')
        if type(payload.get('code', 0)) is not int:
            raise ValueError('Malformed account stream authentication response')
        if payload.get('code', 0) != 0:
            raise BingXAPIError(payload['code'], 'Account stream authentication failed')
        if method == 'POST':
            key = payload.get('listenKey') or (payload.get('data') or {}).get('listenKey')
            if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,256}', key):
                raise ValueError('Invalid account stream key')
        return key

    def session(self):
        from websockets.sync.client import connect
        with self.lock:
            self.connected, self.last_event_type = False, None
        self.execution.verify_account_identity()
        key = self._auth('POST')
        logger = logging.getLogger('copy.account_stream.private')
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
        with connect('wss://open-api-swap.bingx.com/swap-market?' + urlencode({'listenKey': key}),
                     proxy=None, compression=None, open_timeout=10, close_timeout=5,
                     ping_interval=20, ping_timeout=10, max_size=1_048_576, max_queue=64,
                     logger=logger) as ws:
            # Wait for the first server message before certifying subscription.
            connected_at = renewed = time.monotonic()
            while True:
                if time.monotonic() - renewed > 1800:
                    self._auth('PUT', key)
                    renewed = time.monotonic()
                try:
                    message = decode_message(ws.recv(timeout=5))
                except TimeoutError:
                    if time.monotonic() - connected_at > 45 and not self.health()['ready']:
                        raise RuntimeError('Account stream became stale')
                    continue
                if message == 'Ping':
                    ws.send('Pong')
                elif message.get('code', 0) != 0:
                    raise RuntimeError('Account stream rejected subscription')
                elif message.get('e') == 'listenKeyExpired':
                    raise RuntimeError('Account stream expired')
                else:
                    record_event(self.path, message)
                with self.lock:
                    self.last_seen, self.connected, self.error_type = time.monotonic(), True, None
                    if message != 'Ping' and message.get('e') in (
                            'ACCOUNT_CONFIG_UPDATE', 'ACCOUNT_UPDATE', 'TRADE_UPDATE', 'ORDER_TRADE_UPDATE'):
                        self.last_event_type = message['e']

    def start(self):
        def run():
            delay = 1
            while True:
                started = time.monotonic()
                try:
                    self.session()
                except Exception as error:
                    with self.lock:
                        self.connected, self.error_type = False, type(error).__name__
                    # Never log URLs, keys, exception messages or raw events.
                    print(json.dumps({'copy_status': 'account_stream_reconnecting',
                                      'error_type': type(error).__name__,
                                      'error_code': getattr(error, 'code', None)}), flush=True)
                if time.monotonic() - started > 60:
                    delay = 1
                time.sleep(delay)
                delay = min(delay * 2, 30)
        threading.Thread(target=run, name='bingx-account-readonly', daemon=True).start()
