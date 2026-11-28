"""Read-only health snapshots, independent of the order dispatch loop."""
import json
import sqlite3
import threading
import time
from decimal import Decimal
from pathlib import Path


def snapshot(execution, paths, now_ms=None):
    now_ms = time.time_ns() // 1_000_000 if now_ms is None else now_ms
    result = {'copy_status': 'heartbeat', 'checked_ms': now_ms, 'sources': []}
    for path in paths:
        # A separate read-only connection is required in the diagnostics thread.
        with sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=1) as db:
            poll = db.execute('SELECT max(observed_ms) FROM polls').fetchone()[0]
            source = db.execute("SELECT value FROM meta WHERE key='hl_address'").fetchone()
            cursor = db.execute("SELECT value FROM meta WHERE key='hl_cursor'").fetchone()
            result['sources'].append({
                'address': source[0] if source else None, 'poll_ms': poll,
                'exact_source_positions': bool(cursor and 'exact_positions' in json.loads(cursor[0])),
                'poll_age_ms': max(0, now_ms - poll) if poll is not None else None,
                'managed_positions': db.execute('SELECT count(*) FROM copied_positions').fetchone()[0],
                'copied_signals': db.execute('SELECT count(*) FROM copied_signals').fetchone()[0],
                'skipped_signals': db.execute('SELECT count(*) FROM copy_skips').fetchone()[0],
                'pending_entries': db.execute('SELECT count(*) FROM copy_entry_children WHERE adopted=0').fetchone()[0],
                'pending_exits': db.execute('SELECT count(*) FROM copy_exit_dispatch WHERE result IS NULL').fetchone()[0],
                'pending_signals': db.execute(
                    'SELECT count(*) FROM copy_plans p LEFT JOIN copied_signals s ON s.id=p.id '
                    'LEFT JOIN copy_skips k ON k.id=p.id WHERE s.id IS NULL AND k.id IS NULL').fetchone()[0],
            })
    # Signed reads only. A failed API call must never emit a successful balance check.
    try:
        execution.verify_account_identity()
        equity = execution.equity()
        if not equity.is_finite():
            raise ValueError('Invalid equity')
        positions = execution._request('GET', 'user/positions')
        if not isinstance(positions, list):
            raise ValueError('Invalid positions')
        result.update(account_uid_verified=True, actual_exchange_equity=str(equity),
                      exchange_positions=sum(Decimal(p['positionAmt']) != 0 for p in positions),
                      exchange_open_orders=len(execution._orders(None)))
    except Exception as error:
        # Never expose exchange messages, signed URLs or credentials in diagnostics.
        result['exchange_check_error'] = type(error).__name__
    result['completed_ms'] = time.time_ns() // 1_000_000
    account_stream = execution.__dict__.get('account_stream')
    if account_stream is not None:
        result['account_stream'] = account_stream.health()
    return result


def start_health(execution, paths):
    def run():
        while True:
            try:
                status = snapshot(execution, paths)
            except Exception as error:
                status = {'copy_status': 'heartbeat_error', 'error_type': type(error).__name__}
            print(json.dumps(status), flush=True)
            time.sleep(60)
    threading.Thread(target=run, name='copy-health-readonly', daemon=True).start()
