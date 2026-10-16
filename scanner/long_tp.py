"""Minimum-size resting long profit at +7%; native remainder TP at +15%."""
import time
from decimal import Decimal as D, ROUND_UP
from .market import decimal
from .exchange import target, ExchangeError
from .averaging import TERMINAL, filled_quantity


def minimum_partial(info, qty, price):
    step = decimal(info['quantity_increment'], True)
    minimum = max(decimal(info['min_quantity'], True), decimal(info.get('min_trade_size', info['min_quantity']), True))
    minimum = (minimum / step).to_integral_value(rounding=ROUND_UP) * step
    close_notional = decimal(info.get('close_min_notional', info['min_notional']))
    if close_notional < 0:raise ValueError('INVALID_CLOSE_MIN_NOTIONAL')
    part = (max(minimum, close_notional / decimal(price, True)) / step).to_integral_value(rounding=ROUND_UP) * step
    if qty <= 0 or qty % step:
        raise ValueError('INVALID_LONG_SPLIT_QUANTITY')
    if part > decimal(info['max_quantity'], True):
        raise ValueError('LONG_PARTIAL_EXCEEDS_MAXIMUM_QUANTITY')
    if qty - part < minimum:
        raise ValueError('LONG_TOO_SMALL_FOR_MINIMUM_PARTIAL_TP')
    return part


def replay_long(fills, buy_ids, sell_ids):
    qty = cost = D(0)
    closed = False
    for f in sorted(fills, key=lambda x: float(x['timestamp'])):
        q, price, oid = decimal(f['quantity'], True), decimal(f['price'], True), f['order_id']
        if f['side'] == 'buy':
            if oid not in buy_ids or closed:
                raise ValueError('UNATTRIBUTED_LONG_ENTRY_OR_REOPEN')
            qty += q; cost += q * price
        elif f['side'] == 'sell':
            if oid not in sell_ids or q > qty:
                raise ValueError('UNATTRIBUTED_LONG_REDUCTION')
            cost -= q * (cost / qty); qty -= q
            if qty == 0:
                closed = True; cost = D(0)
        else:
            raise ValueError('INVALID_FILL_SIDE')
    return qty, cost / qty if qty else D(0)


class LongTakeProfit:
    def __init__(self, engine):
        self.e, self.db, self.ex, self.emit = engine, engine.db, engine.ex, engine.emit

    def save(self, trade, **changes):
        row = self.db.get(trade['id'])
        state = {**row['data'].get('long_tp', {}), **changes}
        self.db.update(row['id'], row['status'], long_tp=state)
        return state

    def latest(self, trade):
        items = self.db.get(trade['id'])['data'].get('long_tp_orders', [])
        return items[-1] if items else {}

    def update_order(self, trade, **changes):
        row = self.db.get(trade['id']); items = list(row['data']['long_tp_orders'])
        items[-1] = {**items[-1], **changes}
        self.db.update(row['id'], row['status'], long_tp_orders=items)

    def uncertain(self, trade, reason):
        self.db.update(trade['id'], 'UNCERTAIN')
        self.emit('LONG_TP_UNCERTAIN', pair=trade['pair'], trade_id=trade['id'], reason=reason)
        return False

    def cancel_resting(self, trade, reason):
        a = self.latest(trade)
        if not a or a.get('status') in TERMINAL:
            return True
        if not a.get('order_id'):
            return self.uncertain(trade, 'MISSING_LIMIT_ACKNOWLEDGEMENT')
        order = self.ex.find_order(trade['pair'], 'SELL', a['order_id'])
        if order is None:
            return self.uncertain(trade, 'LIMIT_NOT_FOUND')
        self.update_order(trade, status=order['status'], filled_quantity=str(filled_quantity(order)))
        if order['status'] in TERMINAL:
            return True
        attempts = a.get('cancel_attempts', 0)
        if attempts >= 3:
            return self.uncertain(trade, 'LIMIT_CANCEL_NOT_CONFIRMED')
        if not attempts or time.time() - a.get('cancel_requested_at', 0) >= 30:
            self.update_order(trade, cancel_attempts=attempts + 1, cancel_requested_at=time.time(), cancel_reason=reason)
            self.ex.cancel(trade['pair'], order)
            self.emit('LONG_TP_CANCEL_REQUESTED', pair=trade['pair'], trade_id=trade['id'], reason=reason)
        return False

    def prepare(self, trade):
        """Reconcile the partial exit before interpreting a changed position size."""
        a = self.latest(trade)
        if not a or a.get('status') in TERMINAL:
            return True
        if not a.get('order_id'):
            return self.uncertain(trade, 'MISSING_LIMIT_ACKNOWLEDGEMENT')
        order = self.ex.find_order(trade['pair'], 'SELL', a['order_id'])
        if order is None:
            return self.uncertain(trade, 'LIMIT_NOT_FOUND')
        executed = filled_quantity(order)
        self.update_order(trade, status=order['status'], filled_quantity=str(executed))
        if (decimal(order['total_quantity']) != decimal(a['quantity'])
                or decimal(order.get('price') or 0) != decimal(a['limit_price'])
                or order.get('order_type') != 'limit_order'):
            self.cancel_resting(trade, 'LIMIT_CHANGED')
            self.e.conflict(trade, 'LONG_TP_LIMIT_CHANGED'); return False
        if order['status'] in TERMINAL:
            return True
        d = self.db.get(trade['id'])['data']; s = d['long_tp']
        positions = [p for p in self.ex.positions(trade['pair']) if decimal(p['active_pos']) != 0]
        reason = a.get('cancel_reason')
        if len(positions) != 1 or positions[0]['id'] != d['position_id'] or decimal(positions[0]['active_pos']) <= 0:
            reason = 'POSITION_CLOSED_OR_REVERSED'
        elif (decimal(positions[0]['active_pos']) + executed != decimal(s['base_quantity'])
              or decimal(positions[0]['avg_price']) != decimal(s['reference'])):
            reason = 'POSITION_SIZE_OR_AVERAGE_CHANGED'
        elif positions[0].get('margin_type') != 'isolated' or decimal(positions[0].get('leverage') or 0) != 1:
            reason = 'POSITION_SETTINGS_CHANGED'
        elif decimal(positions[0].get('take_profit_trigger') or 0) != 0:
            reason = 'COMPETING_NATIVE_TP'
        elif any(o['id'] != a['order_id'] for o in self.ex.orders(trade['pair'], 'SELL')):
            reason = 'OTHER_SELL_ORDER_PENDING'
        if reason:
            self.cancel_resting(trade, reason); return False
        return True

    def arm_runner(self, trade, p, tp, pct=None):
        if decimal(p.get('take_profit_trigger') or 0) != tp:
            if self.ex.price(trade['pair'], 'SELL') >= tp:
                if not self.e.long_averager.cancel_own(trade, 'FINAL_TP_EXIT'):
                    return False
                self.db.update(trade['id'], 'CLOSING', exit_requested_at=time.time())
                self.ex.exit(trade['pair'], p)
                self.emit('TP_EXIT_REQUESTED', pair=trade['pair'], trade_id=trade['id']); return False
            attempt = self.db.get(trade['id'])['data'].get('long_tp', {}).get('attach', {})
            if attempt.get('target') != str(tp):
                attempt = dict(target=str(tp), attempts=0)
            if attempt['attempts'] >= 3:
                return self.uncertain(trade, 'RUNNER_TP_NOT_CONFIRMED')
            if attempt['attempts'] and time.time() - attempt.get('at', 0) < 30:
                return False
            self.save(trade, attach={**attempt, 'attempts': attempt['attempts'] + 1, 'at': time.time()})
            qty, avg = decimal(p['active_pos']), decimal(p['avg_price'])
            self.ex.take_profit(trade['pair'], p, tp)
            fresh = next((x for x in self.ex.positions(trade['pair']) if x['id'] == p['id']), None)
            if fresh is None or decimal(fresh['active_pos']) == 0:
                return False
            if (decimal(fresh['active_pos']) != qty or decimal(fresh['avg_price']) != avg
                    or fresh.get('margin_type') != 'isolated' or decimal(fresh.get('leverage') or 0) != 1):
                self.e.conflict(trade, 'POSITION_CHANGED_DURING_TP_UPDATE'); return False
            if decimal(fresh.get('take_profit_trigger') or 0) != tp:
                self.emit('TP_PENDING', pair=trade['pair'], trade_id=trade['id']); return False
        self.save(trade, attach={})
        self.db.update(trade['id'], 'OPEN', tp=str(tp), tp_pct=str(self.e.c.long_runner_tp if pct is None else pct))
        return True

    def cancel_native(self, trade, p):
        orders = self.ex.orders(trade['pair'], 'SELL')
        native = [o for o in orders if o.get('stage') == 'tpsl_exit'
                  and o.get('order_category') == 'complete_tpsl' and o.get('order_type') == 'take_profit_market']
        if any(o not in native for o in orders):
            self.emit('LONG_TP_WAITING', pair=trade['pair'], trade_id=trade['id'], reason='MANUAL_SELL_ORDER_PENDING')
            return False
        if len(native) > 1:
            self.e.conflict(trade, 'MULTIPLE_NATIVE_LONG_TPS'); return False
        s = self.db.get(trade['id'])['data']['long_tp']
        if native:
            order = native[0]
            if decimal(order.get('stop_price') or 0) != decimal(p.get('take_profit_trigger') or 0):
                return False
            cancel = s.get('native_cancel') or {}
            if cancel.get('order_id') != order['id']:
                cancel = dict(order_id=order['id'], attempts=0)
            if cancel['attempts'] >= 3:
                return self.uncertain(trade, 'NATIVE_TP_CANCEL_NOT_CONFIRMED')
            if not cancel['attempts'] or time.time() - cancel.get('at', 0) >= 30:
                self.save(trade, native_cancel={**cancel, 'attempts': cancel['attempts'] + 1, 'at': time.time()})
                self.ex.cancel(trade['pair'], order)
            return False
        if s.get('native_cancel'):
            order = self.ex.find_order(trade['pair'], 'SELL', s['native_cancel']['order_id'])
            if order is None or order['status'] not in ('cancelled', 'rejected'):
                return False
        return decimal(p.get('take_profit_trigger') or 0) == 0

    def checked_part(self, trade, qty, tp):
        info = self.e.market.metadata(trade['pair'])
        quote = self.e.market.quotes().get(trade['pair'])
        if quote is None or not -30 <= time.time() - quote.timestamp <= 60:
            raise ValueError('STALE_LIMIT_QUOTE')
        # A SELL limit cannot fill below its limit. Size its minimum notional
        # at that price so it can rest while the market is still below target.
        part = minimum_partial(info, qty, tp)
        if info.get('order_types') and 'limit_order' not in info['order_types']:
            raise ValueError('LONG_TP_LIMIT_NOT_SUPPORTED')
        if tp % decimal(info['price_increment'], True) or not decimal(info['min_price'], True) <= tp <= decimal(info['max_price'], True):
            raise ValueError('LONG_TP_INVALID_PRICE')
        # SELL lower-bound protection; a resting sell above LTP is intentional.
        if info.get('multiplier_down') is not None and tp < quote.price * (1 - decimal(info['multiplier_down']) / 100):
            raise ValueError('LONG_TP_OUTSIDE_EXCHANGE_LTP_RANGE')
        return part

    def manage(self, trade, p):
        ident, d = trade['id'], trade['data']
        qty, fill = decimal(p['active_pos'], True), decimal(p['avg_price'], True)
        s, a = d.get('long_tp', {}), self.latest(trade)
        if (s.get('phase') == 'runner' and qty == decimal(s['runner_quantity'])
                and fill == decimal(s['reference'])):
            self.arm_runner(trade, p, decimal(s['tp2'])); return True
        if s.get('phase') == 'resting':
            if a.get('cycle') != s.get('cycle'):
                self.e.conflict(trade, 'LONG_TP_CYCLE_MISMATCH'); return True
            if a['status'] not in TERMINAL:
                self.db.update(ident, 'OPEN', tp=s['tp1'], tp_pct=str(self.e.c.long_tp)); return True
            executed = decimal(a.get('filled_quantity') or 0)
            if (executed == decimal(s['tp1_quantity']) and qty == decimal(s['base_quantity']) - executed
                    and abs(fill - decimal(s['reference'])) <= decimal(d['info']['price_increment'], True)):
                if not self.e.long_averager.cancel_own(trade, 'FIRST_LONG_PROFIT_FILLED'):
                    return True
                self.save(trade, phase='runner', runner_quantity=str(qty), reference=str(fill), attach={}, native_cancel=None)
                self.emit('LONG_PARTIAL_TP_FILLED', pair=trade['pair'], trade_id=ident,
                          closed_quantity=str(executed), remaining_quantity=str(qty), remaining_tp=s['tp2'])
                self.arm_runner(self.db.get(ident), p, decimal(s['tp2'])); return True
            incomplete = executed > 0 and qty + executed == decimal(s['base_quantity']) and fill == decimal(s['reference'])
            if incomplete or not a.get('cancel_reason'):
                # A rejected first exit must not silently move all profit to +15%.
                self.arm_runner(trade, p, decimal(s['tp2'] if executed else s['tp1']),
                                self.e.c.long_runner_tp if executed else self.e.c.long_tp)
                if self.db.get(ident)['status'] != 'CLOSING':
                    self.e.conflict(trade, 'LONG_TP_LIMIT_INCOMPLETE_OR_CANCELLED')
                return True
            s = {**s, 'phase': 'replan'}
        if not s or s.get('phase') in ('runner', 'replan') or qty != decimal(s['base_quantity']) or fill != decimal(s['reference']):
            if not self.cancel_resting(trade, 'REPLAN'):
                return True
            tp1 = target(d['info'], 'BUY', fill, self.e.c.long_tp)
            tp2 = target(d['info'], 'BUY', fill, self.e.c.long_runner_tp)
            s = dict(base_quantity=str(qty), reference=str(fill), tp1=str(tp1), tp2=str(tp2),
                     phase='waiting', cycle=s.get('cycle', 0) + 1)
            self.db.update(ident, 'PROTECTING', long_tp=s)
        if time.time() < s.get('retry_at', 0):
            return False  # Keep the full +7% fallback until a valid split is possible.
        tp1, tp2 = decimal(s['tp1']), decimal(s['tp2'])
        if self.ex.price(trade['pair'], 'SELL') >= tp2:
            self.arm_runner(trade, p, tp2); return True
        try:
            self.checked_part(trade, qty, tp1)
        except ValueError as error:
            self.save(trade, retry_at=time.time() + 60)
            self.emit('LONG_SPLIT_TP_UNAVAILABLE', pair=trade['pair'], trade_id=ident, reason=str(error), fallback='Full position at +7%')
            return False
        if not self.cancel_native(trade, p):
            return True
        fresh = [x for x in self.ex.positions(trade['pair']) if decimal(x['active_pos']) != 0]
        if (len(fresh) != 1 or fresh[0]['id'] != p['id'] or decimal(fresh[0]['active_pos']) != qty
                or decimal(fresh[0]['avg_price']) != fill or fresh[0].get('margin_type') != 'isolated'
                or decimal(fresh[0].get('leverage') or 0) != 1
                or decimal(fresh[0].get('take_profit_trigger') or 0) != 0
                or decimal(fresh[0].get('stop_loss_trigger') or 0) != 0):
            return True
        if self.ex.orders(trade['pair'], 'SELL'):
            return True
        if self.ex.price(trade['pair'], 'SELL') >= tp2:
            self.arm_runner(trade, fresh[0], tp2); return True
        try:
            part = self.checked_part(trade, qty, tp1)
        except ValueError as error:
            self.save(trade, retry_at=time.time() + 60)
            self.emit('LONG_SPLIT_TP_UNAVAILABLE', pair=trade['pair'], trade_id=ident, reason=str(error), fallback='Full position at +7%')
            return False
        s = {**s, 'phase': 'resting', 'tp1_quantity': str(part), 'native_cancel': None, 'attach': {}}
        intent = dict(status='submitting', quantity=str(part), limit_price=str(tp1), submitted_at=time.time(), cycle=s['cycle'])
        items = list(self.db.get(ident)['data'].get('long_tp_orders', [])) + [intent]
        self.db.update(ident, 'PROTECTING', long_tp=s, long_tp_orders=items)
        try:
            oid = self.ex.partial_long_limit(trade['pair'], part, tp1)
            self.update_order(trade, status='open', order_id=oid)
            self.db.update(ident, 'OPEN', tp=str(tp1), tp_pct=str(self.e.c.long_tp))
            self.emit('LONG_PARTIAL_TP_LIMIT_SUBMITTED', pair=trade['pair'], trade_id=ident, quantity=str(part),
                      limit_price=str(tp1), remaining_quantity=str(qty - part), remaining_target=str(tp2), order_id=oid)
        except Exception as error:
            rejected = isinstance(error, ExchangeError) and error.rejected
            self.update_order(trade, status='rejected' if rejected else 'uncertain', filled_quantity='0')
            self.db.update(ident, 'PROTECTING' if rejected else 'UNCERTAIN')
            self.emit('LONG_TP_UNCERTAIN', pair=trade['pair'], trade_id=ident, error_type=type(error).__name__,
                      reason=str(error) if isinstance(error, ExchangeError) else '')
        return True
