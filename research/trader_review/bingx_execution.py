"""BingX USDT-M execution for a separately configured one-way account."""

import hashlib
import hmac
import json
import sqlite3
import time
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError


class BingXAPIError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(f"BingX API code {code}: {message}")


class ReconciliationRequired(RuntimeError):
    """An existing submission must be resolved without a second POST."""


class BingXExecution:
    def __init__(self, api_key, secret, journal_path, *, trading_enabled=False):
        self.api_key = api_key
        self.secret = secret.encode()
        self.journal_path = journal_path
        self.trading_enabled = trading_enabled
        with sqlite3.connect(journal_path) as journal:
            journal.execute(
                "CREATE TABLE IF NOT EXISTS bingx_submissions "
                "(client_id TEXT PRIMARY KEY, parameters TEXT NOT NULL)"
            )
            journal.execute(
                "CREATE TABLE IF NOT EXISTS bingx_submission_errors "
                "(client_id TEXT PRIMARY KEY, code INTEGER, message TEXT NOT NULL)"
            )

    def _request(self, method, path, *, version=2, **parameters):
        parameters["timestamp"] = time.time_ns() // 1_000_000
        query = urlencode(sorted(parameters.items()))
        signature = hmac.digest(self.secret, query.encode(), "sha256").hex()
        request = Request(
            f"https://open-api.bingx.com/openApi/swap/v{version}/{path}?{query}&signature={signature}",
            headers={"X-BX-APIKEY": self.api_key},
            method=method,
        )
        try:
            with urlopen(request, timeout=10) as response:
                result = json.load(response)
        except HTTPError as error:
            # BingX may return its structured rejection with HTTP 400.
            try:
                result = json.load(error)
            except (ValueError, TypeError):
                raise RuntimeError(f"BingX HTTP {error.code}; response unavailable") from None
        if result["code"] != 0:
            message = str(result.get("msg", "No API error description"))
            # The exchange message is untrusted; never log request credentials.
            for private in (self.api_key, self.secret.decode(), signature, query):
                if private:
                    message = message.replace(private, "[REDACTED]")
            raise BingXAPIError(result["code"], message[:1000])
        return result["data"]

    def equity(self):
        balances = self._request("GET", "user/balance", version=3)
        return next(Decimal(row["equity"]) for row in balances if row["asset"] == "USDT")

    def verify_position(self, symbol, direction, expected, closing_quantity):
        positions = self._request("GET", "user/positions", symbol=symbol)
        positions = [row for row in positions if Decimal(row["positionAmt"]) != 0]
        if not positions and expected == 0:
            return
        if len(positions) != 1:
            raise RuntimeError("Unexpected BingX position count")
        position = positions[0]
        if (position["positionSide"] != direction
                or abs(Decimal(position["positionAmt"])) != expected
                or abs(Decimal(position["availableAmt"])) < closing_quantity):
            raise RuntimeError("BingX position differs from the copy journal")

    def order(self, symbol, client_id):
        return self._request("GET", "trade/order", symbol=symbol, clientOrderId=client_id)

    def submit(self, signal_id, symbol, side, quantity, *, reduce_only=False):
        if not self.trading_enabled:
            raise RuntimeError("BingX order submission is disabled")
        if not signal_id or side not in {"BUY", "SELL"} or not quantity.is_finite() or quantity <= 0:
            raise ValueError("Order requires a signal ID, valid side and positive quantity")

        client_id = hashlib.sha256(signal_id.encode()).hexdigest()[:40]
        parameters = {
            "symbol": symbol,
            "side": side,
            "positionSide": "BOTH",
            "type": "MARKET",
            "quantity": format(quantity, "f"),
            "clientOrderId": client_id,
            "reduceOnly": "true" if reduce_only else "false",
        }
        payload = json.dumps(parameters, sort_keys=True)
        # Commit before submitting. A timeout or restart leaves the order
        # pending reconciliation, with no second POST for the same signal.
        with sqlite3.connect(self.journal_path) as journal:
            inserted = journal.execute(
                "INSERT OR IGNORE INTO bingx_submissions VALUES (?, ?)", (client_id, payload)
            ).rowcount
            saved_payload = journal.execute(
                "SELECT parameters FROM bingx_submissions WHERE client_id = ?", (client_id,)
            ).fetchone()[0]
            rejection = journal.execute(
                "SELECT code,message FROM bingx_submission_errors WHERE client_id=?", (client_id,)
            ).fetchone()
        if saved_payload != payload:
            raise ValueError("Order parameters changed for an existing signal")
        if rejection:
            raise ReconciliationRequired(
                f"Submission {client_id} rejected: BingX API code {rejection[0]}: {rejection[1]}; "
                "no automatic resubmission"
            )
        if inserted:
            try:
                self._request("POST", "trade/order", **parameters)
            except BingXAPIError as error:
                with sqlite3.connect(self.journal_path) as journal:
                    journal.execute("INSERT OR REPLACE INTO bingx_submission_errors VALUES (?,?,?)",
                                    (client_id, error.code, error.message))
                raise
        try:
            return self.order(symbol, client_id)
        except BingXAPIError as error:
            if error.code == 109421:
                raise ReconciliationRequired(
                    f"Submission {client_id} not found on BingX; "
                    "check exchange history and the saved request before any resubmission"
                ) from None
            raise


def opening_quantity(source_quantity, copy_ratio, price, quantity_step, minimum_quantity,
                     minimum_notional):
    required = (source_quantity, copy_ratio, price, quantity_step)
    minimums = (minimum_quantity, minimum_notional)
    if any(not value.is_finite() or value <= 0 for value in required):
        raise ValueError("Source quantity, ratio, price and quantity step must be positive")
    if any(not value.is_finite() or value < 0 for value in minimums):
        raise ValueError("Exchange minimums must be non-negative")
    floor = max(Decimal("10"), minimum_notional)
    proportional_steps = (source_quantity * copy_ratio / quantity_step).to_integral_value(
        rounding=ROUND_DOWN
    )
    minimum_steps = (max(minimum_quantity, floor / price) / quantity_step).to_integral_value(
        rounding=ROUND_CEILING
    )
    return max(proportional_steps, minimum_steps) * quantity_step


def increase_quantity(source_quantity, position_ratio, quantity_step):
    """Round a proportional increase down without applying the opening floor."""
    values = (source_quantity, position_ratio, quantity_step)
    if any(not value.is_finite() or value <= 0 for value in values):
        raise ValueError("Increase inputs must be positive and finite")
    steps = (source_quantity * position_ratio / quantity_step).to_integral_value(
        rounding=ROUND_DOWN
    )
    return steps * quantity_step


def reduction_quantity(source_reduced, source_before, actual_quantity, quantity_step):
    """Apply the source reduction fraction to the current exchange position."""
    values = (source_reduced, source_before, actual_quantity, quantity_step)
    if any(not value.is_finite() or value <= 0 for value in values):
        raise ValueError("Reduction inputs must be positive and finite")
    if source_reduced > source_before:
        raise ValueError("Split a reversal into a full close and a new opening")
    if source_reduced == source_before:
        return actual_quantity
    return increase_quantity(actual_quantity, source_reduced / source_before, quantity_step)


class HyperliquidBingXCopy:
    """Consume ordered normalized observations using a dedicated SQLite journal."""

    def __init__(self, db, execution, cutover_ms):
        self.db = db
        self.execution = execution
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS copy_cutover (id INTEGER PRIMARY KEY, ms INTEGER)")
            db.execute("INSERT OR IGNORE INTO copy_cutover VALUES (1, ?)", (cutover_ms,))
            db.execute(
                "CREATE TABLE IF NOT EXISTS copied_positions "
                "(epoch TEXT PRIMARY KEY, ratio TEXT NOT NULL, quantity TEXT NOT NULL)"
            )
            db.execute("CREATE TABLE IF NOT EXISTS copied_signals (id TEXT PRIMARY KEY)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS copy_plans "
                "(id TEXT PRIMARY KEY, ratio TEXT NOT NULL, before_qty TEXT NOT NULL, qty TEXT NOT NULL)"
            )
        self.cutover_ms = db.execute("SELECT ms FROM copy_cutover WHERE id=1").fetchone()[0]

    def arm_add_once(self, now_ms):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO meta VALUES ('bingx_add_once_after', ?)",
                            (str(now_ms),))

    def can_seed_from_add(self, observation, source_before):
        if observation["action"] != "OPEN" or not source_before:
            return False
        state = dict(self.db.execute(
            "SELECT key,value FROM meta WHERE key IN ('bingx_add_once_after','bingx_add_once_signal')"
        ))
        return (
            "bingx_add_once_after" in state
            and "bingx_add_once_signal" not in state
            and observation["updatedAt"] > int(state["bingx_add_once_after"])
            and not self.db.execute("SELECT 1 FROM copied_positions LIMIT 1").fetchone()
        )

    def apply(self, observation, source_before, copy_ratio, market):
        """Return the resulting quantity once a fill is confirmed, or None for a skipped signal."""
        signal_id = observation["orderId"]
        epoch = observation["positionId"]
        if observation["updatedAt"] <= self.cutover_ms:
            return None
        if self.db.execute("SELECT 1 FROM copied_signals WHERE id=?", (signal_id,)).fetchone():
            return None
        row = self.db.execute(
            "SELECT ratio, quantity FROM copied_positions WHERE epoch=?", (epoch,)
        ).fetchone()
        action = observation["action"]
        source_quantity = Decimal(str(observation["quantity"]))
        price, step, min_qty, min_notional = market
        symbol = observation["symbol"][:-4] + "-USDT"
        plan = self.db.execute(
            "SELECT ratio,before_qty,qty FROM copy_plans WHERE id=?", (signal_id,)
        ).fetchone()
        seed = not row and not plan and self.can_seed_from_add(observation, source_before)
        if ":prebaseline:" in epoch and not (row or plan or seed):
            return None
        if plan:
            ratio, actual, quantity = map(Decimal, plan)
        else:
            if action == "CLOSE":
                if not row:
                    return None
                ratio, actual = map(Decimal, row)
                quantity = reduction_quantity(min(source_quantity, source_before), source_before, actual, step)
            elif action == "OPEN":
                if row:
                    ratio, actual = map(Decimal, row)
                    quantity = increase_quantity(source_quantity, ratio, step)
                else:
                    if source_before and not seed:
                        return None
                    ratio, actual = copy_ratio, Decimal(0)
                    quantity = opening_quantity(source_quantity, ratio, price, step, min_qty, min_notional)
            else:
                raise ValueError("Unknown Hyperliquid observation action")

            if quantity <= 0:
                raise RuntimeError("Copied order is below the quantity step")
            long_position = (observation["side"] == "BUY") != (action == "CLOSE")
            self.execution.verify_position(
                symbol, "LONG" if long_position else "SHORT", actual,
                quantity if action == "CLOSE" else Decimal(0),
            )
            with self.db:
                if seed:
                    self.db.execute("INSERT INTO meta VALUES ('bingx_add_once_signal', ?)",
                                    (signal_id,))
                self.db.execute(
                    "INSERT INTO copy_plans VALUES (?,?,?,?)",
                    (signal_id, str(ratio), str(actual), str(quantity)),
                )
        result = self.execution.submit(
            signal_id, symbol, observation["side"], quantity, reduce_only=action == "CLOSE"
        )
        order = result.get("order", result)
        if order["status"] != "FILLED" or Decimal(str(order["executedQty"])) != quantity:
            raise RuntimeError("BingX order requires reconciliation before processing more signals")
        remaining = actual - quantity if action == "CLOSE" else actual + quantity
        with self.db:
            if remaining:
                self.db.execute(
                    "INSERT OR REPLACE INTO copied_positions VALUES (?,?,?)",
                    (epoch, str(ratio), str(remaining)),
                )
            else:
                self.db.execute("DELETE FROM copied_positions WHERE epoch=?", (epoch,))
            self.db.execute("INSERT INTO copied_signals VALUES (?)", (signal_id,))
        return remaining

