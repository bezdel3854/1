"""Run an isolated Hyperliquid-to-BingX copy worker."""
import argparse
import hashlib
import fcntl
import json
import os
import re
import time
from decimal import Decimal
from pathlib import Path

from .bingx_execution import BingXAPIError, BingXExecution, HyperliquidBingXCopy, ReconciliationRequired
from .finmonster_feed import Observer
from .hyperliquid_feed import ADDRESS, FillObserver, PublicInfo
from .full_copy_contract import CopyBlocked
from .strict_bingx_copy import StrictBingXExecution


def batch_executed_entries(db, rows, *, same_timestamp=False):
    """Persist contiguous fills of one source order before any exchange write.

    A saved batch is immutable across restart, even when more fills arrive.
    This avoids spending an entire entry deadline on many small executions.
    """
    db.execute('CREATE TABLE IF NOT EXISTS copy_fill_batches '
               '(first_row INTEGER PRIMARY KEY,last_row INTEGER,payload TEXT,raw TEXT)')
    saved = {r[0]: r for r in db.execute('SELECT * FROM copy_fill_batches')}
    plans = {r[0] for r in db.execute('SELECT id FROM copy_plans')}
    result=[]; i=0
    while i<len(rows):
        rowid,payload,raw=rows[i]
        if rowid in saved:
            _,end,body,source=saved[rowid]
            result.append((end,body,source))
            while i<len(rows) and rows[i][0]<=end:i+=1
            continue
        event=json.loads(payload); fill=json.loads(raw) if raw else {}
        group=[rows[i]]; quantity=Decimal(str(event['quantity']))
        value=quantity*Decimal(str(event['price']))
        side=Decimal(1) if event['side']=='BUY' else Decimal(-1)
        before=Decimal(str(fill.get('startPosition','0')))
        if before*side<0:before=Decimal(0)
        expected=before+side*quantity
        if event['action']=='OPEN' and fill.get('oid') is not None and event['orderId'] not in plans:
            j=i+1
            while j<len(rows):
                rid,body,source=rows[j]
                nxt=json.loads(body); nf=json.loads(source) if source else {}
                if (rid in saved or nxt['action']!='OPEN' or nxt['positionId']!=event['positionId']
                        or (same_timestamp and nxt['updatedAt'] != event['updatedAt'])
                        or nxt['side']!=event['side'] or nf.get('oid')!=fill['oid']
                        or nxt['orderId'] in plans
                        or Decimal(str(nf.get('startPosition','NaN')))!=expected):break
                q=Decimal(str(nxt['quantity']));quantity+=q
                value+=q*Decimal(str(nxt['price']));expected+=side*q
                group.append(rows[j]);j+=1
        if len(group)>1:
            ids=[json.loads(r[1])['orderId'] for r in group]
            merged={**event,'orderId':'hl-batch:'+hashlib.sha256(json.dumps(ids).encode()).hexdigest(),
                    'quantity':str(quantity),'price':str(value/quantity),
                    'sourcePositionAfter':str(abs(expected)),'sourceFillIds':ids}
            end=group[-1][0];body=json.dumps(merged,sort_keys=True)
            with db:db.execute('INSERT INTO copy_fill_batches VALUES (?,?,?,?)',(rowid,end,body,raw))
            result.append((end,body,raw));i+=len(group)
        else:
            result.append(rows[i]);i+=1
    return result


def synchronize(source, copier, execution):
    source.poll()
    if hasattr(execution, 'synchronize_exits'):
        execution.open_blocker = None
        try:
            execution.synchronize_exits(copier.db)
        except (CopyBlocked, ReconciliationRequired, BingXAPIError) as error:
            execution.open_blocker = str(error)
            print(json.dumps({'copy_status': 'protection_blocked', 'error': str(error)}), flush=True)


def pending_rows(db):
    return db.execute(
        """SELECT o.rowid, o.payload, f.payload FROM observations o
           LEFT JOIN hl_fills f ON f.id = substr(o.order_id, 4, length(o.order_id) -
               CASE WHEN o.order_id LIKE '%:close' THEN 9 ELSE 8 END)
           WHERE o.category='observed' AND o.rowid > coalesce(
               (SELECT cast(value AS INTEGER) FROM meta WHERE key='bingx_copy_row'), 0)
           ORDER BY o.rowid"""
    ).fetchall()


def cycle(source, copier, execution, *, min_equity=Decimal('100'), allowed_symbols=None,
          copy_allocation=None, prepared_rows=None):
    db = copier.db
    if prepared_rows is None:
        synchronize(source, copier, execution)
        rows = pending_rows(db)
    else:
        rows = prepared_rows
    if prepared_rows is None and getattr(execution, 'copy_executed_entries_only', False):
        rows = batch_executed_entries(db, rows, same_timestamp=execution.environment == 'live')
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
            skip_reason = None
            if allowed_symbols is not None and symbol not in allowed_symbols and not managed and not planned:
                skip_reason = 'symbol_not_selected'
            if (event['action'] == 'OPEN'
                    and not execution.needs_reconciliation(event['orderId'])):
                equity = execution.equity()
                if not equity.is_finite() or equity < min_equity:
                    skip_reason = 'account_equity_below_entry_threshold'
            if (event['action'] == 'OPEN' and not managed and not planned
                    and getattr(execution, 'peer_journals', ()) and execution.peer_owns(symbol)):
                skip_reason = 'symbol_reserved_by_another_trader'
            if skip_reason:
                with db:
                    db.execute("INSERT OR IGNORE INTO copy_skips VALUES (?,?,?,?,?)",
                               (event['orderId'], event['updatedAt'], time.time_ns() // 1_000_000,
                                skip_reason, json.dumps(event, sort_keys=True)))
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
                        db.execute('INSERT OR IGNORE INTO copy_skips VALUES (?,?,?,?,?)',
                                   (event['orderId'], event['updatedAt'], time.time_ns() // 1_000_000,
                                    'account_equity_below_entry_threshold', json.dumps(event, sort_keys=True)))
                        db.execute("INSERT OR REPLACE INTO meta VALUES ('bingx_copy_row',?)", (str(rowid),))
                    continue
                live_allocation = getattr(execution, 'live_allocation', None)
                if live_allocation is not None:
                    equity = min(equity, live_allocation)
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


def cycle_sources(sources, *, min_equity, allowed_symbols):
    events = []
    unavailable = False
    for idx, (source, copier, execution) in enumerate(sources):
        try:
            synchronize(source, copier, execution)
        except (OSError, ValueError, RuntimeError) as error:
            unavailable = True
            print(json.dumps({'copy_status': 'source_unavailable', 'source': source.address,
                              'error_type': type(error).__name__}), flush=True)
            continue
        rows = pending_rows(copier.db)
        if getattr(execution, 'copy_executed_entries_only', False):
            rows = batch_executed_entries(copier.db, rows, same_timestamp=True)
        for row in rows:
            events.append((json.loads(row[1])['updatedAt'], idx, row))
    unresolved = any(
        copier.db.execute('SELECT 1 FROM copy_entry_children WHERE adopted=0 LIMIT 1').fetchone()
        or copier.db.execute('SELECT 1 FROM copy_exit_dispatch WHERE result IS NULL LIMIT 1').fetchone()
        for _, copier, _ in sources
    )
    if unavailable or unresolved:
        for _, _, execution in sources:
            execution.open_blocker = ('another_copy_operation_requires_reconciliation' if unresolved
                                      else 'another_trader_feed_unavailable')
    # Preserve each source journal's ordering for equal timestamps.
    failed_sources = set()
    for _, idx, row in sorted(events, key=lambda item: (item[0], item[1], item[2][0])):
        if idx in failed_sources:
            continue  # Never advance a source cursor past its failed operation.
        source, copier, execution = sources[idx]
        try:
            cycle(source, copier, execution, min_equity=min_equity, allowed_symbols=allowed_symbols,
                  prepared_rows=[row])
        except (OSError, ValueError, RuntimeError) as error:
            failed_sources.add(idx)
            # One source's uncertain operation must fence new exposure across
            # the account, but must not starve another source's reductions.
            for _, _, runner in sources:
                runner.open_blocker = 'another_copy_operation_requires_reconciliation'
            print(json.dumps({'copy_status': 'source_event_blocked', 'source': source.address,
                              'error_type': type(error).__name__}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--address", required=True)
    parser.add_argument("--additional-address", action="append", default=[],
                        help="Add a public trader to the same live allocation")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--enable-orders", action="store_true")
    parser.add_argument("--copy-add-once", action="store_true")
    parser.add_argument("--min-equity", type=Decimal, default=Decimal('100'),
                        help="Minimum actual account equity for a new entry; default 100 USDT")
    allocation = parser.add_mutually_exclusive_group()
    allocation.add_argument("--allocation", type=Decimal,
                        help="Maximum capital used to size live copies and estimate initial margin (USDT)")
    allocation.add_argument("--use-account-equity", action="store_true",
                            help="Size new copies and cap combined initial margin using current live account equity")
    parser.add_argument("--symbols", nargs='+', help="Optional BingX symbols allowed for new positions")
    args = parser.parse_args()
    addresses = [a.lower() for a in [args.address, *args.additional_address]]
    if any(not ADDRESS.fullmatch(a) for a in addresses) or len(set(addresses)) != len(addresses):
        parser.error('Trader addresses must be distinct valid public addresses')
    if args.additional_address and ((args.allocation is None and not args.use_account_equity)
                                    or args.copy_add_once):
        parser.error('Multiple traders require a shared allocation and fresh entries only')
    if not args.min_equity.is_finite() or args.min_equity < Decimal('10'):
        parser.error('--min-equity must be finite and at least 10 USDT')
    if args.allocation is not None and (not args.allocation.is_finite() or args.allocation < Decimal('10')):
        parser.error('--allocation must be finite and at least 10 USDT')
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
            expected_uid=os.environ['BINGX_ACCOUNT_UID'], live_allocation=args.allocation,
            use_account_equity=args.use_account_equity)
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
        paths = [args.db, *(args.db.with_name(f'{args.db.stem}-{a[2:]}{args.db.suffix}')
                            for a in addresses[1:])]
        workers = []
        for index, (address, path) in enumerate(zip(addresses, paths)):
            runner = execution if index == 0 else StrictBingXExecution(
                os.environ['BINGX_API_KEY'], os.environ['BINGX_API_SECRET'], path, trading_enabled=True,
                source_client=PublicInfo(), source_address=address,
                expected_uid=execution.expected_uid, live_allocation=args.allocation,
                use_account_equity=args.use_account_equity)
            observer = Observer(path, address, "Hyperliquid BingX copy")
            source = FillObserver(observer.db, address)
            copier = HyperliquidBingXCopy(observer.db, runner, time.time_ns() // 1_000_000)
            runner.peer_journals = tuple(other for other in paths if other != path)
            workers.append((source, copier, runner))
        if args.copy_add_once:
            workers[0][1].arm_add_once(time.time_ns() // 1_000_000)
        from .late_entry_probe import start_probe
        start_probe(args.db)
        from .copy_health import start_health
        start_health(execution, paths)
        while True:
            try:
                allowed = frozenset(args.symbols) if args.symbols else None
                if len(workers) == 1:
                    cycle(*workers[0], min_equity=args.min_equity, allowed_symbols=allowed)
                else:
                    cycle_sources(workers, min_equity=args.min_equity, allowed_symbols=allowed)
            except (OSError, ValueError, RuntimeError) as error:
                # Keep polling, but do not advance the failed signal, alter the
                # position journal, or send another POST for its client ID.
                print(json.dumps({"copy_status": "blocked", "error_type": type(error).__name__}), flush=True)
                if args.once:
                    raise
            if args.once:
                return
            time.sleep(5)


if __name__ == "__main__":
    main()

