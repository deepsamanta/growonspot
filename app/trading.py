import time
from decimal import Decimal
from .signals import geometry
from .exchange import D

class Trader:
    def __init__(self, config, db, exchange):
        self.c, self.db, self.ex = config, db, exchange

    def enter(self, signal_id, message_id, signal):
        if self.db.active():
            self.db.event('SIGNAL_IGNORED_ACTIVE_POSITION')
            return
        if not self.c.enabled:
            self.db.event('TRADING_DISABLED')
            return
        count, loss = self.db.daily()
        if count >= self.c.daily_trades or D(loss) >= self.c.daily_loss:
            self.db.event('DAILY_LIMIT_REACHED')
            return
        positions = self.ex.positions()
        if any(D(p['active_pos']) != 0 or D(p.get('inactive_pos_buy', 0)) != 0 or D(p.get('inactive_pos_sell', 0)) != 0 for p in positions):
            self.db.event('EXTERNAL_POSITION_BLOCKS_ENTRY')
            return
        if self.ex.orders('BUY') or self.ex.orders('SELL'):
            self.db.event('EXTERNAL_ORDER_BLOCKS_ENTRY')
            return
        price = self.ex.price(signal.side)
        if abs(price - signal.entry) / signal.entry * 100 > self.c.deviation:
            self.db.event('ENTRY_SKIPPED_PRICE_DEVIATION')
            return
        qty = self.ex.size(price)
        sl, tp = self.ex.tick(signal.sl), self.ex.tick(signal.tp)
        if not geometry(signal.side, price, sl, tp):
            raise ValueError('SL_TP_INVALID_AT_CURRENT_MARKET')
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
        except Exception:
            self.db.update(trade, 'UNCERTAIN')
            self.db.event('ORDER_UNCERTAIN', trade_id=trade['id'])
        self.reconcile()

    def ownership(self, trade, position):
        d = trade['data']
        signed = D(position['active_pos'])
        expected = D(d['quantity']) * (1 if d['side'] == 'BUY' else -1)
        return signed == expected and (not d.get('position_id') or d['position_id'] == position['id'])

    def reconcile(self):
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
        if len(positions) > 1:
            self.db.update(trade, 'ISOLATION_CONFLICT')
            return
        if not positions:
            if trade['status'] in ('OPEN', 'PROTECTING', 'CLOSING'):
                # Exchange positions are netted; do not invent a fill price or realized PnL.
                self.db.close(trade, d.get('exit_reason', 'EXCHANGE_CLOSED'), D(d['margin']))
            elif time.time() - d['submitted_at'] > 60:
                self.db.update(trade, 'UNCERTAIN')
                self.db.event('ORDER_UNCERTAIN_REQUIRES_REVIEW', trade_id=trade['id'])
            return
        p = positions[0]
        if not d.get('position_id'):
            # Confirm entry ownership from its exchange order before adopting a net position.
            orders = self.ex.orders(d['side'], 'filled,partially_filled,partially_cancelled,cancelled,open,rejected')
            candidates = [o for o in orders if (
                o['id'] == d['exchange_order_id'] if d.get('exchange_order_id') else
                D(o['total_quantity']) == D(d['quantity']) and
                float(o['created_at']) / 1000 >= d['submitted_at'] - 2)]
            if len(candidates) != 1:
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
            self.db.event('ISOLATION_CONFLICT', trade_id=trade['id'])
            return
        if trade['status'] == 'ISOLATION_CONFLICT':
            return  # Manual review required after any unexpected account modification.
        if trade['status'] == 'CLOSING':
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
        self.db.update(trade, 'OPEN')

    def close(self, reason, reply_to=None):
        trade = self.db.active()
        if not trade or (reply_to is not None and reply_to != trade['data']['message_id']):
            return
        d = trade['data']
        if trade['status'] in ('CLOSING', 'ISOLATION_CONFLICT'):
            return
        if d['dry']:
            self.db.close(trade, reason, D(d['margin']))
            return
        self.db.update(trade, trade['status'], exit_reason=reason)
        positions = [p for p in self.ex.positions() if D(p['active_pos']) != 0]
        if not positions or not d.get('position_id'):
            return  # Persisted close intent is applied after pending entry recovery.
        if len(positions) != 1 or not self.ownership(trade, positions[0]):
            self.db.update(self.db.active(), 'ISOLATION_CONFLICT')
            return
        trade = self.db.active()
        self.db.update(trade, 'CLOSING', close_requested_at=time.time())
        self.ex.exit(d['position_id'])
        self.db.event('POSITION_CLOSE_REQUESTED', trade_id=trade['id'], reason=reason)
