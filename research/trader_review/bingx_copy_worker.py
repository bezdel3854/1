"""Run an isolated Hyperliquid-to-BingX copy worker."""
import argparse
import fcntl
import json
import os
import time
from decimal import Decimal
from pathlib import Path

from .bingx_execution import BingXExecution, HyperliquidBingXCopy
from .finmonster_feed import Observer
from .hyperliquid_feed import FillObserver


def cycle(source, copier, execution):
    source.poll()
    db = copier.db
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
        fill = json.loads(raw)
        before = Decimal(fill["startPosition"])
        if event["action"] == "OPEN" and ((before < 0 and event["side"] == "BUY")
                                         or (before > 0 and event["side"] == "SELL")):
            before = Decimal(0)
        if event["updatedAt"] > copier.cutover_ms and ":prebaseline:" not in event["positionId"]:
            symbol = event["symbol"][:-4] + "-USDT"
            contracts = execution._request("GET", "quote/contracts", symbol=symbol)
            contract = next(item for item in contracts if item["symbol"] == symbol)
            ticker = execution._request("GET", "quote/price", symbol=symbol)
            market = (
                Decimal(str(ticker["price"])),
                Decimal(1).scaleb(-int(contract["quantityPrecision"])),
                Decimal(str(contract["tradeMinQuantity"])),
                Decimal(str(contract["tradeMinUSDT"])),
            )
            ratio = Decimal(0)
            opening = event["action"] == "OPEN" and not before
            if opening and not db.execute(
                "SELECT 1 FROM copy_plans WHERE id=?", (event["orderId"],)
            ).fetchone():
                account = source.client.get("clearinghouseState", user=source.address)
                source_equity = Decimal(account["marginSummary"]["accountValue"])
                if not source_equity.is_finite() or source_equity <= 0:
                    raise ValueError("Invalid Hyperliquid equity")
                equity = execution.equity()
                if not equity.is_finite() or equity < Decimal("100"):
                    # A signal seen before funding must never be replayed after funding.
                    with db:
                        db.execute("INSERT OR REPLACE INTO meta VALUES ('bingx_copy_row',?)", (str(rowid),))
                    continue
                ratio = equity / source_equity
            copier.apply(event, abs(before), ratio, market)
        with db:
            db.execute("INSERT OR REPLACE INTO meta VALUES ('bingx_copy_row',?)", (str(rowid),))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--address", required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--enable-orders", action="store_true")
    args = parser.parse_args()
    if args.db.name != "hyperliquid-bingx-live.sqlite":
        parser.error("Use a dedicated hyperliquid-bingx-live.sqlite database")
    execution = BingXExecution(
        os.environ["BINGX_API_KEY"], os.environ["BINGX_API_SECRET"],
        args.db if args.enable_orders else ":memory:",
        trading_enabled=args.enable_orders,
    )
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
        while True:
            cycle(source, copier, execution)
            if args.once:
                return
            time.sleep(15)


if __name__ == "__main__":
    main()
