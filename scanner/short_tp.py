"""Durable staged short profits, including minimum-size partial exits."""
import time
from decimal import Decimal,ROUND_DOWN,ROUND_UP
from .market import decimal
from .exchange import target,ExchangeError
from .averaging import TERMINAL,TRIGGER,filled_quantity

D=Decimal


def partial_quantity(info,qty,price,preferred=None):
    """Keep the planned split when valid; otherwise use the smallest valid close."""
    step=decimal(info['quantity_increment'],True)
    minimum=max(decimal(info['min_quantity'],True),decimal(info.get('min_trade_size',info['min_quantity']),True))
    minimum=(minimum/step).to_integral_value(rounding=ROUND_UP)*step
    min_notional=decimal(info['min_notional'],True)
    maximum=min(decimal(info['max_market_order_quantity'],True),decimal(info['max_quantity'],True))
    part=(qty*D('.75')/step).to_integral_value(rounding=ROUND_DOWN)*step if preferred is None else preferred
    if qty<=0 or qty%step or part%step:raise ValueError('INVALID_SHORT_SPLIT_QUANTITY')
    if part>maximum:raise ValueError('SHORT_PARTIAL_EXCEEDS_MAXIMUM_QUANTITY')
    if part>=minimum and qty-part>=minimum and part*price>=min_notional:
        return part
    part=(max(minimum,min_notional/decimal(price,True))/step).to_integral_value(rounding=ROUND_UP)*step
    # The native full-position TP closes the remainder, even below min notional.
    if part>maximum:raise ValueError('SHORT_PARTIAL_EXCEEDS_MAXIMUM_QUANTITY')
    if qty-part<minimum:raise ValueError('SHORT_TOO_SMALL_FOR_MINIMUM_PARTIAL_TP')
    return part


def split_plan(info,qty,fill):
    tp1=target(info,'SELL',fill,D('.07'));tp2=target(info,'SELL',fill,D('.20'))
    part=partial_quantity(info,qty,tp1)
    return {'base_quantity':str(qty),'reference':str(fill),'tp1_quantity':str(part),
            'tp1':str(tp1),'tp2':str(tp2),'phase':'waiting'}


def replay_short(fills,sell_ids,buy_ids):
    """Weighted cost survives reductions; reopening after flat is a different episode."""
    qty=D(0);cost=D(0);closed=False
    for f in sorted(fills,key=lambda x:float(x['timestamp'])):
        q=decimal(f['quantity'],True);price=decimal(f['price'],True);oid=f['order_id']
        if f['side']=='sell':
            if oid not in sell_ids:raise ValueError('UNKNOWN_SHORT_ENTRY_FILL')
            if closed:raise ValueError('SHORT_REOPENED_AFTER_CLOSURE')
            qty+=q;cost+=q*price
        elif f['side']=='buy':
            if oid not in buy_ids:raise ValueError('UNATTRIBUTED_SHORT_REDUCTION')
            if q>qty:raise ValueError('SHORT_EXIT_EXCEEDS_POSITION')
            cost-=q*(cost/qty);qty-=q
            if qty==0:closed=True;cost=D(0)
        else:raise ValueError('INVALID_FILL_SIDE')
    return qty,cost/qty if qty else D(0)


class ShortTakeProfit:
    def __init__(self,engine):
        self.e=engine;self.db=engine.db;self.ex=engine.ex;self.emit=engine.emit

    def save(self,trade,**changes):
        row=self.db.get(trade['id']);s={**row['data'].get('short_tp',{}),**changes}
        self.db.update(row['id'],row['status'],short_tp=s)
        return s

    def update_partial(self,trade,**changes):
        row=self.db.get(trade['id']);items=list(row['data'].get('tp1_orders',[]))
        items[-1]={**items[-1],**changes}
        self.db.update(row['id'],row['status'],tp1_orders=items)
        return items[-1]

    def prepare(self,trade):
        """Read a submitted partial exit before position-quantity reconciliation."""
        d=trade['data'];items=d.get('tp1_orders',[])
        if not items:return True
        a=items[-1]
        if a.get('status') in TERMINAL:return True
        if not a.get('order_id'):
            self.db.update(trade['id'],'UNCERTAIN')
            self.emit('PARTIAL_TP_UNCERTAIN',pair=trade['pair'],trade_id=trade['id'],reason='MISSING_ACKNOWLEDGEMENT')
            return False
        o=self.ex.find_order(trade['pair'],'BUY',a['order_id'])
        if o is None:
            self.db.update(trade['id'],'UNCERTAIN');return False
        q=filled_quantity(o)
        if decimal(o['total_quantity'])!=decimal(a['quantity']):
            self.e.conflict(trade,'PARTIAL_TP_QUANTITY_CHANGED');return False
        self.update_partial(trade,status=o['status'],filled_quantity=str(q))
        if o['status'] not in TERMINAL and time.time()-a['submitted_at']>10:
            attempts=a.get('cancel_attempts',0)
            if attempts>=3:
                self.db.update(trade['id'],'UNCERTAIN')
                self.emit('PARTIAL_TP_UNCERTAIN',pair=trade['pair'],trade_id=trade['id'],reason='CANCEL_NOT_CONFIRMED')
                return False
            if not attempts or time.time()-a.get('cancel_requested_at',0)>=30:
                self.update_partial(trade,cancel_attempts=attempts+1,cancel_requested_at=time.time())
                self.ex.cancel(trade['pair'],o)
        return True

    def arm_runner(self,trade,p,tp):
        qty=decimal(p['active_pos']);fill=decimal(p['avg_price'])
        if decimal(p.get('take_profit_trigger') or 0)!=tp:
            if self.ex.price(trade['pair'],'BUY')<=tp:
                if not self.e.averager.cancel_own(trade,'FINAL_TP_EXIT'):return False
                self.db.update(trade['id'],'CLOSING',exit_requested_at=time.time())
                self.ex.exit(trade['pair'],p)
                self.emit('TP_EXIT_REQUESTED',pair=trade['pair'],trade_id=trade['id']);return False
            s=self.db.get(trade['id'])['data'].get('short_tp',{})
            attempt=s.get('attach',{})
            if attempt.get('target')!=str(tp):attempt={'target':str(tp),'attempts':0}
            if attempt['attempts']>=3:
                self.db.update(trade['id'],'UNCERTAIN')
                self.emit('PARTIAL_TP_UNCERTAIN',pair=trade['pair'],trade_id=trade['id'],reason='RUNNER_TP_NOT_CONFIRMED')
                return False
            if attempt['attempts'] and time.time()-attempt.get('at',0)<30:return False
            self.save(trade,attach={**attempt,'attempts':attempt['attempts']+1,'at':time.time()})
            self.ex.take_profit(trade['pair'],p,tp)
            fresh=next((x for x in self.ex.positions(trade['pair']) if x['id']==p['id']),None)
            if fresh is None or decimal(fresh['active_pos'])==0:return False
            if decimal(fresh['active_pos'])!=qty or decimal(fresh['avg_price'])!=fill:
                self.e.conflict(trade,'POSITION_CHANGED_DURING_TP_UPDATE');return False
            if decimal(fresh.get('take_profit_trigger') or 0)!=tp:
                self.emit('TP_PENDING',pair=trade['pair'],trade_id=trade['id']);return False
        self.save(trade,attach={})
        self.db.update(trade['id'],'OPEN',tp=str(tp),tp_pct='.20')
        return True

    def cancel_native(self,trade,p,buy_orders):
        """Confirm cancellation before a sized BUY can overlap a full-position TP."""
        s=self.db.get(trade['id'])['data'].get('short_tp',{})
        native=[o for o in buy_orders if o.get('stage')=='tpsl_exit'
                and o.get('order_category')=='complete_tpsl' and o.get('order_type')=='take_profit_market']
        foreign=[o for o in buy_orders if o not in native]
        if foreign:
            self.emit('PARTIAL_TP_WAITING',pair=trade['pair'],trade_id=trade['id'],reason='MANUAL_BUY_ORDER_PENDING')
            return False
        if len(native)>1:
            self.e.conflict(trade,'MULTIPLE_NATIVE_SHORT_TPS');return False
        if native:
            o=native[0]
            if decimal(o.get('stop_price') or 0)!=decimal(p.get('take_profit_trigger') or 0):return False
            cancel=s.get('native_cancel',{})
            if cancel.get('order_id')!=o['id']:cancel={'order_id':o['id'],'attempts':0}
            if cancel['attempts']>=3:
                self.emit('PARTIAL_TP_UNCERTAIN',pair=trade['pair'],trade_id=trade['id'],reason='NATIVE_TP_CANCEL_NOT_CONFIRMED')
                self.db.update(trade['id'],'UNCERTAIN');return False
            if not cancel['attempts'] or time.time()-cancel.get('requested_at',0)>=30:
                cancel.update(attempts=cancel['attempts']+1,requested_at=time.time())
                self.save(trade,native_cancel=cancel)
                self.ex.cancel(trade['pair'],o)
            return False
        if s.get('native_cancel'):
            o=self.ex.find_order(trade['pair'],'BUY',s['native_cancel']['order_id'])
            if o is None or o['status'] not in ('cancelled','rejected'):return False
        return decimal(p.get('take_profit_trigger') or 0)==0

    def rearm(self,trade,p,s):
        d=self.db.get(trade['id'])['data'];history=list(d.get('average_history',[]))
        if d.get('average_order'):history.append(d['average_order'])
        baseline={oid:entry['quantity'] for oid,entry in d.get('manual_entries',{}).items()}
        s={**s,'phase':'runner','runner_quantity':str(-decimal(p['active_pos'])),
           'rearmed_at':time.time(),'native_cancel':None}
        self.db.update(trade['id'],'PROTECTING',short_tp=s,average_history=history,average_order={},
                       averaging_used=False,averaging_source=None,manual_average_seen=False,
                       manual_cycle_baseline=baseline,average_reference=str(decimal(p['avg_price'])),
                       average_margin_mode=True)
        self.emit('PARTIAL_TP_FILLED',pair=trade['pair'],trade_id=trade['id'],
                  closed_quantity=s['tp1_quantity'],remaining_quantity=s['runner_quantity'],
                  remaining_tp=s['tp2'],averaging=f'Rearmed: 3 USDT margin / 3x after +{TRIGGER*100:g}%')

    def manage(self,trade,p):
        """True owns the split lifecycle; False preserves legacy TP for undersized positions."""
        ident,pair,d=trade['id'],trade['pair'],trade['data']
        qty=-decimal(p['active_pos']);fill=decimal(p['avg_price'],True)
        s=d.get('short_tp',{})
        if s.get('phase')=='closing':
            a=d['tp1_orders'][-1]
            if a['status'] not in TERMINAL:return True
            if (decimal(a.get('filled_quantity') or 0)!=decimal(s['tp1_quantity'])
                    or qty!=decimal(s['base_quantity'])-decimal(s['tp1_quantity'])
                    or abs(fill-decimal(s['reference']))>decimal(d['info']['price_increment'],True)):
                self.arm_runner(trade,p,decimal(s['tp2']))
                if self.db.get(ident)['status']=='CLOSING':return True
                self.e.conflict(self.db.get(ident),'PARTIAL_TP_INCOMPLETE_OR_POSITION_CHANGED');return True
            self.rearm(trade,p,s)
            trade=self.db.get(ident);d=trade['data'];s=d['short_tp']
        if s.get('phase')=='runner' and qty==decimal(s['runner_quantity']):
            self.arm_runner(trade,p,decimal(s['tp2']));return True
        if not s or s.get('phase')=='runner' or qty!=decimal(s['base_quantity']) or fill!=decimal(s['reference']):
            try:plan=split_plan(d['info'],qty,fill)
            except ValueError as error:
                self.emit('SPLIT_TP_UNAVAILABLE',pair=pair,trade_id=ident,reason=str(error));return False
            s={**plan,'cycle':s.get('cycle',0)+1}
            self.db.update(ident,'PROTECTING',short_tp=s)
        tp1,tp2=decimal(s['tp1']),decimal(s['tp2'])
        ask=self.ex.price(pair,'BUY')
        if ask<=tp2:
            self.arm_runner(trade,p,tp2);return True
        if ask>tp1:
            self.arm_runner(trade,p,tp2);return True
        # An unsplittable price gap must not repeatedly cancel/recreate the native TP.
        try:partial_quantity(d['info'],qty,ask,preferred=decimal(s['tp1_quantity']))
        except ValueError as error:
            self.arm_runner(trade,p,tp2)
            self.emit('PARTIAL_TP_WAITING',pair=pair,trade_id=ident,reason=str(error));return True
        if not self.e.averager.cancel_own(trade,'PARTIAL_TP_EXIT'):return True
        if not self.cancel_native(trade,p,self.ex.orders(pair,'BUY')):return True
        # Recheck quantity, mode, price and other orders after cancellation/API latency.
        fresh=[x for x in self.ex.positions(pair) if decimal(x['active_pos'])!=0]
        if (len(fresh)!=1 or fresh[0]['id']!=p['id'] or decimal(fresh[0]['active_pos'])!=-qty
                or decimal(fresh[0]['avg_price'])!=fill or fresh[0].get('margin_type')!='crossed'
                or decimal(fresh[0].get('leverage') or 0)!=d['leverage']
                or decimal(fresh[0].get('take_profit_trigger') or 0)!=0):return True
        if self.ex.orders(pair,'BUY'):return True
        ask=self.ex.price(pair,'BUY')
        part=decimal(s['tp1_quantity'])
        if ask<=tp2:
            self.arm_runner(trade,p,tp2);return True
        if ask>tp1:
            self.arm_runner(trade,p,tp2);return True
        try:part=partial_quantity(d['info'],qty,ask,preferred=part)
        except ValueError as error:
            self.arm_runner(trade,p,tp2)
            self.emit('PARTIAL_TP_WAITING',pair=pair,trade_id=ident,reason=str(error));return True
        # Persist any minimum-size adjustment with the intent, before the API call.
        s={**s,'tp1_quantity':str(part)}
        intent={'status':'submitting','quantity':str(part),'submitted_at':time.time(),'cycle':s['cycle']}
        items=list(self.db.get(ident)['data'].get('tp1_orders',[]))+[intent]
        self.db.update(ident,'PROTECTING',tp1_orders=items,short_tp={**s,'phase':'closing'})
        try:
            oid=self.ex.partial_short_exit(pair,part,d['leverage'])
            self.update_partial(trade,status='open',order_id=oid)
            self.emit('PARTIAL_TP_SUBMITTED',pair=pair,trade_id=ident,quantity=str(part),target=str(tp1),order_id=oid)
        except Exception as error:
            rejected=isinstance(error,ExchangeError) and error.rejected
            self.update_partial(trade,status='rejected' if rejected else 'uncertain',filled_quantity='0')
            self.db.update(ident,'PROTECTING' if rejected else 'UNCERTAIN')
            self.emit('PARTIAL_TP_UNCERTAIN',pair=pair,trade_id=ident,error_type=type(error).__name__,
                      reason=str(error) if isinstance(error,ExchangeError) else '')
        return True
