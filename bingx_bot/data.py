"""Public GET-only client; explicit allowlist makes order submission impossible."""
from dataclasses import dataclass, asdict
from urllib.request import Request, urlopen
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from decimal import Decimal, ROUND_FLOOR
import json
import math
import time
import hashlib
import gzip
from pathlib import Path
from .config import BAR, SYMBOLS


class PublicAPIUnavailable(RuntimeError):
    """Retryable public-data outage; never permission to trade on stale data."""
    def __init__(self, endpoint, reason, retry_after=30):
        self.endpoint, self.retry_after = endpoint, retry_after
        super().__init__(f'{endpoint}: {reason}')

@dataclass(frozen=True)
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float

@dataclass(frozen=True)
class Funding:
    ts: int
    rate: float
    mark: float | None = None

@dataclass(frozen=True)
class Rules:
    step: str
    min_qty: float
    min_notional: float
    source: str = 'BingX contract snapshot'

    def floor(self, qty):
        s = Decimal(self.step)
        if not s.is_finite() or s <= 0:
            raise ValueError('Invalid quantity step')
        return float((Decimal(str(qty)) / s).to_integral_value(rounding=ROUND_FLOOR) * s)

def symbol_scope(symbols):
    import re
    values = tuple(symbols)
    if not values or len(set(values)) != len(values) or any(
        not isinstance(v, str) or not re.fullmatch(r'[A-Z0-9]+-USDT', v) for v in values
    ):
        raise ValueError('Invalid or duplicate USDT symbol scope')
    return values

class PublicClient:
    BASE = 'https://open-api.bingx.com'
    PATHS = {
        'klines': '/openApi/swap/v3/quote/klines',
        'contracts': '/openApi/swap/v2/quote/contracts',
        'funding': '/openApi/swap/v2/quote/fundingRate',
        'book': '/openApi/swap/v2/quote/bookTicker',
        'premium': '/openApi/swap/v2/quote/premiumIndex',
        'time': '/openApi/swap/v2/server/time',
    }

    def __init__(self, symbols=SYMBOLS):
        self.symbols = symbol_scope(symbols)
        self.last_request = 0.0
        self.cooldown_until = 0.0
        self.candle_cache = {}

    def unavailable(self, endpoint, reason, delay):
        self.cooldown_until = max(self.cooldown_until, time.monotonic()+delay)
        return PublicAPIUnavailable(endpoint,reason,delay)

    def get(self, endpoint, **params):
        path = self.PATHS[endpoint]  # rejects all non-allowlisted endpoints
        if 'symbol' in params and params['symbol'] not in self.symbols:
            raise ValueError('Unsupported contract')
        remaining = self.cooldown_until-time.monotonic()
        if remaining > 0:
            raise PublicAPIUnavailable(endpoint,'public API cooldown',remaining)
        for attempt in range(4):
            # Conservative process-wide pacing, not an assumed exchange limit.
            time.sleep(max(0, 2.0 - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            req = Request(self.BASE + path + '?' + urlencode(params),
                          headers={'User-Agent': 'bingx-paper-research/0.1'}, method='GET')
            try:
                with urlopen(req, timeout=20) as response:
                    result = json.load(response)
                if result.get('code') != 0:
                    if result.get('code') in (100410,100500,100503):
                        delay = 60 if result['code'] == 100410 else 30
                        raise self.unavailable(endpoint,f'BingX code {result["code"]}',delay)
                    raise ValueError(f'BingX error: {result.get("code")} {result.get("msg")}')
                return result['data']
            except HTTPError as e:
                if e.code in (418,429):
                    delay = 1200 if e.code == 418 else 60
                    try:delay = max(delay,float(e.headers.get('Retry-After',0)))
                    except (ValueError,TypeError,AttributeError):pass
                    raise self.unavailable(endpoint,f'HTTP {e.code}',delay) from e
                if e.code not in (429, 500, 502, 503, 504) or attempt == 3:
                    if e.code in (500,502,503,504):
                        raise self.unavailable(endpoint,f'HTTP {e.code}',30) from e
                    raise
            except (URLError, TimeoutError, ConnectionError) as e:
                if attempt == 3:
                    raise self.unavailable(endpoint,type(e).__name__,30) from e
            time.sleep(2 ** attempt)
        raise RuntimeError('Unreachable')

    def candles(self, symbol, start, end):
        rows = {}
        cursor = start
        while cursor < end:
            cache = self.candle_cache.setdefault(symbol,{})
            if cursor in cache:
                rows[cursor] = cache[cursor]
                cursor += BAR
                continue
            # Explicit bounded chunks; fail on truncated/empty windows, never fill gaps.
            edge = min(end, cursor + 1000 * BAR)
            page = self.get('klines', symbol=symbol, interval='15m',
                            startTime=cursor, endTime=edge - 1, limit=1000)
            if not page:
                raise ValueError(f'Empty history window {symbol} {cursor}')
            chunk = {}
            for x in page:
                if isinstance(x, dict):
                    c = Candle(int(x['time']), *(float(x[k]) for k in ('open','high','low','close','volume')))
                else:
                    c = Candle(int(x[0]), *(float(v) for v in x[1:6]))
                if cursor <= c.ts < edge:
                    if c.ts in chunk and chunk[c.ts] != c:
                        raise ValueError('Conflicting duplicate candle')
                    if c.ts in cache and cache[c.ts] != c:
                        raise ValueError('Conflicting cached candle')
                    chunk[c.ts] = c
                if start <= c.ts < end:
                    if c.ts in rows and rows[c.ts] != c:
                        raise ValueError('Conflicting duplicate candle')
                    rows[c.ts] = c
            validate(sorted(chunk.values(),key=lambda c:c.ts),cursor,edge)
            cache.update(chunk)
            # Keep verified startup progress across API outages and rolling
            # close times, with bounded RAM even for multiyear history fetches.
            if len(cache)>8192:
                for ts in sorted(cache)[:-8192]:del cache[ts]
            cursor = edge
        out = sorted(rows.values(), key=lambda c: c.ts)
        validate(out, start, end)
        return out

    def funding(self, symbol, start, end):
        rows = {}
        # Small time windows avoid silent API default truncation.
        cursor = start
        while cursor < end:
            edge = min(end, cursor + 20 * 24 * 3600_000)
            # BingX returns data:null when a narrow window has no settlement.
            # Fetch a full day of verifiable history rather than assuming null
            # means zero funding; emit only events in the requested interval.
            query_start = min(cursor, edge - 24 * 3600_000)
            page = self.get('funding', symbol=symbol, startTime=query_start, endTime=edge-1, limit=1000)
            if not isinstance(page, list):
                raise ValueError('Funding history is not a list')
            if len(page) >= 1000:
                raise ValueError('Potential funding truncation')
            coverage = []
            for x in page:
                f = Funding(int(x['fundingTime']), float(x['fundingRate']), float(x['markPrice']) if x.get('markPrice') else None)
                if not math.isfinite(f.rate) or abs(f.rate) > .1 or f.ts % BAR or (f.mark is not None and (not math.isfinite(f.mark) or f.mark <= 0)):
                    raise ValueError('Invalid/unaligned funding')
                if query_start <= f.ts < edge:
                    coverage.append(f.ts)
                if start <= f.ts < end:
                    if f.ts in rows and rows[f.ts] != f:
                        raise ValueError('Conflicting funding')
                    rows[f.ts] = f
            if query_start < cursor:
                points = [query_start] + sorted(set(coverage)) + [edge]
                if not coverage or max(b-a for a,b in zip(points,points[1:])) > 12*3600_000:
                    raise ValueError('Funding coverage gap >12h')
            cursor = edge
        out = sorted(rows.values(), key=lambda f: f.ts)
        # Coverage heuristic only: BingX can change settlement intervals.
        # Refuse sparse/missing history instead of treating unknown funding as zero.
        points = [start] + [f.ts for f in out] + [end]
        if end-start >= 24*3600_000 and (not out or max(b-a for a,b in zip(points, points[1:])) > 12*3600_000):
            raise ValueError('Funding coverage gap >12h')
        return out

    def rules(self):
        raw = self.get('contracts')
        out = {}
        for x in raw:
            symbol = x['symbol']
            if symbol in self.symbols:
                if int(x['status']) != 1 or str(x['apiStateOpen']).lower() != 'true' or str(x['apiStateClose']).lower() != 'true':
                    raise ValueError('Contract API inactive')
                precision = int(x['quantityPrecision'])
                # Confirm minQty is present; no arbitrary fallback trading size.
                minimum = float(x['tradeMinQuantity'])
                notional = float(x['tradeMinUSDT'])
                if not 0 <= precision <= 12 or not all(math.isfinite(v) and v > 0 for v in (minimum,notional)):
                    raise ValueError('Invalid contract minimums')
                out[symbol] = Rules(str(Decimal(10) ** -precision), minimum, notional)
        if set(out) != set(self.symbols):
            raise ValueError('Missing requested contract rules')
        return out, raw

    def quote(self, symbol):
        x = self.get('book', symbol=symbol)
        if isinstance(x, list):
            if len(x) != 1:
                raise ValueError('Unexpected book response')
            x = x[0]
        x = x.get('book_ticker', x)
        bid, ask = float(x['bid_price']), float(x['ask_price'])
        ts = int(x['time'])
        if not all(math.isfinite(p) and p > 0 for p in (bid, ask)) or ask < bid:
            raise ValueError('Invalid book')
        return bid, ask, ts

    def now(self):
        return int(self.get('time')['serverTime'])


def validate(rows, start=None, end=None):
    if not rows:
        raise ValueError('No candles')
    for i, c in enumerate(rows):
        if c.ts % BAR or any(not math.isfinite(v) for v in (c.open,c.high,c.low,c.close,c.volume)):
            raise ValueError('Invalid timestamp/non-finite candle')
        if not (0 < c.low <= min(c.open,c.close) <= max(c.open,c.close) <= c.high and c.volume >= 0):
            raise ValueError('Invalid OHLCV')
        if i and c.ts - rows[i-1].ts != BAR:
            raise ValueError('Missing/duplicate/out-of-order candles')
    if start is not None and rows[0].ts != start:
        raise ValueError('History start truncated')
    if end is not None and rows[-1].ts + BAR != end:
        raise ValueError('History end truncated')


def save_dataset(path, candles, funding, rules, metadata=None, symbols=SYMBOLS):
    symbols = symbol_scope(symbols)
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for symbol in symbols:
        data = {'candles':[asdict(x) for x in candles[symbol]],
                'funding':[asdict(x) for x in funding[symbol]], 'rules':asdict(rules[symbol])}
        raw = json.dumps(data, sort_keys=True).encode()
        (path / f'{symbol}.json').write_bytes(raw)
        hashes[symbol] = hashlib.sha256(raw).hexdigest()
    (path/'manifest.json').write_text(json.dumps({'source': PublicClient.BASE, 'sha256':hashes,
        'retrieved_at_ms':int(time.time()*1000), 'metadata':metadata or {}}, indent=2))


def load_dataset(path, symbols=SYMBOLS):
    symbols = symbol_scope(symbols)
    path = Path(path)
    manifest = json.loads((path/'manifest.json').read_text())
    candles, funding, rules = {}, {}, {}
    for symbol in symbols:
        file = path/f'{symbol}.json'
        raw = file.read_bytes() if file.exists() else gzip.decompress((path/f'{symbol}.json.gz').read_bytes())
        if hashlib.sha256(raw).hexdigest() != manifest['sha256'][symbol]:
            raise ValueError('Dataset checksum mismatch')
        data = json.loads(raw)
        candles[symbol] = [Candle(**x) for x in data['candles']]
        validate(candles[symbol])
        funding[symbol] = [Funding(**x) for x in data['funding']]
        fs = funding[symbol]
        if any(not math.isfinite(f.rate) or abs(f.rate) > .1 or f.ts % BAR or (f.mark is not None and (not math.isfinite(f.mark) or f.mark <= 0)) for f in fs):
            raise ValueError('Invalid funding')
        if any(a.ts >= b.ts for a,b in zip(fs,fs[1:])):
            raise ValueError('Funding duplicates/order')
        points = [candles[symbol][0].ts]+[f.ts for f in fs]+[candles[symbol][-1].ts+BAR]
        if points[-1]-points[0] >= 24*3600_000 and (not fs or max(b-a for a,b in zip(points,points[1:])) > 12*3600_000):
            raise ValueError('Funding coverage gap')
        rules[symbol] = Rules(**data['rules'])
    if any([c.ts for c in candles[s]] != [c.ts for c in candles[symbols[0]]] for s in symbols):
        raise ValueError('Portfolio histories not aligned')
    return candles, funding, rules, manifest
