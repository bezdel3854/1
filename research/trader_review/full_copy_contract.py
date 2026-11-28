"""Compile visible source settings and exit orders without submitting trades.

This is a fail-closed contract, not a claim of identical cross-exchange fills.
Unknown fields/semantics require review rather than guessed replacements.
"""
from dataclasses import dataclass, asdict
from decimal import Decimal, InvalidOperation, ROUND_DOWN
import hashlib
import json
import time


class CopyBlocked(ValueError):
    pass


def number(value, *, positive=False):
    if isinstance(value, bool):
        raise CopyBlocked('boolean_in_numeric_field')
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise CopyBlocked('invalid_number') from None
    if not out.is_finite() or (positive and out <= 0):
        raise CopyBlocked('invalid_number')
    return out


def json_number(value):
    """BingX nested prices must be JSON numbers; refuse lossy conversions."""
    value = number(value, positive=True)
    out = float(value)
    if number(out) != value:
        raise CopyBlocked('price_not_exactly_serializable')
    return out


def exact_price(value, tick):
    value, tick = number(value, positive=True), number(tick, positive=True)
    if value % tick:
        raise CopyBlocked('source_price_not_representable_on_target')
    return str(value)


def scaled_quantity(source_quantity, ratio, step, min_quantity, min_notional, price):
    qty = number(source_quantity, positive=True) * number(ratio, positive=True)
    step = number(step, positive=True)
    rounded = (qty / step).to_integral_value(rounding=ROUND_DOWN) * step
    if rounded <= 0 or rounded < number(min_quantity) or rounded * number(price, positive=True) < number(min_notional):
        raise CopyBlocked('proportional_quantity_below_exchange_minimum')
    return rounded


def flatten_orders(orders):
    if not isinstance(orders, list):
        raise CopyBlocked('invalid_source_orders')
    result = {}
    def visit(order, active=True):
        if not isinstance(order, dict):
            raise CopyBlocked('invalid_source_order')
        # Children of a pending parent are not necessarily live protections.
        # Only separately visible top-level orders can certify protection.
        if active:
            identity = str(order.get('oid', ''))
            if not identity or not identity.isdigit():
                raise CopyBlocked('missing_source_order_identity')
            payload = {k: v for k, v in order.items() if k != 'children'}
            if identity in result and result[identity] != payload:
                raise CopyBlocked('conflicting_source_order_identity')
            result[identity] = payload
    for order in orders:
        visit(order)
    return list(result.values())


@dataclass(frozen=True)
class ExitOrder:
    source_id: str
    kind: str
    side: str
    quantity: str
    full_position: bool
    stop_price: str | None = None
    price: str | None = None
    time_in_force: str | None = None
    working_type: str | None = None

    def bingx(self, symbol, *, hedge=False):
        data = dict(symbol=symbol, side=self.side, type=self.kind,
                    quantity=self.quantity, positionSide=('LONG' if self.side == 'SELL' else 'SHORT') if hedge else 'BOTH')
        if not hedge:
            data['reduceOnly'] = 'true'
        if self.stop_price is not None:
            data['stopPrice'] = self.stop_price
            data['workingType'] = self.working_type
        if self.price is not None:
            data['price'] = self.price
        if self.time_in_force is not None:
            data['timeInForce'] = self.time_in_force
        return data


@dataclass(frozen=True)
class CopyContract:
    symbol: str
    position_side: str
    leverage: int
    margin_type: str
    source_quantity: str
    target_quantity: str
    source_margin: str
    target_margin: str
    exits: tuple[ExitOrder, ...]
    isolated_collateral_adjusted: bool = False

    def fingerprint(self):
        payload = asdict(self)
        # Mark price changes the margin amount without amending any order.
        payload.pop('source_margin'); payload.pop('target_margin')
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def entry_brackets(self, *, require_pair=True):
        """Only certify an atomic attached pair for two whole-position market exits."""
        stops = [x for x in self.exits if x.kind == 'STOP_MARKET' and x.full_position]
        profits = [x for x in self.exits if x.kind == 'TAKE_PROFIT_MARKET' and x.full_position]
        if (len(stops) > 1 or len(profits) > 1
                or len(stops) + len(profits) != len(self.exits)
                or (require_pair and (len(stops) != 1 or len(profits) != 1))):
            raise CopyBlocked('entry_requires_single_full_position_market_sl_and_tp')
        return {key: json.dumps({'type': order.kind, 'stopPrice': json_number(order.stop_price),
                                'workingType': 'MARK_PRICE', 'stopGuaranteed': False}, sort_keys=True)
                for key, orders in [('stopLoss', stops), ('takeProfit', profits)]
                for order in orders}


def compile_contract(account, orders, coin, target_equity, rules, *, observed_ms,
                     now_ms=None, ratio=None, target_quantity=None, require_protection=True,
                     copy_executed_entries_only=False, for_exit_sync=False):
    now_ms = time.time_ns() // 1_000_000 if now_ms is None else now_ms
    if not isinstance(observed_ms, int) or not 0 <= now_ms - observed_ms <= 5000:
        raise CopyBlocked('stale_or_future_source_snapshot')
    positions = [p['position'] for p in account.get('assetPositions', []) if p['position'].get('coin') == coin]
    if len(positions) != 1:
        raise CopyBlocked('source_position_missing_or_ambiguous')
    p = positions[0]
    source_qty = number(p['szi'])
    if not source_qty:
        raise CopyBlocked('source_position_flat')
    long = source_qty > 0
    lev = p.get('leverage', {})
    leverage = number(lev.get('value'), positive=True)
    if leverage != leverage.to_integral_value():
        raise CopyBlocked('non_integer_source_leverage')
    if lev.get('type') not in ('cross', 'isolated'):
        raise CopyBlocked('unknown_margin_mode')
    margin_type = {'cross': 'CROSSED', 'isolated': 'ISOLATED'}[lev['type']]
    source_equity = number(account['marginSummary']['accountValue'], positive=True)
    target_equity = number(target_equity, positive=True)
    ratio = number(ratio, positive=True) if ratio is not None else target_equity / source_equity
    price = number(rules['price'], positive=True)
    qty = scaled_quantity(abs(source_qty), ratio, rules['quantity_step'], rules['minimum_quantity'], rules['minimum_notional'], price)
    if target_quantity is not None:
        qty = number(target_quantity, positive=True)
        if qty % number(rules['quantity_step'], positive=True):
            raise CopyBlocked('target_quantity_not_representable')
    source_margin = number(p['marginUsed'], positive=True)
    target_margin = source_margin * ratio
    adjusted = False
    if margin_type == 'ISOLATED':
        # Compare collateral before unrealized PnL to the ordinary entry
        # margin. The entry price published by the source may be rounded.
        try:
            entry_price = number(p.get('entryPx'), positive=True)
            collateral = number(lev.get('rawUsd')) + source_qty * entry_price
            reported_collateral = source_margin - number(p.get('unrealizedPnl'))
            initial_margin = abs(source_qty) * entry_price / leverage
            price_resolution = Decimal(1).scaleb(entry_price.as_tuple().exponent)
            rounding = abs(source_qty) * price_resolution / 2
            tolerance = Decimal('.01') + min(rounding, initial_margin * Decimal('.0001'))
            adjusted = (collateral <= 0
                        or abs(collateral - reported_collateral) > tolerance
                        or abs(collateral - initial_margin) > tolerance)
        except CopyBlocked:
            adjusted = True
        if adjusted and not for_exit_sync:
            raise CopyBlocked('isolated_collateral_requires_separate_verified_transfer')
        # BingX establishes initial margin from its own execution price.
        target_margin = qty * price / leverage
    if target_margin > target_equity and not (for_exit_sync and margin_type == 'ISOLATED'):
        raise CopyBlocked('insufficient_proportional_margin')
    tick = number(rules['price_tick'], positive=True)
    exits = []
    for o in flatten_orders(orders):
        if o.get('coin') != coin:
            continue
        if o.get('reduceOnly') is not True:
            # A limit entry may already be partially filled. In execution-follow
            # mode, copy only its confirmed fills from the ordered feed. Do not
            # submit its remaining quantity as well: that would double-copy it.
            if (copy_executed_entries_only and o.get('reduceOnly') is False
                    and o.get('side') == ('B' if long else 'A')
                    and o.get('orderType') == 'Limit'
                    and o.get('isTrigger') is False
                    and o.get('tif') in ('Gtc', 'Ioc', 'Alo')):
                number(o.get('sz'), positive=True)
                number(o.get('limitPx'), positive=True)
                continue
            raise CopyBlocked('pending_source_entry_requires_order_mirroring')
        if o.get('side') != ('A' if long else 'B'):
            raise CopyBlocked('source_exit_wrong_side')
        whole = o.get('isPositionTpsl') is True and number(o.get('sz', '0')) == 0
        source_exit_qty = abs(source_qty) if whole else number(o['sz'], positive=True)
        if source_exit_qty > abs(source_qty):
            raise CopyBlocked('source_exit_exceeds_position')
        q = qty if whole else scaled_quantity(source_exit_qty, ratio, rules['quantity_step'], rules['minimum_quantity'], rules['minimum_notional'], price)
        if copy_executed_entries_only:
            # Separately rounded entry fills can leave less size on BingX.
            q = min(q, qty)
        name = o.get('orderType')
        mapping = {'Stop Market': 'STOP_MARKET', 'Take Profit Market': 'TAKE_PROFIT_MARKET',
                   'Stop Limit': 'STOP', 'Take Profit Limit': 'TAKE_PROFIT', 'Limit': 'LIMIT'}
        if name not in mapping:
            raise CopyBlocked('unsupported_source_exit_type')
        kind = mapping[name]
        trigger = kind != 'LIMIT'
        if bool(o.get('isTrigger')) != trigger:
            raise CopyBlocked('source_order_type_conflict')
        stop = exact_price(o['triggerPx'], tick) if trigger else None
        limit = exact_price(o['limitPx'], tick) if kind in ('STOP', 'TAKE_PROFIT', 'LIMIT') else None
        if kind in ('STOP', 'STOP_MARKET') and ((number(stop) >= price) if long else (number(stop) <= price)):
            raise CopyBlocked('stop_already_crossed_on_target')
        if kind in ('TAKE_PROFIT', 'TAKE_PROFIT_MARKET') and ((number(stop) <= price) if long else (number(stop) >= price)):
            raise CopyBlocked('take_profit_already_crossed_on_target')
        tif = None
        if kind in ('STOP', 'TAKE_PROFIT', 'LIMIT'):
            tif_map = {'Gtc': 'GTC', 'Ioc': 'IOC', 'Alo': 'PostOnly'}
            if o.get('tif') not in tif_map:
                raise CopyBlocked('unknown_source_time_in_force')
            tif = tif_map[o['tif']]
        exits.append(ExitOrder(str(o['oid']), kind, 'SELL' if long else 'BUY', str(q), whole,
                               stop, limit, tif, 'MARK_PRICE' if trigger else None))
    stops = [o for o in exits if o.kind in ('STOP', 'STOP_MARKET')]
    profits = [o for o in exits if o.kind in ('TAKE_PROFIT', 'TAKE_PROFIT_MARKET', 'LIMIT')]
    if not stops and require_protection:
        raise CopyBlocked('source_has_no_stop_loss')
    if require_protection and sum(number(o.quantity) for o in stops) < qty:
        raise CopyBlocked('source_stop_loss_does_not_cover_position')
    if not profits and require_protection:
        raise CopyBlocked('source_has_no_take_profit')
    return CopyContract(coin+'-USDT', 'LONG' if long else 'SHORT', int(leverage), margin_type,
                        str(abs(source_qty)), str(qty), str(source_margin), str(target_margin),
                        tuple(sorted(exits, key=lambda x: x.source_id)), adjusted)


def verify_target_settings(contract, leverage, margin):
    side = 'longLeverage' if contract.position_side == 'LONG' else 'shortLeverage'
    if number(leverage.get(side)) != contract.leverage or margin.get('marginType') != contract.margin_type:
        raise CopyBlocked('target_leverage_or_margin_mismatch')


def settings_plan(contract, leverage, margin, *, hedge, target_has_position, target_has_orders):
    """Return explicit mutations; callers must verify readback before an entry."""
    side = 'longLeverage' if contract.position_side == 'LONG' else 'shortLeverage'
    max_key = 'maxLongLeverage' if contract.position_side == 'LONG' else 'maxShortLeverage'
    if contract.leverage > number(leverage.get(max_key), positive=True):
        raise CopyBlocked('source_leverage_exceeds_target_limit')
    changes = []
    if margin.get('marginType') != contract.margin_type:
        changes.append(('POST', 'trade/marginType', {'symbol': contract.symbol, 'marginType': contract.margin_type}))
    if number(leverage.get(side)) != contract.leverage:
        changes.append(('POST', 'trade/leverage', {'symbol': contract.symbol,
                        'side': contract.position_side if hedge else 'BOTH', 'leverage': contract.leverage}))
    if changes and (target_has_position or target_has_orders):
        raise CopyBlocked('settings_change_would_affect_existing_exposure')
    return changes


def exit_sync_plan(contract, owned_orders, *, hedge=False):
    """Plan bot-owned exits only. Never cancel unrelated/manual exchange orders.

    Conditional orders lack guaranteed client-ID idempotency on BingX. An
    executor must journal requests before dispatch and fence ambiguous results.
    This function deliberately does not execute operations.
    """
    desired = {o.source_id: o.bingx(contract.symbol, hedge=hedge) for o in contract.exits}
    plan = []
    for source_id, params in desired.items():
        old = owned_orders.get(source_id)
        if old and old.get('ambiguous'):
            raise CopyBlocked('ambiguous_previous_protection_submission')
        if old is None:
            plan.append({'action': 'create', 'source_id': source_id, 'parameters': params})
        elif old['parameters'] != params:
            if not old.get('order_id'):
                raise CopyBlocked('missing_target_protection_order_id')
            plan.append({'action': 'replace', 'source_id': source_id,
                         'order_id': str(old['order_id']), 'parameters': params,
                         'cancelReplaceMode': 'STOP_ON_FAILURE', 'verify_position_after': True})
    for source_id, old in owned_orders.items():
        if source_id not in desired:
            if old.get('ambiguous') or not old.get('order_id'):
                raise CopyBlocked('ambiguous_previous_protection_submission')
            plan.append({'action': 'cancel', 'source_id': source_id, 'order_id': str(old['order_id'])})
    # First create replacements, then cancel obsolete exits. Reduce-only is
    # essential when old and new protection overlap during that interval.
    return plan
