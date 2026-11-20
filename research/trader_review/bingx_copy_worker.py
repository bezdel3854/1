"""Run an isolated Hyperliquid-to-BingX copy worker."""
import argparse
import fcntl
import json
import os
import re
import time
from decimal import Decimal
from pathlib import Path

from .bingx_execution import BingXAPIError, BingXExecution, HyperliquidBingXCopy, ReconciliationRequired
from .finmonster_feed import Observer
from .hyperliquid_feed import FillObserver
from .full_copy_contract import CopyBlocked
from .strict_bingx_copy import StrictBingXExecution
from .hyperliquid_feed import PublicInfo


def cycle(source, copier, execution, *, min_equity=Decimal('100'), allowed_symbols=None, copy_allocation=None):
    source.poll()
    db = copier.db
    if hasattr(execution, 'synchronize_exits'):
        execution.open_blocker = None
        try:
            execution.synchronize_exits(db)
        except (CopyBlocked, ReconciliationRequired, BingXAPIError) as error:
            execution.open_blocker = str(error)
            print(json.dumps({'copy_status': 'protection_blocked', 'error': str(error)}), flush=True)
    rows = db.execute(
        """SELECT o.rowid, o.payload, f.payload FROM observations o
           LEFT JOIN hl_fills f ON f.id = substr(o.order_id, 4, length(o.order_id) -
               CASE WHEN o.order_id LIKE '%:close' THEN 9 ELSE 8 END)
           WHERE o.category='observed' AND o.rowid > coalesce(
               (SELECT cast(value AS INTEGER) FROM meta WHERE key='bingx_copy_row'), 0)
           ORDER BY o.rowid"""
    ).fetchall()
    for rowid, payload, raw in rows:
        event = json.loads(payload)
        if copier.skip_stale_entry(event):
            with db:
                db.execute("INSERT OR REPLACE INTO meta VALUES ('bingx_copy_row',?)", (str(rowid),))
            continue
        fill = json.loads(raw)
        before = Decimal(fill["startPosition"])
        if event["action"] == "OPEN" and ((before < 0 and event["side"] == "BUY")
                                         or (before > 0 and event["side"] == "SELL")):
            before = Decimal(0)
        seed = copier.can_seed_from_add(event, before)
        managed = db.execute(
            "SELECT 1 FROM copied_positions WHERE epoch=?", (event["positionId"],)
        ).fetchone()
        planned = db.execute(
            "SELECT 1 FROM copy_plans WHERE id=?", (event["orderId"],)
        ).fetchone()
        if event["updatedAt"] > copier.cutover_ms and (
            ":prebaseline:" not in event["positionId"] or seed or managed or planned
        ):
            symbol = event["symbol"][:-4] + "-USDT"
            if allowed_symbols is not None and symbol not in allowed_symbols and not managed and not planned:
                with db:
                    db.execute("INSERT OR IGNORE INTO copy_skips VALUES (?,?,?,?,?)",
                               (event['orderId'], event['updatedAt'], time.time_ns() // 1_000_000,
                                'symbol_not_selected', json.dumps(event, sort_keys=True)))
                    db.execute("INSERT OR REPLACE INTO meta VALUES ('bingx_copy_row',?)", (str(rowid),))
                continue
            contracts = execution._request("GET", "quote/contracts", symbol=symbol)
            contract = next((item for item in contracts if item["symbol"] == symbol), None)
            if contract is None:
                if managed or planned:
                    raise ReconciliationRequired(f'Contract unavailable for managed/planned signal: {symbol}')
                with db:
                    db.execute("INSERT OR IGNORE INTO copy_skips VALUES (?,?,?,?,?)",
                               (event['orderId'], event['updatedAt'], time.time_ns() // 1_000_000,
                                'unsupported_contract', json.dumps(event, sort_keys=True)))
                    db.execute("INSERT OR REPLACE INTO meta VALUES ('bingx_copy_row',?)", (str(rowid),))
                continue
            ticker = execution._request("GET", "quote/price", symbol=symbol)
            market = (
                Decimal(str(ticker["price"])),
                Decimal(1).scaleb(-int(contract["quantityPrecision"])),
                Decimal(str(contract["tradeMinQuantity"])),
                Decimal(str(contract["tradeMinUSDT"])),
            )
            ratio = Decimal(0)
            opening = event["action"] == "OPEN" and not managed and (not before or seed)
            if opening and not planned:
                account = source.client.get("clearinghouseState", user=source.address)
                source_equity = Decimal(account["marginSummary"]["accountValue"])
                if not source_equity.is_finite() or source_equity <= 0:
                    raise ValueError("Invalid Hyperliquid equity")
                equity = execution.equity()
                if copy_allocation is not None:
                    if execution.environment != 'demo':
                        raise CopyBlocked('fixed_demo_allocation_cannot_be_used_on_live')
                    equity = min(equity, copy_allocation)
                if not equity.is_finite() or equity < min_equity:
                    # A signal seen before funding must never be replayed after funding.
                    with db:
                        db.execute("INSERT OR REPLACE INTO meta VALUES ('bingx_copy_row',?)", (str(rowid),))
                    continue
                ratio = equity / source_equity
                if copy_allocation is not None and hasattr(execution, 'copy_ratio'):
                    ratio = execution.copy_ratio(account, equity)
            if hasattr(execution, 'set_context'):
                saved_ratio = db.execute('SELECT ratio FROM copied_positions WHERE epoch=?',
                                         (event['positionId'],)).fetchone()
                if not saved_ratio:
                    saved_ratio = db.execute('SELECT ratio FROM copy_plans WHERE id=?',
                                             (event['orderId'],)).fetchone()
                execution.set_context(event, abs(before), Decimal(saved_ratio[0]) if saved_ratio else ratio)
            try:
                copier.apply(event, abs(before), ratio, market)
            except CopyBlocked as error:
                if event['action'] != 'OPEN' or execution.needs_reconciliation(event['orderId']):
                    raise
                # A refused, UNSENT entry is terminal, never a delayed catch-up.
                # Continue processing later reductions in this same poll.
                with db:
                    db.execute('INSERT OR IGNORE INTO copy_skips VALUES (?,?,?,?,?)',
                               (event['orderId'], event['updatedAt'], time.time_ns()//1_000_000,
                                str(error), json.dumps(event, sort_keys=True)))
                print(json.dumps({'copy_status': 'entry_blocked', 'reason': str(error)}), flush=True)
        with db:
            db.execute("INSERT OR REPLACE INTO meta VALUES ('bingx_copy_row',?)", (str(rowid),))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--address", required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--enable-orders", action="store_true")
    parser.add_argument("--copy-add-once", action="store_true")
    parser.add_argument("--min-equity", type=Decimal, default=Decimal('100'),
                        help="Minimum actual account equity for a new entry; default 100 USDT")
    parser.add_argument("--symbols", nargs='+', help="Optional BingX symbols allowed for new positions")
    args = parser.parse_args()
    if not args.min_equity.is_finite() or args.min_equity < Decimal('10'):
        parser.error('--min-equity must be finite and at least 10 USDT')
    if args.symbols and any(not re.fullmatch(r'[A-Z0-9]+-USDT', s) for s in args.symbols):
        parser.error('--symbols must use the form BTC-USDT')
    if args.db.name != "hyperliquid-bingx-live.sqlite":
        parser.error("Use a dedicated hyperliquid-bingx-live.sqlite database")
    if args.enable_orders:
        if not os.environ.get('BINGX_ACCOUNT_UID', '').isdigit():
            parser.error('Set BINGX_ACCOUNT_UID from the verified intended exchange account before enabling orders')
        execution = StrictBingXExecution(
            os.environ['BINGX_API_KEY'], os.environ['BINGX_API_SECRET'], args.db,
            trading_enabled=True, source_client=PublicInfo(), source_address=args.address,
            expected_uid=os.environ['BINGX_ACCOUNT_UID'])
    else:
        execution = BingXExecution(os.environ['BINGX_API_KEY'], os.environ['BINGX_API_SECRET'],
                                   ':memory:', trading_enabled=False)
    if not args.enable_orders:
        while True:
            print(json.dumps({"balance_read": execution.equity().is_finite(),
                              "positions_read": isinstance(execution._request("GET", "user/positions"), list),
                              "real_orders_enabled": False}))
            if args.once:
                return
            time.sleep(60)
    with args.db.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        observer = Observer(args.db, args.address.lower(), "Hyperliquid BingX copy")
        source = FillObserver(observer.db, args.address)
        copier = HyperliquidBingXCopy(observer.db, execution, time.time_ns() // 1_000_000)
        if args.copy_add_once:
            copier.arm_add_once(time.time_ns() // 1_000_000)
        from .late_entry_probe import start_probe
        start_probe(args.db)
        while True:
            try:
                cycle(source, copier, execution, min_equity=args.min_equity,
                      allowed_symbols=frozenset(args.symbols) if args.symbols else None)
            except (BingXAPIError, ReconciliationRequired, CopyBlocked) as error:
                # Keep polling, but do not advance the failed signal, alter the
                # position journal, or send another POST for its client ID.
                print(json.dumps({"copy_status": "blocked", "error": str(error)}), flush=True)
                if args.once:
                    raise
            if args.once:
                return
            time.sleep(5)


if __name__ == "__main__":
    main()

