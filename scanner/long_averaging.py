"""One $6 long addition after a >60% drop and completed 4h recovery confirmation."""
import time
from decimal import Decimal as D
from .averaging import TERMINAL,filled_quantity,validate_quantity
from .exchange import ExchangeError,nongold,quantity
from .market import decimal
from .strategy import long_confirmation

DROP=D('.60')
RECOVERABLE={'POSITION_QUANTITY_OR_ID_CHANGED','POSITION_CHANGED_DURING_TP_UPDATE',
             'LONG_AVERAGE_POSITION_MISMATCH','LONG_MANUAL_ADDITION_UNVERIFIED'}


class LongAverager:
    def __init__(self,engine):
        self.e=engine
        self.db,self.ex,self.c,self.market,self.emit=engine.db,engine.ex,engine.c,engine.market,engine.emit
        self.checked={}

    def save_order(self,trade,**changes):
        row=self.db.get(trade['id']);a={**row['data'].get('average_order',{}),**changes}
        if changes.get('status') in TERMINAL:a['cancel_unconfirmed']=False
        self.db.update(row['id'],row['status'],average_order=a)
        return a

    def manual(self,trade,ids,reason):
        row=self.db.get(trade['id']);d=row['data']
        self.db.update(row['id'],row['status'],averaging_used=True,manual_average_seen=True,
                       averaging_source=d.get('averaging_source') or 'MANUAL',
                       manual_order_ids=sorted(set(d.get('manual_order_ids',[]))|set(ids)))
        if not d.get('manual_average_seen'):
            self.emit('MANUAL_AVERAGE_DETECTED',pair=row['pair'],trade_id=row['id'],side='BUY',reason=reason)

    def cancel_own(self,trade,reason,order=None):
        a=self.db.get(trade['id'])['data'].get('average_order',{})
        if not a or a.get('status') in TERMINAL:return True
        if not a.get('order_id'):
            self.db.update(trade['id'],'UNCERTAIN');return False
        o=order or self.ex.find_order(trade['pair'],'BUY',a['order_id'])
        if o is None:
            self.db.update(trade['id'],'UNCERTAIN');return False
        self.save_order(trade,status=o['status'],filled_quantity=str(filled_quantity(o)),avg_price=str(o.get('avg_price') or 0))
        if o['status'] in TERMINAL:return True
        attempts=a.get('cancel_attempts',0)
        if attempts>=3:
            self.save_order(trade,cancel_unconfirmed=True)
            self.db.update(trade['id'],'UNCERTAIN')
            self.emit('LONG_AVERAGE_UNCERTAIN',pair=trade['pair'],trade_id=trade['id'],reason='CANCEL_NOT_CONFIRMED')
            return False
        if not attempts or time.time()-a.get('cancel_requested_at',0)>=30:
            self.save_order(trade,cancel_attempts=attempts+1,cancel_requested_at=time.time(),cancel_reason=reason)
            self.ex.cancel(trade['pair'],o)
            self.emit('LONG_AVERAGE_CANCEL_REQUESTED',pair=trade['pair'],trade_id=trade['id'],reason=reason)
        return False

    def sync(self,trade,positions,initial_order,initial_qty):
        ident,pair,d=trade['id'],trade['pair'],trade['data']
        initial_fill=decimal(initial_order.get('avg_price') or d['initial_fill'],True)
        self.db.update(ident,trade['status'],initial_quantity=str(initial_qty),initial_fill=str(initial_fill))
        own=d.get('average_order',{});own_id=own.get('order_id');bot_ids={d['order_id'],own_id}
        pending=[o for o in self.ex.orders(pair,'BUY') if o.get('id') not in bot_ids
                 and o.get('stage')=='default' and o.get('side')=='buy']
        external={o['id']:o for o in self.ex.recent_entries(pair,'BUY',d['submitted_at']-2)
                  if o['id'] not in bot_ids and filled_quantity(o)>0}
        if pending or external:self.manual(trade,[o['id'] for o in pending]+list(external),'MANUAL_ENTRY_ORDER')
        known_qty=decimal(d.get('filled_quantity') or initial_qty)
        if (len(positions)==1 and positions[0]['id']==d['position_id']
                and decimal(positions[0]['active_pos'])>known_qty and not own):
            self.manual(trade,[],'POSITION_INCREASE')
        d=self.db.get(ident)['data'];own_qty=D(0);own_fill=D(0)
        if own:
            if not own_id:
                if own.get('status') not in TERMINAL:
                    self.db.update(ident,'UNCERTAIN')
                    self.emit('LONG_AVERAGE_UNCERTAIN',pair=pair,trade_id=ident,reason='MISSING_ORDER_ACKNOWLEDGEMENT')
                    return None
            else:
                o=self.ex.find_order(pair,'BUY',own_id)
                if o is None:
                    self.db.update(ident,'UNCERTAIN');return None
                own_qty=filled_quantity(o);own_fill=decimal(o.get('avg_price') or 0)
                if decimal(o['total_quantity'])!=decimal(own['quantity']):
                    self.cancel_own(trade,'ORDER_QUANTITY_CHANGED',o)
                    self.e.conflict(trade,'LONG_AVERAGE_ORDER_CHANGED');return None
                if own_qty>0 and own_fill<=0:
                    self.db.update(ident,'UNCERTAIN');return None
                self.save_order(trade,status=o['status'],filled_quantity=str(own_qty),avg_price=str(own_fill))
                if o['status'] not in TERMINAL:
                    reason=own.get('cancel_reason')
                    if not positions:reason='BASE_POSITION_CLOSED'
                    elif len(positions)!=1 or positions[0]['id']!=d['position_id'] or decimal(positions[0]['active_pos'])<=0:
                        reason='BASE_POSITION_CHANGED'
                    elif d.get('manual_average_seen'):reason='MANUAL_AVERAGE_DETECTED'
                    elif own_qty>0:reason='PARTIAL_MARKET_FILL_REMAINDER'
                    elif time.time()-own['submitted_at']>10:reason='MARKET_ORDER_TIMEOUT'
                    if reason:self.cancel_own(trade,reason,o)
                    if not positions or self.db.get(ident)['status']=='UNCERTAIN':return None
        known=dict(d.get('manual_entries',{}));unknown=set(external)-set(known)
        if unknown:
            linked={r['parent_id'] for r in self.ex.transactions(d['submitted_at']-2)
                    if r.get('pair')==pair and r.get('position_id')==d['position_id']
                    and r.get('stage')=='default' and 'Order' in r.get('parent_type','')}
            if not unknown<=linked:
                self.cancel_own(trade,'UNVERIFIED_MANUAL_ADDITION')
                self.e.conflict(trade,'LONG_MANUAL_ADDITION_UNVERIFIED');return None
        for oid,o in external.items():
            known[oid]={'quantity':str(filled_quantity(o)),'fill':str(decimal(o.get('avg_price'),True))}
        self.db.update(ident,self.db.get(ident)['status'],manual_entries=known)
        qty=initial_qty+own_qty+sum((decimal(v['quantity']) for v in known.values()),D(0))
        cost=initial_qty*initial_fill+own_qty*own_fill+sum((decimal(v['quantity'])*decimal(v['fill']) for v in known.values()),D(0))
        avg=cost/qty
        if positions:
            p=positions[0];tolerance=max(decimal(d['info']['price_increment'],True),avg*D('.00000001'))
            if (len(positions)!=1 or p['id']!=d['position_id'] or decimal(p['active_pos'])!=qty
                    or abs(decimal(p['avg_price'],True)-avg)>tolerance):
                self.cancel_own(trade,'POSITION_CHANGED')
                self.e.conflict(trade,'LONG_AVERAGE_POSITION_MISMATCH');return None
            if p.get('margin_type')!='isolated' or decimal(p.get('leverage') or 0)!=1:
                self.cancel_own(trade,'POSITION_SETTINGS_CHANGED')
                self.e.conflict(trade,'MARGIN_MODE_OR_LEVERAGE_MISMATCH');return None
            avg=decimal(p['avg_price'],True)
        if own_qty>decimal(own.get('announced_quantity') or 0):
            self.save_order(trade,announced_quantity=str(own_qty))
            self.emit('LONG_AVERAGE_FILLED',pair=pair,trade_id=ident,added_quantity=str(own_qty),
                      combined_quantity=str(qty),average_entry=str(avg),tp_percent='6')
        return qty,avg

    def maybe_place(self,trade):
        d=trade['data'];pair=trade['pair'];now=time.time()
        if (not self.c.enabled or trade['status']!='OPEN' or d['side']!='BUY' or not d.get('position_id')
                or d.get('averaging_used') or d['mode']!='isolated' or d['leverage']!=1):return
        nongold(pair)
        if not self.e.capacity().within_limits:return
        reference=decimal(d['initial_fill'],True);threshold=reference*(1-DROP)
        price=self.ex.price(pair,'BUY')
        if price>=threshold:return
        if now-self.checked.get(trade['id'],0)<60:return
        self.checked[trade['id']]=now
        bars=self.market.four_hour(pair,now)
        if not long_confirmation(bars,now,price):return
        info=self.market.metadata(pair)
        if not self.market.eligible_metadata(info):return
        if info.get('order_types') and 'market_order' not in info['order_types']:return
        original=self.ex.find_order(pair,'BUY',d['order_id'])
        if original is None:return
        positions=[p for p in self.ex.positions(pair) if decimal(p['active_pos'])!=0]
        synced=self.sync(self.db.get(trade['id']),positions,original,filled_quantity(original))
        row=self.db.get(trade['id'])
        if synced is None or not positions or row['data'].get('averaging_used') or row['status']!='OPEN':return
        if synced[0]!=decimal(d['filled_quantity']):return
        if not self.e.capacity().within_limits:return
        # Preserve manual closing orders; do not add while a discretionary close is pending.
        if any(o.get('stage')!='tpsl_exit' for o in self.ex.orders(pair,'SELL')):return
        latest=self.market.quotes().get(pair);price=self.ex.price(pair,'BUY');now=time.time()
        if latest is None or not -30<=now-latest.timestamp<=60:return
        high=max(price,latest.price,latest.mark_price or latest.price)
        low=min(price,latest.price,latest.mark_price or latest.price)
        confirmation=long_confirmation(bars,now,low)
        if high>=threshold or not confirmation:return
        try:
            qty=quantity(info,high,self.c.long_margin,1,self.c.long_margin_cap,low)
            validate_quantity(info,qty,high,synced[0],1,low)
        except ValueError as error:
            self.emit('LONG_AVERAGE_SKIPPED',pair=pair,trade_id=trade['id'],reason=str(error));return
        # Repeat position and same-side order checks immediately before the durable claim.
        fresh=[p for p in self.ex.positions(pair) if decimal(p['active_pos'])!=0]
        if (len(fresh)!=1 or fresh[0]['id']!=d['position_id'] or decimal(fresh[0]['active_pos'])!=synced[0]
                or decimal(fresh[0]['avg_price'])!=synced[1] or fresh[0].get('margin_type')!='isolated'
                or decimal(fresh[0].get('leverage') or 0)!=1 or decimal(fresh[0].get('stop_loss_trigger') or 0)!=0):return
        pending=[o for o in self.ex.orders(pair,'BUY') if o.get('id')!=d['order_id'] and o.get('stage')=='default']
        if pending:
            self.manual(trade,[o['id'] for o in pending],'MANUAL_ENTRY_BEFORE_SUBMISSION');return
        intent={'status':'submitting','quantity':str(qty),'reference':str(reference),'drop_trigger':str(DROP),
                'market_price':str(price),'order_type':'market_order','side':'BUY','submitted_at':time.time(),
                'estimated_margin':str(qty*high),'confirmation':confirmation}
        if not self.db.claim_average(trade['id'],intent):return
        try:
            oid=self.ex.average_long(pair,qty)
            self.save_order(trade,status='open',order_id=oid)
            self.emit('LONG_AVERAGE_SUBMITTED',pair=pair,trade_id=trade['id'],order_id=oid,quantity=str(qty),
                      estimated_margin=str(qty*high),leverage=1,mode='isolated',confirmation=confirmation,
                      initial_entry=str(reference),tp='6% above the confirmed combined average')
        except Exception as error:
            rejected=isinstance(error,ExchangeError) and error.rejected
            self.save_order(trade,status='rejected' if rejected else 'uncertain')
            if not rejected:self.db.update(trade['id'],'UNCERTAIN')
            self.emit('LONG_AVERAGE_REJECTED' if rejected else 'LONG_AVERAGE_UNCERTAIN',pair=pair,trade_id=trade['id'],
                      error_type=type(error).__name__,reason=str(error) if isinstance(error,ExchangeError) else '')
