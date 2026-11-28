"""BingX USDT-M execution for a separately configured one-way account."""

import hashlib
import hmac
import json
import re
import sqlite3
import time
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError


class BingXAPIError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(f"BingX API code {code}: {message}")


class ReconciliationRequired(RuntimeError):
    """An existing submission must be resolved without a second POST."""


MAX_ENTRY_AGE_MS = 30_000
MAX_SOURCE_CLOCK_SKEW_MS = 5_000


class EntryExpired(RuntimeError):
    """The entry has not been sent and must never be opened later."""


class EntryUnfilled(RuntimeError):
    """The exchange confirmed an immediate entry ended with zero execution."""

    def __init__(self, message='', *, reason='entry_price_limit_not_filled'):
        super().__init__(message)
        self.reason = reason


def require_entry_deadline(deadline_ms):
    if deadline_ms is not None and time.time_ns() // 1_000_000 >= deadline_ms:
        raise EntryExpired('source entry exceeded 30 second deadline')


class BingXExecution:
    def __init__(self, api_key, secret, journal_path, *, trading_enabled=False, environment='live'):
        if environment not in ('live', 'demo'):
            raise ValueError('Unknown BingX environment')
        self._environment = environment
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

    @property
    def environment(self):
        return self._environment

    @property
    def api_origin(self):
        return {'live':'https://open-api.bingx.com', 'demo':'https://open-api-vst.bingx.com'}[self.environment]

    def _request(self, method, path, *, version=2, api_family='swap', _deadline_ms=None, **parameters):
        if api_family not in ('swap', 'account'):
            raise ValueError('Unsupported API family')
        require_entry_deadline(_deadline_ms)
        parameters["timestamp"] = time.time_ns() // 1_000_000
        if _deadline_ms is not None:
            remaining = _deadline_ms - parameters['timestamp']
            require_entry_deadline(_deadline_ms)
            # Bound acceptance at the exchange as well as local dispatch.
            parameters['recvWindow'] = min(5000, remaining)
        normalized = {}
        for key, value in parameters.items():
            if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', key):
                raise ValueError('Invalid API parameter name')
            if isinstance(value, (dict, list)):
                value = json.dumps(value, separators=(',', ':'), allow_nan=False)
            elif isinstance(value, bool):
                value = 'true' if value else 'false'
            else:
                value = str(value)
            if any(c in value for c in '&=?#\r\n'):
                raise ValueError('Invalid API parameter value')
            normalized[key] = value
        # BingX signs RAW values, then URL-encodes the transmitted query.
        # Signing percent-encoded JSON works for ordinary scalar calls but
        # fails as soon as attached TP/SL objects are present.
        canonical = '&'.join(f'{key}={value}' for key, value in sorted(normalized.items()))
        query = urlencode(sorted(normalized.items()), quote_via=quote)
        signature = hmac.digest(self.secret, canonical.encode(), "sha256").hex()
        request = Request(
            f"{self.api_origin}/openApi/{api_family}/v{version}/{path}?{query}&signature={signature}",
            headers={"X-BX-APIKEY": self.api_key, "X-SOURCE-KEY": "BX-AI-SKILL"},
            method=method,
        )
        try:
            require_entry_deadline(_deadline_ms)
            with urlopen(request, timeout=10) as response:
                result = json.load(response)
        except HTTPError as error:
            # BingX may return its structured rejection with HTTP 400.
            try:
                result = json.load(error)
            except (ValueError, TypeError):
                raise RuntimeError(f"BingX HTTP {error.code}; response unavailable") from None
        if (not isinstance(result, dict) or type(result.get('code')) is not int
                or (result['code'] == 0 and 'data' not in result)):
            # A malformed response to a write is an UNKNOWN outcome. Keep
            # its durable claim and let the worker reconcile, never repost.
            raise ValueError('Malformed BingX API response')
        if result["code"] != 0:
            message = str(result.get("msg", "No API error description"))
            # The exchange message is untrusted; never log request credentials.
            for private in (self.api_key, self.secret.decode(), signature, canonical, query):
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

    def hedge_mode(self):
        value = self._request("GET", "positionSide/dual", version=1)["dualSidePosition"]
        if value not in (True, False, 'true', 'false'):
            raise ValueError("Unknown BingX position mode")
        return value is True or value == 'true'

    def has_submission(self, signal_id):
        client_id = hashlib.sha256(signal_id.encode()).hexdigest()[:40]
        with sqlite3.connect(self.journal_path) as journal:
            return bool(journal.execute(
                'SELECT 1 FROM bingx_submissions WHERE client_id=?', (client_id,)
            ).fetchone())

    def needs_reconciliation(self, signal_id):
        client_id = hashlib.sha256(signal_id.encode()).hexdigest()[:40]
        with sqlite3.connect(self.journal_path) as journal:
            row = journal.execute('SELECT e.code FROM bingx_submissions s LEFT JOIN '
                                  'bingx_submission_errors e ON e.client_id=s.client_id '
                                  'WHERE s.client_id=?', (client_id,)).fetchone()
        # Only explicit, definitive rejections can expire. Timeouts, system
        # errors and accepted orders remain fenced until reconciled.
        # 101400 can also mean duplicate clientOrderID: an earlier order may
        # exist, so it must remain recoverable by its immutable client ID.
        return bool(row and row[0] not in {109400, 109425, 101204, 101206, 100421})

    def submit(self, signal_id, symbol, side, quantity, *, reduce_only=False, deadline_ms=None):
        if not self.trading_enabled:
            raise RuntimeError("BingX order submission is disabled")
        if not signal_id or side not in {"BUY", "SELL"} or not quantity.is_finite() or quantity <= 0:
            raise ValueError("Order requires a signal ID, valid side and positive quantity")

        client_id = hashlib.sha256(signal_id.encode()).hexdigest()[:40]
        if not self.has_submission(signal_id):
            require_entry_deadline(deadline_ms)
        parameters = {
            "symbol": symbol,
            "side": side,
            "positionSide": "BOTH",
            "type": "MARKET",
            "quantity": format(quantity, "f"),
            "clientOrderId": client_id,
            "reduceOnly": "true" if reduce_only else "false",
        }
        if not reduce_only:
            parameters.update(self.opening_parameters(signal_id, symbol, side, quantity, deadline_ms))
        if self.hedge_mode():
            # In hedge mode LONG/SHORT selects the position being changed;
            # BUY/SELL selects increase vs reduction. reduceOnly is forbidden.
            long_position = (side == 'BUY') != reduce_only
            parameters['positionSide'] = 'LONG' if long_position else 'SHORT'
            del parameters['reduceOnly']
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
        if rejection and not self.needs_reconciliation(signal_id):
            raise ReconciliationRequired(
                f"Submission {client_id} rejected: BingX API code {rejection[0]}: {rejection[1]}; "
                "no automatic resubmission"
            )
        if inserted:
            try:
                require_entry_deadline(deadline_ms)
                if deadline_ms is None:
                    self._request("POST", "trade/order", **parameters)
                else:
                    self._request("POST", "trade/order", _deadline_ms=deadline_ms, **parameters)
            except EntryExpired:
                # No network request was made. Release only this unsent claim;
                # ambiguous network failures keep their claim for reconciliation.
                with sqlite3.connect(self.journal_path) as journal:
                    journal.execute('DELETE FROM bingx_submissions WHERE client_id=? AND parameters=?',
                                    (client_id, payload))
                raise
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

    def opening_parameters(self, signal_id, symbol, side, quantity, deadline_ms):
        """Legacy adapter hook; real workers use StrictBingXExecution."""
        return {}


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
            db.execute(
                'CREATE TABLE IF NOT EXISTS copy_skips '
                '(id TEXT PRIMARY KEY, source_ms INTEGER NOT NULL, decided_ms INTEGER NOT NULL, '
                'reason TEXT NOT NULL, observation TEXT NOT NULL)'
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

    def record_expired_entry(self, observation, reason):
        signal_id = observation['orderId']
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO copy_skips VALUES (?,?,?,?,?)',
                            (signal_id, observation['updatedAt'], time.time_ns() // 1_000_000,
                             reason, json.dumps(observation, sort_keys=True)))
            # An unsent, expired seed must not consume the one-time permission.
            self.db.execute("DELETE FROM meta WHERE key='bingx_add_once_signal' AND value=? "
                            'AND NOT EXISTS (SELECT 1 FROM copied_positions)', (signal_id,))
        print(json.dumps({'copy_status': 'skipped', 'signal_id': signal_id, 'reason': reason}), flush=True)

    def skip_stale_entry(self, observation):
        if observation['action'] != 'OPEN' or observation['updatedAt'] <= self.cutover_ms:
            return False
        signal_id = observation['orderId']
        if self.db.execute('SELECT 1 FROM copy_skips WHERE id=?', (signal_id,)).fetchone():
            return True
        # Already-dispatched orders must be reconciled even after the deadline.
        if self.execution.needs_reconciliation(signal_id):
            return False
        age = time.time_ns() // 1_000_000 - int(observation['updatedAt'])
        if age >= MAX_ENTRY_AGE_MS or age < -MAX_SOURCE_CLOCK_SKEW_MS:
            self.record_expired_entry(observation, 'stale_entry' if age >= 0 else 'future_source_timestamp')
            return True
        return False

    def apply(self, observation, source_before, copy_ratio, market):
        """Return the resulting quantity once a fill is confirmed, or None for a skipped signal."""
        signal_id = observation["orderId"]
        epoch = observation["positionId"]
        if observation["updatedAt"] <= self.cutover_ms:
            return None
        if self.db.execute("SELECT 1 FROM copied_signals WHERE id=?", (signal_id,)).fetchone():
            return None
        if self.skip_stale_entry(observation):
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
                if getattr(self.execution, 'strict_sizing', False):
                    source_after = max(Decimal(0), source_before - source_quantity)
                    desired = increase_quantity(source_after, ratio, step) if source_after else Decimal(0)
                    quantity = max(Decimal(0), actual - min(actual, desired))
                    if not quantity:
                        # A confirmed local protective fill may already have
                        # reduced this amount. Do not close the same risk twice.
                        with self.db:
                            self.db.execute('INSERT INTO copied_signals VALUES (?)', (signal_id,))
                        return actual
                else:
                    quantity = reduction_quantity(min(source_quantity, source_before), source_before, actual, step)
            elif action == "OPEN":
                if row:
                    ratio, actual = map(Decimal, row)
                    if getattr(self.execution, 'strict_sizing', False):
                        from .full_copy_contract import scaled_quantity
                        quantity = scaled_quantity(source_quantity, ratio, step, min_qty, min_notional, price)
                    else:
                        quantity = increase_quantity(source_quantity, ratio, step)
                else:
                    if source_before and not seed:
                        return None
                    ratio, actual = copy_ratio, Decimal(0)
                    if getattr(self.execution, 'strict_sizing', False):
                        from .full_copy_contract import scaled_quantity
                        quantity = scaled_quantity(source_quantity, ratio, step, min_qty, min_notional, price)
                    else:
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
        options = {'reduce_only': action == 'CLOSE'}
        if action == 'OPEN':
            options['deadline_ms'] = int(observation['updatedAt']) + MAX_ENTRY_AGE_MS
        try:
            result = self.execution.submit(signal_id, symbol, observation['side'], quantity, **options)
        except EntryExpired:
            self.record_expired_entry(observation, 'expired_before_dispatch')
            return None
        except EntryUnfilled as error:
            self.record_expired_entry(observation, error.reason)
            return None
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

