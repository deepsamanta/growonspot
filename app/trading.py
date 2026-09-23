import time
from decimal import Decimal
from .signals import geometry, Signal
from .exchange import D, APIError, EntryNotSubmitted, OrderRejected

class Trader:
    def __init__(self, config, db, exchange):
        self.c, self.db, self.ex = config, db, exchange

    def enter(self, signal_id, message_id, signal):
        self.db.enqueue(signal_id, message_id, signal.data())
        self.reconcile()

    def process_pending(self):
        pending = self.db.pending()
        if not pending:
            return
        signal_id, message_id = pending['signal_id'], pending['message_id']
        if time.time() - pending['created_at'] > getattr(self.c, 'max_age', 1200):
            self.db.finish_pending(signal_id)
            self.db.event('PENDING_SIGNAL_EXPIRED', message_id=message_id)
            return
        if not self.c.enabled:
            self.db.finish_pending(signal_id)
            self.db.event('TRADING_DISABLED')
            return
        trade = self.db.active()
        if trade and (trade['status'] != 'OPEN' or trade['data'].get('exit_reason')):
            return
        count, loss = self.db.daily()
        if count >= self.c.daily_trades or D(loss) >= self.c.daily_loss:
            self.db.finish_pending(signal_id)
            self.db.event('DAILY_LIMIT_REACHED')
            return
        data = pending['data']
        signal = Signal(data['symbol'], data['side'], D(data['entry']), D(data['sl']), D(data['tp']), data.get('ticket_id'))
        try:
            price = self.ex.price(signal.side)
            qty = self.ex.size(price)
            sl, tp = self.ex.tick(signal.sl), self.ex.tick(signal.tp)
            if not geometry(signal.side, price, sl, tp):
                raise ValueError('SL_TP_INVALID_AT_CURRENT_MARKET')
        except ValueError as error:
            self.db.finish_pending(signal_id)
            self.db.event('SIGNAL_REJECTED', message_id=message_id, reason=str(error))
            return
        if trade:
            if trade['data']['side'] != signal.side:
                self.db.event('REVERSAL_REQUESTED', trade_id=trade['id'], message_id=message_id, side=signal.side)
                self.close('SIGNAL_REVERSAL')
                # Confirm flat before placing the opposite order; delayed exits are
                # resumed from the durable queue by the next polling cycle.
                self._reconcile()
                if not self.db.active():
                    self.process_pending()
                return
            self.add(trade, signal_id, message_id, signal, price, qty, sl, tp)
            return
        self.open_new(signal_id, message_id, signal, price, qty, sl, tp)

    def open_new(self, signal_id, message_id, signal, price, qty, sl, tp):
        positions = self.ex.positions()
        if any(D(p['active_pos']) != 0 or D(p.get('inactive_pos_buy', 0)) != 0 or D(p.get('inactive_pos_sell', 0)) != 0 for p in positions):
            self.db.finish_pending(signal_id)
            self.db.event('EXTERNAL_POSITION_BLOCKS_ENTRY')
            return
        if self.ex.orders('BUY') or self.ex.orders('SELL'):
            self.db.finish_pending(signal_id)
            self.db.event('EXTERNAL_ORDER_BLOCKS_ENTRY')
            return
        data = {**signal.data(), 'message_id': message_id, 'pair': self.ex.pair,
                'quantity': str(qty), 'sl': str(sl), 'tp': str(tp),
                'leverage': self.c.leverage, 'margin': str(self.c.margin), 'dry': self.c.dry,
                'submitted_at': time.time(), 'market_price': str(price)}
        self.db.reserve(signal_id, data)  # Durable intent BEFORE first mutation; never resubmit an uncertain entry.
        trade = self.db.active()
        if self.c.dry:
            self.db.update(trade, 'OPEN', actual_fill_price=str(price))
            self.db.event('WOULD_OPEN', **data)
            return
        try:
            order_id = self.ex.create(signal, qty, sl, tp)
            self.db.update(trade, 'SUBMITTED', exchange_order_id=order_id)
            self.db.event('ORDER_SUBMITTED', trade_id=trade['id'], order_id=order_id)
        except (EntryNotSubmitted, OrderRejected) as error:
            self.db.update(trade, 'REJECTED', failure=error.details())
            self.db.event('ENTRY_REJECTED' if isinstance(error, OrderRejected) else 'ENTRY_NOT_SUBMITTED', trade_id=trade['id'], **error.details())
        except Exception as error:
            self.db.event('ENTRY_SUBMISSION_FAILED', trade_id=trade['id'],
                          **(error.details() if isinstance(error, APIError) else {'error_type': type(error).__name__}))
            self.db.update(trade, 'UNCERTAIN')
        self._reconcile()

    def add(self, trade, signal_id, message_id, signal, price, qty, sl, tp):
        positions = [p for p in self.ex.positions() if D(p['active_pos']) != 0]
        if not self.c.dry and (len(positions) != 1 or not self.ownership(trade, positions[0])):
            self.db.update(trade, 'ISOLATION_CONFLICT')
            return
        # Only the bot's current full-position SL/TP orders may coexist with an add.
        # Unrelated working orders must never be silently adopted or cancelled.
        for side in ('BUY', 'SELL'):
            for order in self.ex.orders(side):
                expected_side = 'sell' if signal.side == 'BUY' else 'buy'
                expected = trade['data']['sl'] if order.get('order_type') == 'stop_market' else trade['data']['tp']
                if not (order.get('stage') == 'tpsl_exit' and order.get('status') == 'untriggered'
                        and order.get('side') == expected_side
                        and order.get('order_type') in ('stop_market', 'take_profit_market')
                        and D(order.get('stop_price') or 0) == D(expected)):
                    self.db.finish_pending(signal_id)
                    self.db.event('EXTERNAL_ORDER_BLOCKS_ENTRY', message_id=message_id)
                    return
        data = {**signal.data(), 'message_id': message_id, 'quantity': str(qty),
                'sl': str(sl), 'tp': str(tp), 'margin': str(self.c.margin),
                'submitted_at': time.time(), 'market_price': str(price)}
        self.db.reserve_addition(trade, signal_id, data)
        addition = self.db.pending_addition(trade)
        if self.c.dry:
            self.finish_addition(trade, addition, qty, price)
            self.db.update(self.db.active(), 'OPEN')
        else:
            try:
                order_id = self.ex.create(signal, qty, sl, tp)
                self.db.update_addition(addition, 'SUBMITTED', exchange_order_id=order_id)
                self.db.event('ADD_ORDER_SUBMITTED', trade_id=trade['id'], message_id=message_id, order_id=order_id)
            except (EntryNotSubmitted, OrderRejected) as error:
                self.db.update_addition(addition, 'REJECTED', failure=error.details())
                self.db.update(self.db.active(), 'OPEN')
                self.db.event('ENTRY_REJECTED' if isinstance(error, OrderRejected) else 'ENTRY_NOT_SUBMITTED', trade_id=trade['id'], message_id=message_id, **error.details())
            except Exception as error:
                self.db.event('ENTRY_SUBMISSION_FAILED', trade_id=trade['id'], message_id=message_id,
                              **(error.details() if isinstance(error, APIError) else {'error_type': type(error).__name__}))
                self.db.update_addition(addition, 'UNCERTAIN')
                self.db.update(self.db.active(), 'UNCERTAIN')
        self._reconcile()

    def finish_addition(self, trade, addition, filled, fill_price):
        d, a = trade['data'], addition['data']
        quantity = D(d['quantity']) + filled
        self.db.apply_addition(trade, addition,
            {**d, 'quantity': str(quantity), 'margin': str(D(d['margin']) + D(a['margin'])),
             'sl': a['sl'], 'tp': a['tp'], 'open_alerted': False},
            {'filled_quantity': str(filled), 'actual_fill_price': str(fill_price)})
        self.db.event('POSITION_ADDED', trade_id=trade['id'], message_id=a['message_id'],
                      side=d['side'], quantity=str(filled), total_quantity=str(quantity),
                      actual_fill_price=str(fill_price), sl=a['sl'], tp=a['tp'], margin=a['margin'])

    def reconcile_addition(self, trade, addition, positions):
        a = addition['data']
        if trade['status'] == 'ISOLATION_CONFLICT':
            return False
        if not a.get('exchange_order_id'):
            self.db.update(trade, 'UNCERTAIN')
            return False  # Never infer acknowledgement from an existing net position.
        order = self.ex.find_order(trade['data']['side'], a['exchange_order_id'])
        if order is None:
            self.db.update(trade, 'UNCERTAIN')
            return False
        if order['status'] not in ('filled', 'cancelled', 'partially_cancelled', 'rejected'):
            if not a.get('cancel_requested'):
                self.db.update_addition(addition, 'SUBMITTED', cancel_requested=True)
                self.ex.cancel(order['id'])
            if time.time() - a['submitted_at'] > 60:
                self.db.update(trade, 'UNCERTAIN')
            return False
        filled = D(order['total_quantity']) - D(order['remaining_quantity']) - D(order.get('cancelled_quantity', 0))
        if filled == 0:
            self.db.update_addition(addition, 'REJECTED')
            self.db.update(trade, 'PROTECTING')
            self.db.event('ADD_ORDER_REJECTED', trade_id=trade['id'], message_id=a['message_id'])
            return True
        expected = (D(trade['data']['quantity']) + filled) * (1 if trade['data']['side'] == 'BUY' else -1)
        # Order and position endpoints can briefly disagree during fill settlement.
        # Re-read after the terminal order; never permanently flag a normal lag.
        positions[:] = [p for p in self.ex.positions() if D(p['active_pos']) != 0]
        if (0 < filled <= D(a['quantity']) and len(positions) == 1
                and positions[0]['id'] == trade['data']['position_id']):
            actual = D(positions[0]['active_pos']) * (1 if trade['data']['side'] == 'BUY' else -1)
            if D(trade['data']['quantity']) <= actual < abs(expected):
                if time.time() - a['submitted_at'] > 60:
                    self.db.update(trade, 'UNCERTAIN')
                return False
        if (filled < 0 or filled > D(a['quantity']) or len(positions) != 1
                or positions[0]['id'] != trade['data']['position_id'] or D(positions[0]['active_pos']) != expected):
            self.db.update(trade, 'ISOLATION_CONFLICT')
            return False
        self.finish_addition(trade, addition, filled, order['avg_price'])
        return True

    def ownership(self, trade, position):
        d = trade['data']
        signed = D(position['active_pos'])
        expected = D(d['quantity']) * (1 if d['side'] == 'BUY' else -1)
        return signed == expected and (not d.get('position_id') or d['position_id'] == position['id'])

    def reconcile(self):
        self._reconcile()
        self.process_pending()

    def reconcile_flat_entry(self, trade):
        """A missing position alone never proves an unacknowledged entry failed."""
        d = trade['data']
        if d.get('exchange_order_id'):
            order = self.ex.find_order(d['side'], d['exchange_order_id'])
            if order and order['status'] in ('filled', 'cancelled', 'partially_cancelled', 'rejected'):
                filled = (D(order['total_quantity']) - D(order['remaining_quantity'])
                          - D(order.get('cancelled_quantity', 0)))
                if order['status'] == 'rejected' or filled == 0:
                    self.db.update(trade, 'REJECTED', exit_reason='ENTRY_TERMINAL_NO_FILL')
                    self.db.event('ENTRY_NOT_FILLED', trade_id=trade['id'], order_id=order['id'])
                    return
                if 0 < filled <= D(d['quantity']) and not self.ex.orders('BUY') and not self.ex.orders('SELL'):
                    # Recheck flat after order history; never close a DB record while
                    # an exchange position or working entry can still remain.
                    positions = self.ex.positions()
                    if not any(D(p.get(k) or 0) != 0 for p in positions
                               for k in ('active_pos', 'inactive_pos_buy', 'inactive_pos_sell')):
                        self.db.close(trade, 'EXCHANGE_CLOSED_BEFORE_RECOVERY', D(d['margin']))
                        return
        if trade['status'] != 'UNCERTAIN':
            self.db.update(trade, 'UNCERTAIN')
            self.db.event('ORDER_UNCERTAIN_REQUIRES_REVIEW', trade_id=trade['id'])

    def _reconcile(self):
        trade = self.db.active()
        if not trade:
            self.ex.positions()  # API heartbeat even while idle.
            return
        d = trade['data']
        if d['dry']:
            price = self.ex.price('SELL' if d['side'] == 'BUY' else 'BUY')
            if not geometry(d['side'], price, D(d['sl']), D(d['tp'])):
                self.db.close(trade, 'DRY_SL_OR_TP', D(d['margin']))
            return
        positions = [p for p in self.ex.positions() if D(p['active_pos']) != 0]
        addition = self.db.pending_addition(trade)
        if addition:
            if not self.reconcile_addition(trade, addition, positions):
                return
            trade = self.db.active()
            d = trade['data']
        if len(positions) > 1:
            self.db.update(trade, 'ISOLATION_CONFLICT')
            return
        if not positions:
            if trade['status'] in ('OPEN', 'PROTECTING', 'CLOSING'):
                # Exchange positions are netted; do not invent a fill price or realized PnL.
                self.db.close(trade, d.get('exit_reason', 'EXCHANGE_CLOSED'), D(d['margin']))
            elif time.time() - d['submitted_at'] > 60:
                self.reconcile_flat_entry(trade)
            return
        p = positions[0]
        if not d.get('position_id'):
            # Confirm entry ownership from its exchange order before adopting a net position.
            if d.get('exchange_order_id'):
                order = self.ex.find_order(d['side'], d['exchange_order_id'])
                candidates = [order] if order is not None else []
            else:
                orders = self.ex.orders(d['side'], 'filled,partially_filled,partially_cancelled,cancelled,open,rejected')
                candidates = [o for o in orders if D(o['total_quantity']) == D(d['quantity']) and
                              float(o['created_at']) / 1000 >= d['submitted_at'] - 2]
            if len(candidates) != 1:
                if trade['status'] != 'UNCERTAIN':
                    self.db.update(trade, 'UNCERTAIN')
                    self.db.event('ORDER_UNCERTAIN_REQUIRES_REVIEW', trade_id=trade['id'])
                return
            order = candidates[0]
            d['exchange_order_id'] = order['id']
            if order['status'] not in ('filled', 'cancelled', 'partially_cancelled', 'rejected'):
                # Cancel a still-working market remainder once; reconcile the final filled amount.
                if not d.get('cancel_requested'):
                    self.db.update(trade, 'SUBMITTED', **{**d, 'cancel_requested': True})
                    self.ex.cancel(order['id'])
                return
            filled = D(order['total_quantity']) - D(order['remaining_quantity']) - D(order.get('cancelled_quantity', 0))
            signed = D(p['active_pos']) * (1 if d['side'] == 'BUY' else -1)
            if filled <= 0 or filled != signed or filled > D(d['quantity']):
                self.db.update(trade, 'ISOLATION_CONFLICT')
                return
            d['requested_quantity'] = d['quantity']
            d['quantity'] = str(filled)
            self.db.update(trade, trade['status'], **d)
            trade = self.db.active()
        if not self.ownership(trade, p):
            self.db.update(trade, 'ISOLATION_CONFLICT')
            return
        if trade['status'] == 'ISOLATION_CONFLICT':
            return  # Manual review required after any unexpected account modification.
        if trade['status'] == 'CLOSING':
            if time.time() - d.get('close_requested_at', 0) > 30 and not d.get('exit_unconfirmed_alerted'):
                self.db.update(trade, 'CLOSING', exit_unconfirmed_alerted=True)
                self.db.event('EXIT_UNCONFIRMED', trade_id=trade['id'])
            return  # Never repeat an uncertain exit and risk affecting a subsequent position.
        self.db.update(trade, 'PROTECTING', **{**d, 'position_id': p['id'], 'actual_fill_price': str(p['avg_price'])})
        trade = self.db.active()
        d = trade['data']
        if d.get('exit_reason'):
            self.close(d['exit_reason'])
            return
        sl, tp = D(d['sl']), D(d['tp'])
        if D(p.get('stop_loss_trigger') or 0) != sl or D(p.get('take_profit_trigger') or 0) != tp:
            try:
                self.ex.protect(p, sl, tp)
                refreshed = next((x for x in self.ex.positions() if x['id'] == p['id']), None)
                if refreshed is None or D(refreshed['active_pos']) == 0:
                    return
                if D(refreshed.get('stop_loss_trigger') or 0) != sl or D(refreshed.get('take_profit_trigger') or 0) != tp:
                    raise ValueError('PROTECTION_NOT_CONFIRMED')
            except Exception:
                self.db.event('PROTECTION_FAILED_EMERGENCY_EXIT', trade_id=trade['id'])
                self.close('ERROR_PROTECTION')
                return
        self.db.update(trade, 'OPEN', open_alerted=True)
        if not d.get('open_alerted'):
            self.db.event('POSITION_OPEN', trade_id=trade['id'], message_id=d['message_id'],
                          side=d['side'], quantity=d['quantity'], actual_fill_price=d['actual_fill_price'],
                          sl=d['sl'], tp=d['tp'], margin=d['margin'], leverage=d.get('leverage'))

    def close(self, reason, reply_to=None):
        trade = self.db.active()
        if reply_to is not None and (not trade or reply_to not in self.db.message_ids(trade)):
            return
        if reason != 'SIGNAL_REVERSAL':
            self.db.cancel_pending()
        if not trade:
            return
        d = trade['data']
        self.db.update(trade, trade['status'], exit_reason=reason)
        trade = self.db.active()
        if trade['status'] in ('CLOSING', 'ISOLATION_CONFLICT'):
            return
        if self.db.pending_addition(trade):
            return  # Reconcile the added fill before issuing one full-position exit.
        if d['dry']:
            self.db.close(trade, reason, D(d['margin']))
            return
        positions = [p for p in self.ex.positions() if D(p['active_pos']) != 0]
        if not positions or not d.get('position_id'):
            return  # Persisted close intent is applied after pending entry recovery.
        if len(positions) != 1 or not self.ownership(trade, positions[0]):
            self.db.update(self.db.active(), 'ISOLATION_CONFLICT')
            return
        trade = self.db.active()
        self.db.update(trade, 'CLOSING', close_requested_at=time.time())
        try:
            self.ex.exit(d['position_id'])
        except Exception:
            self.db.event('EXIT_REQUEST_FAILED', trade_id=trade['id'], reason=reason)
            raise
        self.db.event('POSITION_CLOSE_REQUESTED', trade_id=trade['id'], reason=reason)
