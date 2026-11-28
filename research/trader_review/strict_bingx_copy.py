"""Fail-closed source settings and protection adapter.

Never run this module to enable trading automatically. The worker's explicit
order flag is still required. Unsupported source semantics block new exposure.
"""
from dataclasses import asdict
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import hashlib
import json
import sqlite3
import time

from .bingx_execution import BingXAPIError, BingXExecution, ReconciliationRequired, require_entry_deadline
from .full_copy_contract import (CopyBlocked, compile_contract, number,
                                 settings_plan, verify_target_settings, exit_sync_plan)


def source_signature(account):
    return sorted((p['position']['coin'], p['position']['szi'],
                   json.dumps(p['position'].get('leverage'), sort_keys=True))
                  for p in account.get('assetPositions', []))


def source_snapshot(client, address):
    started = time.time_ns() // 1_000_000
    def pair(pool):
        account = pool.submit(client.get, 'clearinghouseState', user=address)
        orders = pool.submit(client.get, 'frontendOpenOrders', user=address)
        return account.result(), orders.result()
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, orders = pair(pool)
        verified_started = time.time_ns() // 1_000_000
        b, second_orders = pair(pool)
    ended = time.time_ns() // 1_000_000
    if ended - verified_started > 5000 or ended - started > 15000:
        raise CopyBlocked('source_snapshot_too_slow')
    if source_signature(a) != source_signature(b) or orders != second_orders:
        raise CopyBlocked('source_changed_during_snapshot')
    return b, second_orders, verified_started


class StrictBingXExecution(BingXExecution):
    strict_sizing = True
    required_source_protection = False
    copy_executed_entries_only = True
    peer_journals = ()
    def __init__(self, *args, source_client, source_address, expected_uid=None,
                 live_allocation=None, use_account_equity=False, **kwargs):
        self.live_allocation = None if live_allocation is None else number(live_allocation, positive=True)
        self.use_account_equity = use_account_equity
        if (self.live_allocation is not None or use_account_equity) and kwargs.get('environment', 'live') != 'live':
            raise ValueError('Live allocation requires the live exchange')
        super().__init__(*args, **kwargs)
        self.source_client, self.source_address = source_client, source_address
        self.expected_uid = str(expected_uid) if expected_uid is not None else None
        self.context = None
        self.open_blocker = None
        self.account_stream = None
        self.event_journal_path = self.journal_path
        with sqlite3.connect(self.journal_path) as db:
            from .bingx_account_stream import initialize
            initialize(db)
            db.execute('CREATE TABLE IF NOT EXISTS copy_contracts '
                       '(epoch TEXT PRIMARY KEY, symbol TEXT, ratio TEXT, payload TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS copy_exit_orders '
                       '(epoch TEXT, source_id TEXT, order_id TEXT, parameters TEXT, '
                       'PRIMARY KEY(epoch,source_id))')
            db.execute('CREATE TABLE IF NOT EXISTS copy_exit_dispatch '
                       '(id INTEGER PRIMARY KEY, epoch TEXT, source_id TEXT, action TEXT, '
                       'payload TEXT, result TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS copy_exit_receipts '
                       '(dispatch_id INTEGER PRIMARY KEY, order_id TEXT NOT NULL, response TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS copy_entry_children '
                       '(signal_id TEXT PRIMARY KEY, epoch TEXT, symbol TEXT, quantity TEXT, '
                       'before_ids TEXT, contract TEXT, adopted INTEGER DEFAULT 0)')
            db.execute('CREATE TABLE IF NOT EXISTS copy_exit_fills '
                       '(order_id TEXT PRIMARY KEY, executed_qty TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS copy_exit_history '
                       '(epoch TEXT,order_id TEXT,parameters TEXT NOT NULL,PRIMARY KEY(epoch,order_id))')
            db.execute('CREATE TABLE IF NOT EXISTS locally_exited_epochs '
                       '(epoch TEXT PRIMARY KEY, reason TEXT)')

    def set_context(self, event, source_before, ratio):
        self.context = (event, source_before, ratio)

    def verify_account_identity(self):
        if not self.expected_uid or not self.expected_uid.isdigit():
            raise CopyBlocked('account_uid_not_pinned')
        try:
            actual = self._request('GET', 'uid', api_family='account', version=1).get('uid')
        except BingXAPIError as error:
            if self.environment != 'live' or str(error.code) != '100400':
                raise
            # The live account endpoint may be unavailable. The signed USDT
            # balance also carries the account identity when BingX sends it in full.
            balances = self._request('GET', 'user/balance', version=3)
            ids = [row.get('shortUid', row.get('userId')) for row in balances
                   if row.get('asset') == 'USDT']
            actual = ids[0] if len(ids) == 1 else None
        if str(actual) != self.expected_uid:
            raise CopyBlocked('account_uid_mismatch_no_mutations_allowed')

    def _positions(self, symbol):
        return [p for p in self._request('GET', 'user/positions', symbol=symbol)
                if number(p['positionAmt']) != 0]

    def _orders(self, symbol):
        data = self._request('GET', 'trade/openOrders', **({'symbol': symbol} if symbol else {}))
        out = data.get('orders') if isinstance(data, dict) else data
        if not isinstance(out, list):
            raise CopyBlocked('invalid_target_open_orders')
        return out

    def reserved_peer_symbols(self):
        symbols = set()
        for path in self.peer_journals:
            # Each peer journal is initialized before either source is polled.
            # A missing or unreadable peer must stop dispatch, not hide exposure.
            with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
                symbols.update(row[0] for row in db.execute('SELECT symbol FROM copy_contracts'))
        return symbols

    def peer_owns(self, symbol):
        return symbol in self.reserved_peer_symbols()

    def _rules(self, symbol):
        contracts = self._request('GET', 'quote/contracts', symbol=symbol)
        contract = next((c for c in contracts if c.get('symbol') == symbol), None)
        if not contract:
            raise CopyBlocked('unsupported_target_contract')
        ticker = self._request('GET', 'quote/premiumIndex', symbol=symbol)
        if isinstance(ticker, list):
            ticker = next(t for t in ticker if t.get('symbol') == symbol)
        return dict(price=ticker['markPrice'],
                    price_tick=Decimal(1).scaleb(-int(contract['pricePrecision'])),
                    quantity_step=Decimal(1).scaleb(-int(contract['quantityPrecision'])),
                    minimum_quantity=contract['tradeMinQuantity'],
                    minimum_notional=contract['tradeMinUSDT'])

    def _contract(self, symbol, ratio, quantity, *, for_exit_sync=False):
        # Fetch target rules first so the source snapshot is fresh at compile.
        rules = self._rules(symbol)
        equity = self.contract_capital()
        account, orders, stamp = source_snapshot(self.source_client, self.source_address)
        return compile_contract(account, orders, symbol.removesuffix('-USDT'),
                                equity, rules, observed_ms=stamp,
                                ratio=ratio, target_quantity=quantity,
                                require_protection=self.required_source_protection,
                                copy_executed_entries_only=self.copy_executed_entries_only,
                                for_exit_sync=for_exit_sync)

    def contract_capital(self):
        equity = self.equity()
        return min(equity, self.live_allocation) if self.live_allocation is not None else equity

    def verify_live_margin_budget(self, positions, quantity, leverage, price):
        if self.live_allocation is None and not self.use_account_equity:
            return
        committed = sum((number(p['initialMargin'], positive=True) for p in positions), Decimal(0))
        order_margin = quantity * price / leverage
        if committed + order_margin > self.contract_capital():
            raise CopyBlocked('entry_exceeds_live_margin_allocation')

    def entry_brackets(self, contract):
        return contract.entry_brackets(require_pair=False)

    def entry_price_parameters(self, side, source_price, rules):
        deviation = number(rules['price'], positive=True) / source_price - 1
        adverse = deviation if side == 'BUY' else -deviation
        if adverse > Decimal('.001'):
            raise CopyBlocked('cross_exchange_entry_price_deviation_exceeds_0.1_percent')
        return {}

    def opening_parameters(self, signal_id, symbol, side, quantity, deadline_ms):
        with sqlite3.connect(self.journal_path) as db:
            saved = db.execute('SELECT parameters FROM bingx_submissions WHERE client_id=?',
                               (hashlib.sha256(signal_id.encode()).hexdigest()[:40],)).fetchone()
        if saved:
            # An ambiguous/successful old submission is reconciled, never
            # rewritten using newer source stops and never sent twice.
            params = json.loads(saved[0])
            return {key: params[key] for key in ('stopLoss', 'takeProfit', 'type', 'price', 'timeInForce')
                    if key in params}
        if self.account_stream is not None:
            self.account_stream.require_ready()
        if self.open_blocker:
            raise CopyBlocked(self.open_blocker)
        if self.context is None or self.context[0]['orderId'] != signal_id:
            raise CopyBlocked('missing_signal_contract_context')
        event, before, ratio = self.context
        with sqlite3.connect(self.journal_path) as db:
            if db.execute('SELECT 1 FROM copy_contracts WHERE symbol=? AND epoch<>?',
                          (symbol, event['positionId'])).fetchone():
                raise ReconciliationRequired('Previous epoch protection cleanup required before entry')
            if db.execute('SELECT 1 FROM locally_exited_epochs WHERE epoch=?', (event['positionId'],)).fetchone():
                raise CopyBlocked('local_protection_filled_do_not_reopen_epoch')
        if not ratio or not number(ratio, positive=True):
            raise CopyBlocked('invalid_copy_ratio')
        expected = number(event['quantity'], positive=True) * number(ratio, positive=True)
        if quantity > expected:
            raise CopyBlocked('minimum_order_would_increase_source_risk')
        require_entry_deadline(deadline_ms)
        all_positions = [p for p in self._request('GET', 'user/positions')
                         if number(p['positionAmt']) != 0]
        with sqlite3.connect(self.journal_path) as db:
            has_positions_table = db.execute("SELECT 1 FROM sqlite_master WHERE name='copied_positions'").fetchone()
            managed_symbols = ({r[0] for r in db.execute(
                'SELECT c.symbol FROM copy_contracts c JOIN copied_positions p ON p.epoch=c.epoch')}
                if has_positions_table else set())
        peer_symbols = self.reserved_peer_symbols()
        if symbol in peer_symbols:
            raise CopyBlocked('symbol_reserved_by_another_trader')
        if any(p.get('symbol') not in managed_symbols | peer_symbols for p in all_positions):
            raise CopyBlocked('manual_or_unowned_position_requires_separate_copy_account')
        positions, orders = self._positions(symbol), self._orders(symbol)
        with sqlite3.connect(self.journal_path) as db:
            owned_ids = {r[0] for r in db.execute('SELECT order_id FROM copy_exit_orders')}
        if any(str(o.get('orderID', o.get('orderId'))) not in owned_ids for o in orders):
            raise CopyBlocked('unowned_target_orders_prevent_exclusive_mirroring')
        actual = sum(abs(number(p['positionAmt'])) for p in positions)
        contract = self._contract(symbol, ratio, actual + quantity)
        rules = self._rules(symbol)
        source_price = number(event.get('price'), positive=True)
        price_params = self.entry_price_parameters(side, source_price, rules)
        self.verify_live_margin_budget(all_positions, quantity, contract.leverage,
                                       max(source_price, number(rules['price'], positive=True)))
        if (side == 'BUY') != (contract.position_side == 'LONG'):
            raise CopyBlocked('source_reversed_before_dispatch')
        brackets = self.entry_brackets(contract)
        leverage = self._request('GET', 'trade/leverage', symbol=symbol)
        margin = self._request('GET', 'trade/marginType', symbol=symbol)
        hedge = self.hedge_mode()
        changes = settings_plan(contract, leverage, margin, hedge=hedge,
                                target_has_position=bool(positions), target_has_orders=bool(orders))
        for method, path, parameters in changes:
            require_entry_deadline(deadline_ms)
            if not self.trading_enabled:
                raise CopyBlocked('settings_mutation_disabled')
            self._request(method, path, _deadline_ms=deadline_ms, **parameters)
        verify_target_settings(contract,
            self._request('GET', 'trade/leverage', symbol=symbol),
            self._request('GET', 'trade/marginType', symbol=symbol))
        require_entry_deadline(deadline_ms)
        # Recheck source settings/orders after changing exchange configuration.
        latest = self._contract(symbol, ratio, actual + quantity)
        if latest.fingerprint() != contract.fingerprint():
            raise CopyBlocked('source_contract_changed_before_dispatch')
        if self.account_stream is not None:
            self.account_stream.require_ready()
        epoch = event['positionId']
        recorded_contract = {**asdict(contract), 'entry_brackets_attached': bool(brackets)}
        with sqlite3.connect(self.journal_path) as db:
            db.execute('INSERT OR REPLACE INTO copy_contracts VALUES (?,?,?,?)',
                       (epoch, symbol, str(ratio), json.dumps(recorded_contract, sort_keys=True)))
            db.execute('INSERT OR IGNORE INTO copy_entry_children '
                       '(signal_id,epoch,symbol,quantity,before_ids,contract) VALUES (?,?,?,?,?,?)',
                       (signal_id, epoch, symbol, str(quantity),
                        json.dumps([str(o.get('orderID', o.get('orderId'))) for o in orders]),
                        json.dumps(recorded_contract, sort_keys=True)))
        return {**brackets, **price_params}

    def submit(self, signal_id, symbol, side, quantity, *, reduce_only=False, deadline_ms=None):
        self.verify_account_identity()
        result = super().submit(signal_id, symbol, side, quantity,
                                reduce_only=reduce_only, deadline_ms=deadline_ms)
        if not reduce_only:
            self.check_entry_result(signal_id, symbol, result.get('order', result))
            self.adopt_entry_children(signal_id)
        return result

    def check_entry_result(self, signal_id, symbol, order):
        """Check venue-specific immediate order outcomes before adopting protection."""

    def adopt_entry_children(self, signal_id):
        from .full_copy_contract import ExitOrder
        with sqlite3.connect(self.journal_path) as db:
            row = db.execute('SELECT epoch,symbol,quantity,before_ids,contract,adopted '
                             'FROM copy_entry_children WHERE signal_id=?', (signal_id,)).fetchone()
            if not row or row[-1]:
                return
            epoch, symbol, quantity, before_json, contract_json, _ = row
            parent_id = hashlib.sha256(signal_id.encode()).hexdigest()[:40]
            parent = self.order(symbol, parent_id)
            parent = parent.get('order', parent)
            if parent.get('status') != 'FILLED' or number(parent.get('executedQty')) != number(quantity):
                raise ReconciliationRequired('Entry is not fully confirmed; protection adoption blocked')
            before_ids = set(json.loads(before_json))
            new_orders = [o for o in self._orders(symbol)
                          if str(o.get('orderID', o.get('orderId'))) not in before_ids]
            contract = json.loads(contract_json)
            if not contract.get('entry_brackets_attached', True):
                if not contract['exits']:
                    if new_orders:
                        raise ReconciliationRequired('Unprotected source entry left unexpected target orders')
                    db.execute('UPDATE copy_entry_children SET adopted=1 WHERE signal_id=?', (signal_id,))
                    return
                if self.environment != 'demo':
                    raise CopyBlocked('detached_initial_protection_is_demo_only')
                if new_orders:
                    raise ReconciliationRequired('Unexpected demo orders during initial protection setup')
                hedge = self.hedge_mode()
                specs = tuple(ExitOrder(**raw) for raw in contract['exits'])
                from .full_copy_contract import CopyContract
                current = CopyContract(**{**{k:v for k,v in contract.items()
                                            if k != 'entry_brackets_attached'}, 'exits': specs})
                for change in exit_sync_plan(current, self._owned(db, epoch), hedge=hedge):
                    if self._dispatch_exit(db, epoch, change, symbol):
                        break  # Reconcile the fill against the copied position next cycle.
                db.execute('UPDATE copy_entry_children SET adopted=1 WHERE signal_id=?', (signal_id,))
                print(json.dumps({'copy_mode':'demo','source_exit_orders':len(contract['exits']),
                                  'invented_protection':False}), flush=True)
                return
            if len(new_orders) != len(contract['exits']):
                new_orders = self._recover_entry_children(parent, symbol, before_ids, new_orders)
                if len(new_orders) != len(contract['exits']):
                    raise ReconciliationRequired('Entry attached protection count differs from request')
            hedge = self.hedge_mode()
            matched = set()
            for raw in contract['exits']:
                spec = ExitOrder(**{**raw, 'quantity': quantity})
                params = spec.bingx(symbol, hedge=hedge)
                matches = [o for o in new_orders if self._matches(o, params)]
                if len(matches) != 1:
                    raise ReconciliationRequired('Entry SL/TP not confirmed uniquely on exchange')
                oid = str(matches[0].get('orderID', matches[0].get('orderId', '')))
                if not oid or oid in matched:
                    raise ReconciliationRequired('Attached protection identity ambiguous')
                matched.add(oid)
                canonical = raw['source_id']
                if db.execute('SELECT 1 FROM copy_exit_orders WHERE epoch=? AND source_id=?', (epoch, canonical)).fetchone():
                    canonical += '#'+oid
                db.execute('INSERT OR IGNORE INTO copy_exit_orders VALUES (?,?,?,?)',
                           (epoch, canonical, oid, json.dumps(params, sort_keys=True)))
            db.execute('UPDATE copy_entry_children SET adopted=1 WHERE signal_id=?', (signal_id,))

    def _recover_entry_children(self, parent, symbol, before_ids, active):
        from .bingx_account_stream import order_id
        try:
            parent_id = order_id(parent.get('orderID', parent.get('orderId')))
        except ValueError:
            raise ReconciliationRequired('Entry parent identity missing for protection recovery') from None
        if parent.get('symbol', symbol) != symbol:
            raise ReconciliationRequired('Entry parent symbol differs during protection recovery')
        with sqlite3.connect(self.event_journal_path) as events:
            linked = [r[0] for r in events.execute(
                'SELECT order_id FROM copy_exchange_links WHERE parent_id=? AND symbol=?', (parent_id, symbol))]
        candidates = {str(o.get('orderID', o.get('orderId'))): o for o in active}
        for oid in linked:
            if oid in before_ids:
                raise ReconciliationRequired('Parent-linked child predates entry; inspect exchange')
            if oid in candidates:
                continue
            response = self._request('GET', 'trade/order', symbol=symbol, orderId=oid)
            order = response.get('order', response)
            if (str(order.get('orderID', order.get('orderId'))) != oid
                    or order.get('symbol') != symbol
                    or order.get('status') not in ('FILLED', 'CANCELLED', 'CANCELED', 'EXPIRED', 'FAILED')
                    or 'executedQty' not in order
                    or not 0 <= number(order['executedQty']) <= number(order.get('origQty'), positive=True)):
                raise ReconciliationRequired('Parent-linked protection outcome not confirmed')
            candidates[oid] = order
        return list(candidates.values())

    def _owned(self, db, epoch):
        rows = db.execute('SELECT source_id,order_id,parameters FROM copy_exit_orders WHERE epoch=?', (epoch,))
        return {s: {'order_id': i, 'parameters': json.loads(p)} for s, i, p in rows}

    def synchronize_exits(self, copy_db):
        """Detect changes, own attached exits, mirror updates with a durable fence.

        Any uncertain exchange result blocks all later protection mutations;
        restarting cannot cause another POST for that operation.
        """
        self.verify_account_identity()
        with sqlite3.connect(self.journal_path) as db:
            # Preparing an entry reserves its protection metadata before the
            # durable POST claim. Expiry or a pre-dispatch read failure can
            # leave this reservation behind, even though no order was sent.
            for signal_id, epoch in db.execute(
                    'SELECT signal_id,epoch FROM copy_entry_children WHERE adopted=0').fetchall():
                if self.needs_reconciliation(signal_id):
                    continue  # Accepted/ambiguous requests retain their fence.
                db.execute('DELETE FROM copy_entry_children WHERE signal_id=?', (signal_id,))
                if (not copy_db.execute('SELECT 1 FROM copied_positions WHERE epoch=?', (epoch,)).fetchone()
                        and not db.execute('SELECT 1 FROM copy_entry_children WHERE epoch=?', (epoch,)).fetchone()
                        and not db.execute('SELECT 1 FROM copy_exit_orders WHERE epoch=?', (epoch,)).fetchone()):
                    db.execute('DELETE FROM copy_contracts WHERE epoch=?', (epoch,))
                db.commit()
                print(json.dumps({'copy_status': 'unaccepted_entry_reservation_released',
                                  'signal_id': signal_id}), flush=True)
            for (epoch,) in copy_db.execute('SELECT epoch FROM copied_positions'):
                if not db.execute('SELECT 1 FROM copy_contracts WHERE epoch=?', (epoch,)).fetchone():
                    raise ReconciliationRequired('Legacy copied position lacks verified protection contract')
            for dispatch_id, epoch, payload, oid, result in db.execute(
                    'SELECT d.id,d.epoch,d.payload,r.order_id,r.response FROM copy_exit_dispatch d '
                    'LEFT JOIN copy_exit_receipts r ON r.dispatch_id=d.id WHERE d.result IS NULL').fetchall():
                change = json.loads(payload)
                if change['action'] == 'cancel':
                    self._recover_cancellation(db, dispatch_id, epoch, change)
                    continue
                if not oid:
                    raise ReconciliationRequired('Ambiguous protection operation; inspect exchange before resuming')
                self._confirm_exit(db, dispatch_id, epoch, change, oid, json.loads(result))
            if db.execute('SELECT 1 FROM copy_entry_children WHERE adopted=0 LIMIT 1').fetchone():
                raise ReconciliationRequired('Unadopted entry protection; reconcile parent and children before resuming')
            for epoch, symbol, ratio, payload in db.execute('SELECT * FROM copy_contracts').fetchall():
                row = copy_db.execute('SELECT quantity FROM copied_positions WHERE epoch=?', (epoch,)).fetchone()
                positions = self._positions(symbol)
                if not row:
                    if copy_db.execute("SELECT 1 FROM sqlite_master WHERE name='copy_plans'").fetchone():
                        for (signal_id,) in db.execute('SELECT signal_id FROM copy_entry_children WHERE epoch=?', (epoch,)):
                            if copy_db.execute('SELECT 1 FROM copy_plans p WHERE p.id=? '
                                    'AND NOT EXISTS (SELECT 1 FROM copied_signals s WHERE s.id=p.id) '
                                    'AND NOT EXISTS (SELECT 1 FROM copy_skips k WHERE k.id=p.id)', (signal_id,)).fetchone():
                                raise ReconciliationRequired('Entry journal not committed; reconcile parent before cleanup')
                    if positions:
                        raise ReconciliationRequired('Exchange exposure exists without a copy position')
                    # No new entry until every owned stale exit is gone.
                    for sid, old in self._owned(db, epoch).items():
                        self._dispatch_exit(db, epoch, dict(action='cancel', source_id=sid, order_id=old['order_id']), symbol)
                        db.execute('DELETE FROM copy_exit_orders WHERE epoch=? AND source_id=?', (epoch, sid))
                    db.execute('DELETE FROM copy_contracts WHERE epoch=?', (epoch,)); db.commit()
                    continue
                qty = number(row[0], positive=True)
                actual = sum(abs(number(p['positionAmt'])) for p in positions)
                if len(positions) > 1:
                    raise ReconciliationRequired('Multiple target positions conflict with source one-way exposure')
                if actual != qty:
                    self._reconcile_protective_fills(db, copy_db, epoch, symbol, qty, actual)
                    qty = actual
                    if not qty:
                        # Cleanup sibling protective orders on the next pass.
                        continue
                prior = json.loads(payload)
                actual_lev = self._request('GET', 'trade/leverage', symbol=symbol)
                actual_margin = self._request('GET', 'trade/marginType', symbol=symbol)
                # Source position can be flat while the pending close fill is
                # waiting in the ordered feed; keep current protection then.
                try:
                    contract = self._contract(symbol, number(ratio, positive=True), qty,
                                              for_exit_sync=True)
                except CopyBlocked as error:
                    # Existing protection is retained; worker may still process
                    # risk-reducing source fills, but cannot add new exposure.
                    self.open_blocker = str(error)
                    print(json.dumps({'copy_status': 'source_protection_blocked', 'reason': str(error)}), flush=True)
                    continue
                hedge = self.hedge_mode()
                owned = self._owned(db, epoch)
                orders = self._orders(symbol)
                desired = {o.source_id: o.bingx(symbol, hedge=hedge) for o in contract.exits}
                if not owned and desired and self.required_source_protection:
                    raise ReconciliationRequired('Protection ownership metadata missing; never adopt manual orders')
                # Actual existence/configuration is checked every cycle,
                # including after restart, not just against cached requests.
                by_id = {str(o.get('orderID', o.get('orderId'))): o for o in orders}
                if set(by_id) != {o['order_id'] for o in owned.values()}:
                    raise ReconciliationRequired('Unowned target orders prevent exclusive protection synchronization')
                for sid, old in owned.items():
                    if old['order_id'] not in by_id or not self._matches(by_id[old['order_id']], old['parameters']):
                        raise ReconciliationRequired('Owned protection disappeared or differs on exchange')
                if actual_margin.get('marginType') != contract.margin_type:
                    raise CopyBlocked('margin_mode_changed_with_live_position_requires_reconciliation')
                side_key = 'longLeverage' if contract.position_side == 'LONG' else 'shortLeverage'
                max_key = 'maxLongLeverage' if contract.position_side == 'LONG' else 'maxShortLeverage'
                if contract.isolated_collateral_adjusted:
                    self.open_blocker = 'isolated_collateral_requires_separate_verified_transfer'
                    print(json.dumps({'copy_status': 'isolated_collateral_changed',
                                      'symbol': symbol, 'reason': self.open_blocker}), flush=True)
                elif number(actual_lev.get(side_key)) != contract.leverage:
                    if not self.trading_enabled or contract.leverage > number(actual_lev.get(max_key), positive=True):
                        raise CopyBlocked('live_leverage_change_not_available')
                    # Idempotent setting update only after ownership and
                    # protection are verified. Readback is mandatory.
                    self._request('POST', 'trade/leverage', symbol=symbol,
                                  side=contract.position_side if hedge else 'BOTH',
                                  leverage=contract.leverage)
                    actual_lev = self._request('GET', 'trade/leverage', symbol=symbol)
                if not contract.isolated_collateral_adjusted:
                    verify_target_settings(contract, actual_lev, actual_margin)
                for change in exit_sync_plan(contract, owned, hedge=hedge):
                    if self._dispatch_exit(db, epoch, change, symbol):
                        remaining = sum(abs(number(p['positionAmt'])) for p in self._positions(symbol))
                        self._reconcile_protective_fills(db, copy_db, epoch, symbol, qty, remaining)
                        break
                db.execute('UPDATE copy_contracts SET payload=? WHERE epoch=?',
                           (json.dumps(asdict(contract), sort_keys=True), epoch)); db.commit()

    def _reconcile_protective_fills(self, db, copy_db, epoch, symbol, expected, actual):
        if actual < 0 or actual >= expected:
            raise ReconciliationRequired('Unexplained target position increase; do not add or resubmit')
        newly_filled = Decimal(0)
        updates = []
        terminal = []
        retired_terminal = []
        rows = db.execute('SELECT source_id,order_id,parameters FROM copy_exit_orders WHERE epoch=? '
                          'UNION ALL SELECT NULL,h.order_id,h.parameters FROM copy_exit_history h '
                          'WHERE h.epoch=? AND NOT EXISTS (SELECT 1 FROM copy_exit_orders o '
                          'WHERE o.epoch=h.epoch AND o.order_id=h.order_id)', (epoch, epoch)).fetchall()
        for sid, oid, payload in rows:
            result = self._request('GET', 'trade/order', symbol=symbol, orderId=oid)
            result = result.get('order', result)
            params = json.loads(payload)
            if (str(result.get('orderId', result.get('orderID'))) != oid
                    or result.get('symbol') != symbol or result.get('side') != params['side']
                    or 'executedQty' not in result):
                raise ReconciliationRequired('Protective order identity or fill fields differ from owned order')
            filled = number(result['executedQty'])
            old = db.execute('SELECT executed_qty FROM copy_exit_fills WHERE order_id=?', (oid,)).fetchone()
            prior = number(old[0]) if old else Decimal(0)
            if filled < prior or filled > number(params['quantity'], positive=True):
                raise ReconciliationRequired('Invalid protective fill reconciliation')
            newly_filled += filled - prior
            updates.append((oid, str(filled)))
            if result.get('status') in ('FILLED', 'CANCELLED', 'CANCELED', 'EXPIRED', 'FAILED'):
                if sid is None:
                    retired_terminal.append((epoch, oid))
                else:
                    terminal.append((epoch, sid))
        if newly_filled != expected - actual:
            raise ReconciliationRequired('Position change not explained by confirmed bot-owned protective fills')
        # Keep the verified fill and position update atomic on the worker's DB.
        same_file = db.execute('PRAGMA database_list').fetchone()[2]
        same_file = same_file and same_file == copy_db.execute('PRAGMA database_list').fetchone()[2]
        db.executemany('INSERT OR REPLACE INTO copy_exit_fills VALUES (?,?)', updates)
        # Retire only after its confirmed fills explain the position change.
        # Otherwise removing a just-filled stop would erase the evidence.
        db.executemany('DELETE FROM copy_exit_orders WHERE epoch=? AND source_id=?', terminal)
        db.executemany('DELETE FROM copy_exit_history WHERE epoch=? AND order_id=?', retired_terminal)
        db.execute('INSERT OR REPLACE INTO locally_exited_epochs VALUES (?,?)',
                   (epoch, 'confirmed_bot_protection_fill'))
        if not same_file:
            db.commit()
        target_db = db if same_file else copy_db
        with target_db:
            if actual:
                target_db.execute('UPDATE copied_positions SET quantity=? WHERE epoch=?', (str(actual), epoch))
            else:
                target_db.execute('DELETE FROM copied_positions WHERE epoch=?', (epoch,))

    @staticmethod
    def _matches(order, parameters):
        for key in ('symbol', 'side', 'positionSide', 'type'):
            if order.get(key) != parameters.get(key):
                return False
        for key, response_key in [('quantity', 'origQty'), ('stopPrice', 'stopPrice'), ('price', 'price')]:
            if key in parameters and number(order.get(response_key)) != number(parameters[key]):
                return False
        for key in ('workingType', 'timeInForce'):
            if key in parameters and order.get(key) != parameters[key]:
                return False
        if 'reduceOnly' in parameters and str(order.get('reduceOnly')).lower() != parameters['reduceOnly']:
            return False
        return True

    def _dispatch_exit(self, db, epoch, change, symbol):
        if not self.trading_enabled:
            raise CopyBlocked('protection_mutation_disabled')
        payload = json.dumps(change, sort_keys=True)
        cursor = db.execute('INSERT INTO copy_exit_dispatch(epoch,source_id,action,payload) VALUES (?,?,?,?)',
                            (epoch, change['source_id'], change['action'], payload))
        dispatch_id = cursor.lastrowid
        db.commit()  # uncertainty fence must exist BEFORE the request
        action = change['action']
        if action == 'cancel':
            existing = self._request('GET', 'trade/order', symbol=symbol, orderId=change['order_id'])
            existing = existing.get('order', existing)
            if existing.get('status') not in ('FILLED', 'CANCELLED', 'CANCELED', 'EXPIRED', 'FAILED'):
                self._request('DELETE', 'trade/order', symbol=symbol, orderId=change['order_id'])
                response = self._request('GET', 'trade/order', symbol=symbol, orderId=change['order_id'])
                existing = response.get('order', response)
            return self._finish_cancellation(db, dispatch_id, epoch, change, symbol, existing)
        else:
            params = change['parameters']
            if action == 'replace':
                result = self._request('POST', 'trade/cancelReplace', version=1,
                    cancelOrderId=change['order_id'], cancelReplaceMode='STOP_ON_FAILURE', **params)
                oid = self._replacement_id(result, change['order_id'])
            else:
                result = self._request('POST', 'trade/order', **params)
                result_order = result.get('order', result)
                oid = str(result_order.get('orderID', result_order.get('orderId', '')))
            if not oid.isdigit() or oid == '0':
                raise ReconciliationRequired('Protection response missing valid order ID')
            db.execute('INSERT INTO copy_exit_receipts VALUES (?,?,?)', (dispatch_id, oid, json.dumps(result)))
            db.commit()
            return self._confirm_exit(db, dispatch_id, epoch, change, oid, result)
        db.execute('UPDATE copy_exit_dispatch SET result=? WHERE id=?', (json.dumps(result), dispatch_id)); db.commit()
        return False

    def _recover_cancellation(self, db, dispatch_id, epoch, change):
        """Recover a completed cancellation by reads; never issue another DELETE."""
        owned = db.execute('SELECT parameters FROM copy_exit_orders '
                           'WHERE epoch=? AND source_id=? AND order_id=?',
                           (epoch, change['source_id'], change['order_id'])).fetchone()
        if not owned:
            raise ReconciliationRequired('Pending cancellation lacks owned order metadata')
        symbol = json.loads(owned[0])['symbol']
        response = self._request('GET', 'trade/order', symbol=symbol, orderId=change['order_id'])
        order = response.get('order', response)
        return self._finish_cancellation(db, dispatch_id, epoch, change, symbol, order)

    def _finish_cancellation(self, db, dispatch_id, epoch, change, symbol, order):
        owned = db.execute('SELECT parameters FROM copy_exit_orders WHERE epoch=? AND source_id=? AND order_id=?',
                           (epoch, change['source_id'], change['order_id'])).fetchone()
        if not owned:
            raise ReconciliationRequired('Cancellation lacks owned order metadata')
        params = json.loads(owned[0])
        filled = number(order.get('executedQty'))
        if (str(order.get('orderId', order.get('orderID'))) != change['order_id']
                or order.get('symbol') != symbol
                or order.get('status') not in ('FILLED', 'CANCELLED', 'CANCELED', 'EXPIRED', 'FAILED')
                or not 0 <= filled <= number(params['quantity'], positive=True)
                or (filled and order.get('side') != params['side'])
                or (order.get('status') == 'FILLED' and filled != number(params['quantity']))
                or any(str(o.get('orderId', o.get('orderID'))) == change['order_id']
                       for o in self._orders(symbol))):
            raise ReconciliationRequired('Cancellation outcome remains uncertain; inspect exchange')
        with db:
            if not filled:
                db.execute('DELETE FROM copy_exit_orders WHERE epoch=? AND source_id=?',
                           (epoch, change['source_id']))
            db.execute('UPDATE copy_exit_dispatch SET result=? WHERE id=?',
                       (json.dumps({'confirmed_cancel': order}), dispatch_id))
        return filled > 0

    def _confirm_exit(self, db, dispatch_id, epoch, change, oid, result):
        params = change['parameters']
        symbol = params['symbol']
        active = {str(o.get('orderID', o.get('orderId'))): o for o in self._orders(symbol)}
        if change['action'] == 'replace' and change['order_id'] in active:
            raise ReconciliationRequired('Old protection still open after replacement; inspect orders')
        match = active.get(oid)
        filled = False
        if not match:
            response = self._request('GET', 'trade/order', symbol=symbol, orderId=oid)
            terminal = response.get('order', response)
            if (str(terminal.get('orderId', terminal.get('orderID'))) == oid
                    and terminal.get('status') == 'FILLED'
                    and self._matches(terminal, params)
                    and number(terminal['executedQty']) == number(params['quantity'], positive=True)):
                match, filled = terminal, True
        if not match or not self._matches(match, params):
            raise ReconciliationRequired('Protection creation/replacement unconfirmed; never resubmit blindly')
        with db:
            if change['action'] == 'replace':
                old = db.execute('SELECT parameters FROM copy_exit_orders '
                                 'WHERE epoch=? AND source_id=? AND order_id=?',
                                 (epoch, change['source_id'], change['order_id'])).fetchone()
                if not old:
                    raise ReconciliationRequired('Replacement lacks original owned protection metadata')
                db.execute('INSERT OR IGNORE INTO copy_exit_history VALUES (?,?,?)',
                           (epoch, change['order_id'], old[0]))
            db.execute('INSERT OR REPLACE INTO copy_exit_orders VALUES (?,?,?,?)',
                       (epoch, change['source_id'], oid, json.dumps(params, sort_keys=True)))
            db.execute('UPDATE copy_exit_dispatch SET result=? WHERE id=?', (json.dumps(result), dispatch_id))
        return filled

    def _replacement_id(self, result, old_id):
        if result.get('cancelResult') == 'SUCCESS' and result.get('newOrderResult') == 'SUCCESS':
            new_id = result.get('newOrderId')
        elif (self.environment == 'demo' and result.get('cancelResult') in ('true', True)
              and result.get('replaceResult') in ('true', True)):
            cancelled = result.get('cancelResponse')
            created = result.get('newOrderResponse')
            if (not isinstance(cancelled, dict) or str(cancelled.get('cancelOrderId')) != old_id
                    or not isinstance(created, dict)):
                raise ReconciliationRequired('Protection replacement response has unverified order identities')
            new_id = created.get('orderId')
        else:
            raise ReconciliationRequired('Protection cancel/replace failed; inspect remaining protection immediately')
        if new_id is None or str(new_id) in ('', old_id):
            raise ReconciliationRequired('Protection replacement has no distinct new order ID')
        return str(new_id)
