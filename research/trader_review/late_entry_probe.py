"""Three-day market-data-only experiment. No credentials or trading endpoints."""
import json
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

DAY_MS = 86_400_000
RULES = {
    'paper_capital_usdt': 100,
    'entry_delay_min_ms': 30_000,
    'entry_delay_max_ms': 1_800_000,
    'max_observed_excursion_fraction': 0.003,
    'entry_price_tolerance_fraction': 0.001,
    'max_quote_gap_ms': 15_000,
    'assumed_taker_fee_fraction_per_side': 0.0005,
    'cancel_pending_on_source_reduction': True,
    'require_funding_costs_for_final_verdict': True,
    'minimum_completed_late_cycles_for_pass': 5,
    'require_positive_net_and_not_worse_than_immediate': True,
    'real_orders_enabled': False,
}


def quote(symbol):
    url = 'https://open-api.bingx.com/openApi/swap/v2/quote/bookTicker?' + urlencode({'symbol': symbol})
    with urlopen(url, timeout=4) as response:
        body = json.load(response)
    if body['code'] != 0:
        raise ValueError('Public quote API rejection')
    data = body['data']
    if isinstance(data, list):
        data = next(row for row in data if row['symbol'] == symbol)
    bid, ask = float(data.get('bidPrice',data.get('bid_price'))), float(data.get('askPrice',data.get('ask_price')))
    if not 0 < bid <= ask:
        raise ValueError('Invalid book')
    return (int(data.get('time') or data.get('book_ticker',{}).get('lastUpdateId') or time.time_ns() // 1_000_000), bid, ask,
            float(data.get('bidQty') or data.get('bid_qty') or 0), float(data.get('askQty') or data.get('ask_qty') or 0))


def initialize(out, source, now_ms):
    out.executescript('''
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS source_events
          (id TEXT PRIMARY KEY,source_row INTEGER,seen_ms INTEGER,payload TEXT,fill TEXT);
        CREATE TABLE IF NOT EXISTS quotes
          (sample_ms INTEGER,symbol TEXT,exchange_ms INTEGER,bid REAL,ask REAL,bid_qty REAL,ask_qty REAL);
        CREATE INDEX IF NOT EXISTS quote_symbol_time ON quotes(symbol,sample_ms);
        CREATE TABLE IF NOT EXISTS health (sample_ms INTEGER,source_poll_ms INTEGER);
        CREATE TABLE IF NOT EXISTS errors (sample_ms INTEGER,kind TEXT);
        CREATE TABLE IF NOT EXISTS source_account (sample_ms INTEGER,payload TEXT);
    ''')
    with out:
        if not out.execute("SELECT 1 FROM meta WHERE key='start_ms'").fetchone():
            cursor = source.execute('SELECT coalesce(max(rowid),0) FROM observations').fetchone()[0]
            for k, v in {'start_ms': now_ms, 'end_ms': now_ms+3*DAY_MS,
                         'cursor': cursor, 'rules': RULES, 'status': 'collecting'}.items():
                out.execute('INSERT INTO meta VALUES (?,?)', (k, json.dumps(v)))
    return {k: json.loads(v) for k,v in out.execute('SELECT key,value FROM meta')}


def sample(out, source, state, now_ms, get_quote=quote, get_account=None):
    if now_ms >= state['end_ms']:
        with out:
            out.execute("UPDATE meta SET value=? WHERE key='status'", (json.dumps('complete'),))
        return False
    rows = source.execute('''SELECT o.rowid,o.order_id,o.payload,f.payload FROM observations o
        LEFT JOIN hl_fills f ON f.id=substr(o.order_id,4,length(o.order_id)-
          CASE WHEN o.order_id LIKE '%:close' THEN 9 ELSE 8 END)
        WHERE o.rowid>? AND o.category='observed' ORDER BY o.rowid''', (state['cursor'],)).fetchall()
    with out:
        for rowid, signal, payload, fill in rows:
            out.execute('INSERT OR IGNORE INTO source_events VALUES (?,?,?,?,?)',
                        (signal,rowid,now_ms,payload,fill))
            state['cursor'] = rowid
        out.execute("UPDATE meta SET value=? WHERE key='cursor'", (json.dumps(state['cursor']),))
        poll = source.execute('SELECT max(observed_ms) FROM polls').fetchone()[0]
        out.execute('INSERT INTO health VALUES (?,?)', (now_ms,poll))
    if rows and get_account is not None:
        try:
            account = get_account()
            with out:
                out.execute('INSERT INTO source_account VALUES (?,?)', (now_ms,json.dumps(account)))
        except Exception as error:
            with out:
                out.execute('INSERT INTO errors VALUES (?,?)',(now_ms,'source_account:'+type(error).__name__))
    symbols = {'HYPE-USDT'}
    for raw, in out.execute('SELECT payload FROM source_events'):
        coin = json.loads(raw)['symbol']
        symbols.add(coin[:-4]+'-USDT')
    for symbol in sorted(symbols):
        try:
            exchange_ms,bid,ask,bid_qty,ask_qty = get_quote(symbol)
            stamp = time.time_ns() // 1_000_000
            with out:
                out.execute('INSERT INTO quotes VALUES (?,?,?,?,?,?,?)',
                            (stamp,symbol,exchange_ms,bid,ask,bid_qty,ask_qty))
        except Exception as error:
            with out:
                out.execute('INSERT INTO errors VALUES (?,?)', (now_ms,'quote:'+symbol+':'+type(error).__name__))
    return True


def run(source_path):
    path = Path(source_path).with_name('hyperliquid-late-entry-test.sqlite')
    source = sqlite3.connect('file:'+str(source_path)+'?mode=ro',uri=True,timeout=2)
    source.execute('PRAGMA query_only=ON')
    out = sqlite3.connect(path,timeout=2)
    out.execute('PRAGMA journal_mode=WAL')
    state = initialize(out,source,time.time_ns()//1_000_000)
    from .hyperliquid_feed import PublicInfo
    address = source.execute("SELECT value FROM meta WHERE key='hl_address'").fetchone()[0]
    def get_account():return PublicInfo().get('clearinghouseState',user=address)
    while time.time_ns()//1_000_000 < state['end_ms']:
        try:
            if not sample(out,source,state,time.time_ns()//1_000_000,get_account=get_account):
                break
        except Exception as error:
            print(json.dumps({'late_probe':'error','kind':type(error).__name__}),flush=True)
        time.sleep(5)
    with out:
        out.execute("UPDATE meta SET value=? WHERE key='status'", (json.dumps('complete'),))
    out.close()
    source.close()


def start_probe(source_path):
    # A separate connection and thread ensure quote/network failures never
    # delay source polling, entry deadlines or existing-position reductions.
    def guarded():
        try:
            run(source_path)
        except Exception as error:
            print(json.dumps({'late_probe':'stopped','kind':type(error).__name__}),flush=True)
    thread = threading.Thread(target=guarded,name='late-entry-data-only',daemon=True)
    thread.start()
    return thread
