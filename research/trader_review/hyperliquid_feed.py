"""Public Hyperliquid fills -> existing paper engine; no signing/order API."""
import hashlib
import json
import math
import re
import time
import urllib.request
from .finmonster_feed import canonical

ADDRESS = re.compile(r'0x[0-9a-fA-F]{40}')
READ_TYPES = frozenset(('userFillsByTime', 'clearinghouseState', 'portfolio', 'meta'))

class PublicInfo:
    def get(self, kind, **params):
        if kind not in READ_TYPES:
            raise ValueError('Only public read operations are allowed')
        request = urllib.request.Request('https://api.hyperliquid.xyz/info',
            canonical(dict(type=kind, **params)).encode(),
            {'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)


def finite(value, positive=False):
    if isinstance(value, bool):
        raise ValueError('Invalid number')
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0):
        raise ValueError('Invalid number')
    return result


def normalize(fill, epochs, now):
    coin = fill['coin']
    if not re.fullmatch(r'[A-Z0-9]+', coin):
        return []  # HIP-3/spot are explicitly unsupported by the BingX paper engine.
    stamp = fill['time']
    if not isinstance(stamp, int) or isinstance(stamp, bool) or not 0 < stamp <= now+1000:
        raise ValueError('Invalid source time')
    side = {'B': 1, 'A': -1}.get(fill['side'])
    if side is None:
        raise ValueError('Unknown fill side')
    qty, price, start = finite(fill['sz'], True), finite(fill['px'], True), finite(fill['startPosition'])
    fid = f"hl:{coin}:{stamp}:{fill['tid']}"
    before_side = 1 if start > 0 else -1 if start < 0 else 0
    output = []
    if before_side and before_side != side:
        close = min(abs(start), qty)
        epoch = epochs.get(coin, f'{coin}:prebaseline:{before_side}')
        output.append(dict(orderId=fid+':close', positionId=epoch, symbol=coin+'USDT',
            side='BUY' if side == 1 else 'SELL', action='CLOSE', status='Filled',
            updatedAt=stamp, quantity=close, price=price))
        qty -= close
        if close >= abs(start):
            epochs.pop(coin, None)
    if qty > 1e-12:
        if not before_side or before_side != side:
            epochs[coin] = fid+':position'
        else:
            epochs.setdefault(coin, f'{coin}:prebaseline:{side}')
        output.append(dict(orderId=fid+':open', positionId=epochs[coin], symbol=coin+'USDT',
            side='BUY' if side == 1 else 'SELL', action='OPEN', status='Filled',
            updatedAt=stamp, quantity=qty, price=price,
            sourcePositionAfter=abs(start + (1 if side == 1 else -1)*finite(fill['sz'], True))))
    return output


class FillObserver:
    """Atomic cursor + journal. Historical snapshot never becomes an entry."""
    def __init__(self, db, address, client=None):
        if not ADDRESS.fullmatch(address):
            raise ValueError('Invalid public trader address')
        self.db, self.address, self.client = db, address.lower(), client or PublicInfo()
        db.execute('CREATE TABLE IF NOT EXISTS hl_fills(id TEXT PRIMARY KEY,time INTEGER,payload TEXT)')
        old = db.execute("SELECT value FROM meta WHERE key='hl_address'").fetchone()
        if old and old[0] != self.address:
            raise ValueError('Separate database required for a different trader')
        with db:
            db.execute("INSERT OR IGNORE INTO meta VALUES('hl_address',?)", (self.address,))

    def poll(self, now=None):
        now = int(time.time()*1000) if now is None else now
        row = self.db.execute("SELECT value FROM meta WHERE key='hl_cursor'").fetchone()
        saved = json.loads(row[0]) if row else None
        start = max(0, saved['time']-1000) if saved else now-86400000
        fills = self.client.get('userFillsByTime', user=self.address, startTime=start, endTime=now)
        if not isinstance(fills, list) or len(fills) >= 2000:
            raise ValueError('Invalid or capped fill history; refusing cursor advance')
        # An overlapping anchor proves continuity; never silently resume over a history gap.
        def identity(f):
            return f"{f['coin']}:{f['time']}:{f['tid']}"
        if saved and saved.get('anchor') and not any(identity(f) == saved['anchor'] for f in fills):
            raise ValueError('Source history gap; refusing cursor advance')
        epochs = dict(saved.get('epochs', {})) if saved else {}
        baseline_ms = saved.get('baseline_ms', now) if saved else now
        expected = dict(saved.get('positions', {})) if saved else {}
        prepared = []
        for fill in sorted(fills, key=lambda f:(f['time'], int(f['tid']), f['coin'])):
            fid = identity(fill)
            if self.db.execute('SELECT 1 FROM hl_fills WHERE id=?',(fid,)).fetchone():
                continue
            coin = fill['coin']
            if re.fullmatch(r'[A-Z0-9]+', coin):
                before = finite(fill['startPosition'])
                if coin in expected and not math.isclose(before, expected[coin], rel_tol=1e-8, abs_tol=1e-8):
                    raise ValueError('Source position discontinuity; refusing cursor advance')
                expected[coin] = before + (1 if fill['side'] == 'B' else -1)*finite(fill['sz'], True)
            orders = normalize(fill, epochs, now)
            prepared.append((fid, fill, orders))
        latest = max(fills, key=lambda f:(f['time'],int(f['tid']),f['coin'])) if fills else None
        cursor = dict(time=latest['time'] if latest else now,
                      anchor=identity(latest) if latest else None, epochs=epochs, baseline_ms=baseline_ms, positions=expected)
        with self.db:
            for fid, fill, orders in prepared:
                self.db.execute('INSERT INTO hl_fills VALUES(?,?,?)',(fid,fill['time'],canonical(fill)))
                for order in orders:
                    body=canonical(order)
                    self.db.execute('INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?,?)',
                        (order['orderId'],hashlib.sha256(body.encode()).hexdigest(),order['updatedAt'],now,
                         'observed' if saved and order['updatedAt'] > baseline_ms else 'baseline',body))
            self.db.execute("INSERT OR REPLACE INTO meta VALUES('hl_cursor',?)",(canonical(cursor),))
            self.db.execute('INSERT INTO polls VALUES(?,?,?)',(now,0,canonical({
                'source':'hyperliquid','fills':len(prepared),'baseline':saved is None})))
        return dict(observed_at_ms=now, baseline=saved is None, new_fills=len(prepared),
                    latest_source_ms=cursor['time'])
