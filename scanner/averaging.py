"""One durable short addition per position, including manual same-side orders."""
import time
from decimal import Decimal
from .exchange import ExchangeError, nongold, short_limit_price, target, quantity
from .market import decimal
from .strategy import History, weekly_resistances

D=Decimal
TRIGGER=D('.30')
TERMINAL={'filled','cancelled','partially_cancelled','rejected'}
RECOVERABLE={'POSITION_QUANTITY_OR_ID_CHANGED','AVERAGE_POSITION_MISMATCH',
             'MANUAL_ADDITION_UNVERIFIED','POSITION_CHANGED_DURING_TP_UPDATE','SHORT_FILL_LEDGER_MISMATCH',
             'PARTIAL_TP_INCOMPLETE_OR_POSITION_CHANGED'}


def filled_quantity(order):
    if order['status']=='rejected':return D(0)
    qty=decimal(order['total_quantity'])-decimal(order['remaining_quantity'])-decimal(order.get('cancelled_quantity') or 0)
    if not 0<=qty<=decimal(order['total_quantity']):raise ValueError('INVALID_ADDITION_FILL')
    return qty


def validate_quantity(info,qty,price,current_qty,leverage,minimum_price):
    """Validate the exact first-fill quantity, including the combined leverage tier."""
    step=decimal(info['quantity_increment'],True)
    minimum=max(decimal(info['min_quantity'],True),decimal(info.get('min_trade_size',info['min_quantity']),True))
    if qty%step or qty<minimum or qty*minimum_price<decimal(info['min_notional'],True):
        raise ValueError('AVERAGE_QUANTITY_BELOW_EXCHANGE_MINIMUM')
    if qty>decimal(info['max_quantity'],True):raise ValueError('AVERAGE_QUANTITY_ABOVE_EXCHANGE_MAXIMUM')
    supported=[decimal(k,True) for k,v in info.get('dynamic_position_leverage_details',{}).items()
               if decimal(v,True)>=(current_qty+qty)*price]
    if not supported or leverage>max(supported):raise ValueError('AVERAGE_COMBINED_LEVERAGE_NOT_SUPPORTED')


class ShortAverager:
    def __init__(self,engine):
        self.e=engine
        self.db,self.ex,self.c,self.market,self.emit=engine.db,engine.ex,engine.c,engine.market,engine.emit
        self.history=History(self.market,self.db)
        self.checked={}

    def save_order(self,trade,**changes):
        row=self.db.get(trade['id'])
        order={**row['data'].get('average_order',{}),**changes}
        if changes.get('status') in TERMINAL:order['cancel_unconfirmed']=False
        self.db.update(row['id'],row['status'],average_order=order)
        return order

    def manual(self,trade,ids,reason):
        row=self.db.get(trade['id']);d=row['data']
        observed=sorted(set(d.get('manual_order_ids',[]))|set(ids))
        self.db.update(row['id'],row['status'],averaging_used=True,manual_average_seen=True,
                       averaging_source=d.get('averaging_source') or 'MANUAL',manual_order_ids=observed)
        if not d.get('manual_average_seen'):
            self.emit('MANUAL_AVERAGE_DETECTED',pair=row['pair'],trade_id=row['id'],reason=reason)

    def cancel_own(self,trade,reason,order=None):
        """Never cancel a manual order. Unknown acknowledgements remain occupied."""
        row=self.db.get(trade['id']);a=row['data'].get('average_order')
        if not a or a.get('status') in TERMINAL:return True
        if not a.get('order_id'):
            self.db.update(row['id'],'UNCERTAIN')
            return False
        order=order or self.ex.find_order(row['pair'],'SELL',a['order_id'])
        if order is None:return False
        if order['status'] in TERMINAL:
            self.save_order(row,status=order['status'],filled_quantity=str(filled_quantity(order)),avg_price=str(order.get('avg_price') or 0))
            return True
        now=time.time();attempts=a.get('cancel_attempts',0)
        if attempts>=3:
            if not a.get('cancel_unconfirmed'):
                self.save_order(row,cancel_unconfirmed=True)
                self.emit('AVERAGE_UNCERTAIN',pair=row['pair'],trade_id=row['id'],reason='CANCEL_NOT_CONFIRMED')
            return False
        if attempts and now-a.get('cancel_requested_at',0)<30:return False
        self.save_order(row,cancel_attempts=attempts+1,cancel_requested_at=now,cancel_reason=reason)
        self.ex.cancel(row['pair'],order)
        self.emit('AVERAGE_CANCEL_REQUESTED',pair=row['pair'],trade_id=row['id'],reason=reason)
        return False  # Confirm on a later read, including a possible fill during cancellation.

    def sync(self,trade,positions,initial_order,initial_qty):
        """Attribute increases before changing TP; manual activity consumes allowance."""
        ident,pair,d=trade['id'],trade['pair'],trade['data']
        initial_fill=decimal(initial_order.get('avg_price') or d['fill'],True)
        self.db.update(ident,trade['status'],initial_quantity=str(initial_qty),initial_fill=str(initial_fill))
        own=d.get('average_order',{})
        own_id=own.get('order_id')
        bot_ids={d['order_id'],own_id,*[a.get('order_id') for a in d.get('average_history',[])]}
        pending=[o for o in self.ex.orders(pair,'SELL') if o.get('id') not in bot_ids
                 and o.get('stage')=='default' and o.get('side')=='sell']
        recent=self.ex.recent_entries(pair,'SELL',d['submitted_at']-2)
        external={o['id']:o for o in recent if o['id'] not in bot_ids and filled_quantity(o)>0}
        baseline=d.get('manual_cycle_baseline',{})
        additions=[oid for oid,o in external.items() if filled_quantity(o)>decimal(baseline.get(oid) or 0)]
        if pending or additions:
            self.manual(trade,[o['id'] for o in pending]+additions,'MANUAL_ENTRY_ORDER')
        # An unexplained increase also blocks an addition while order/ledger APIs catch up.
        previously_known=decimal(d.get('filled_quantity') or initial_qty)
        if (len(positions)==1 and positions[0]['id']==d['position_id']
                and -decimal(positions[0]['active_pos'])>previously_known
                and not own):
            self.manual(trade,[],'POSITION_INCREASE')
        trade=self.db.get(ident);d=trade['data']
        own_order=None;own_qty=D(0);own_fill=D(0)
        if own:
            if not own_id:
                if own.get('status') not in TERMINAL:
                    self.db.update(ident,'UNCERTAIN')
                    self.emit('AVERAGE_UNCERTAIN',pair=pair,trade_id=ident,reason='MISSING_ORDER_ACKNOWLEDGEMENT')
                    return None
            else:
                own_order=self.ex.find_order(pair,'SELL',own_id)
                if own_order is None:
                    self.db.update(ident,'UNCERTAIN');return None
                own_qty=filled_quantity(own_order)
                if (decimal(own_order['total_quantity'])!=decimal(own['quantity']) or own_qty>decimal(own['quantity'])):
                    self.e.conflict(trade,'UNEXPECTED_AVERAGE_FILL');return None
                own_fill=decimal(own_order.get('avg_price') or 0)
                self.save_order(trade,status=own_order['status'],filled_quantity=str(own_qty),avg_price=str(own_fill))
                if own_order['status'] not in TERMINAL:
                    reason=None
                    if d.get('manual_average_seen'):reason='MANUAL_AVERAGE_DETECTED'
                    elif not positions:reason='BASE_POSITION_CLOSED'
                    elif (len(positions)!=1 or positions[0]['id']!=d['position_id']
                          or decimal(positions[0]['active_pos'])>=0):reason='BASE_POSITION_CHANGED'
                    elif own.get('cancel_reason'):reason=own['cancel_reason']
                    elif own_qty>0:reason='PARTIAL_FILL_REMAINDER'
                    elif time.time()>=own['expires_at']:reason='LIMIT_EXPIRED'
                    elif not self.e.capacity().within_limits:reason='ACCOUNT_CAPACITY_CHANGED'
                    if reason:self.cancel_own(trade,reason,own_order)
                    if not positions:return None
        # Bind each external filled order to this exact position episode in the ledger.
        known=dict(d.get('manual_entries',{}))
        unknown=set(external)-set(known)
        if unknown:
            ledger=self.ex.transactions(d['submitted_at']-2)
            linked={r['parent_id'] for r in ledger if r.get('pair')==pair and r.get('position_id')==d['position_id']
                    and r.get('stage')=='default' and 'Order' in r.get('parent_type','')}
            if not unknown<=linked:
                self.e.conflict(trade,'MANUAL_ADDITION_UNVERIFIED');return None
        for oid,o in external.items():
            known[oid]={'quantity':str(filled_quantity(o)),'fill':str(decimal(o.get('avg_price'),True))}
        self.db.update(ident,self.db.get(ident)['status'],manual_entries=known)
        qty=initial_qty+own_qty+sum((decimal(v['quantity']) for v in known.values()),D(0))
        cost=initial_qty*initial_fill+own_qty*own_fill+sum((decimal(v['quantity'])*decimal(v['fill']) for v in known.values()),D(0))
        expected_fill=cost/qty
        if positions and d.get('tp1_orders'):
            from .short_tp import replay_short
            try:
                qty,expected_fill=replay_short(self.ex.trade_fills(pair,d['submitted_at']-2),
                    bot_ids|set(known),{o.get('order_id') for o in d['tp1_orders']})
                if qty<=0:raise ValueError('SHORT_ALREADY_CLOSED')
            except ValueError as error:
                self.cancel_own(trade,'POSITION_CHANGED')
                self.e.conflict(trade,'SHORT_FILL_LEDGER_MISMATCH');return None
        if positions:
            p=positions[0]
            tolerance=max(decimal(d['info']['price_increment'],True),expected_fill*D('.00000001'))
            if (len(positions)!=1 or p['id']!=d['position_id'] or decimal(p['active_pos'])!=-qty
                    or abs(decimal(p['avg_price'],True)-expected_fill)>tolerance):
                if -decimal(p['active_pos'])>qty:self.manual(trade,[],'UNATTRIBUTED_POSITION_INCREASE')
                self.cancel_own(trade,'POSITION_CHANGED')
                self.e.conflict(trade,'AVERAGE_POSITION_MISMATCH');return None
            if p.get('margin_type')!=d['mode'] or decimal(p.get('leverage') or 0)!=d['leverage']:
                self.cancel_own(trade,'POSITION_SETTINGS_CHANGED')
                self.e.conflict(trade,'MARGIN_MODE_OR_LEVERAGE_MISMATCH');return None
            expected_fill=decimal(p['avg_price'],True)
        if own_qty>decimal(own.get('announced_quantity') or 0):
            self.save_order(trade,announced_quantity=str(own_qty))
            self.emit('AVERAGE_FILLED',pair=pair,trade_id=ident,added_quantity=str(own_qty),
                      combined_quantity=str(qty),average_entry=str(expected_fill))
        return qty,expected_fill

    def maybe_place(self,trade):
        d=trade['data'];pair=trade['pair'];now=time.time()
        if (not self.c.enabled or trade['status']!='OPEN' or d['side']!='SELL'
                or d.get('averaging_used') or d.get('order_type')!='limit_order'):return
        nongold(pair)
        if not self.e.capacity().within_limits:return
        price=self.ex.price(pair,'SELL')
        initial_fill=decimal(d.get('average_reference') or d['initial_fill'],True)
        qty=decimal(d['initial_quantity'],True);current_qty=decimal(d['filled_quantity'],True)
        if price<=initial_fill*(1+TRIGGER):return
        # Resistance history is cached daily; reevaluate a missing level at most once a minute.
        if now-self.checked.get(trade['id'],0)<60:return
        self.checked[trade['id']]=now
        bars=self.history.daily(pair,now)
        above=[r for r in weekly_resistances(bars,now) if r>price]
        if not above or price<above[0]*(1-self.c.short_distance):return
        resistance=above[0]
        info=self.market.metadata(pair)
        if not self.market.eligible_metadata(info):return
        if info.get('order_types') and 'limit_order' not in info['order_types']:return
        latest=self.market.quotes().get(pair)
        if latest is None:return
        price=self.ex.price(pair,'SELL')
        current=max(price,latest.price,latest.mark_price or latest.price)
        if (price<=initial_fill*(1+TRIGGER) or not resistance*(1-self.c.short_distance)<=current<resistance):return
        limit=short_limit_price(info,resistance,latest.price)
        try:
            minimum_price=min(price,latest.price,latest.mark_price or latest.price)
            if d.get('average_margin_mode'):
                qty=quantity(info,limit,self.c.short_margin,d['leverage'],minimum_price=minimum_price)
            validate_quantity(info,qty,limit,current_qty,d['leverage'],minimum_price)
        except ValueError as error:
            self.emit('AVERAGE_SKIPPED',pair=pair,trade_id=trade['id'],reason=str(error));return
        # Check manual orders and quantity again after potentially slow history/API reads.
        original=self.ex.find_order(pair,'SELL',d['order_id'])
        if original is None:return
        positions=[p for p in self.ex.positions(pair) if decimal(p.get('active_pos') or 0)!=0]
        original_qty=filled_quantity(original)
        result=self.sync(self.db.get(trade['id']),positions,original,original_qty)
        refreshed=self.db.get(trade['id'])
        if result is None or not positions or result[0]!=current_qty or refreshed['data'].get('averaging_used'):return
        if not self.e.capacity().within_limits:return
        final_positions=[p for p in self.ex.positions(pair) if decimal(p.get('active_pos') or 0)!=0]
        if (len(final_positions)!=1 or final_positions[0]['id']!=d['position_id']
                or decimal(final_positions[0]['active_pos'])!=-current_qty
                or decimal(final_positions[0]['avg_price'])!=result[1]
                or final_positions[0].get('margin_type')!=d['mode']
                or decimal(final_positions[0].get('leverage') or 0)!=d['leverage']):return
        bot_ids={d['order_id'],*[a.get('order_id') for a in d.get('average_history',[])]}
        manual_pending=[o for o in self.ex.orders(pair,'SELL') if o.get('id') not in bot_ids
                        and o.get('side')=='sell' and o.get('stage')=='default']
        if manual_pending:
            self.manual(trade,[o['id'] for o in manual_pending],'MANUAL_ENTRY_BEFORE_SUBMISSION');return
        intent={'status':'submitting','quantity':str(qty),'reference':str(resistance),'limit_price':str(limit),
                'submitted_at':time.time(),'expires_at':time.time()+self.c.short_limit_seconds,
                'estimated_margin':str(qty*limit/d['leverage'])}
        if not self.db.claim_average(trade['id'],intent):return
        try:
            oid=self.ex.create(pair,'SELL',qty,d['leverage'],d['mode'],target(info,'SELL',limit,self.c.short_tp),limit_price=limit)
            self.save_order(trade,status='open',order_id=oid)
            self.emit('AVERAGE_SUBMITTED',pair=pair,trade_id=trade['id'],order_id=oid,quantity=str(qty),
                      limit_price=str(limit),leverage=d['leverage'],mode=d['mode'],expires_in_seconds=self.c.short_limit_seconds)
        except Exception as error:
            rejected=isinstance(error,ExchangeError) and error.rejected
            self.save_order(trade,status='rejected' if rejected else 'uncertain')
            if not rejected:self.db.update(trade['id'],'UNCERTAIN')
            self.emit('AVERAGE_REJECTED' if rejected else 'AVERAGE_UNCERTAIN',pair=pair,trade_id=trade['id'],
                      error_type=type(error).__name__,reason=str(error) if isinstance(error,(ExchangeError,ValueError)) else '')
