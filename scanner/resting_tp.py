"""Exchange-resting first short profit exit; native final TP follows its fill."""
import time
from .market import decimal
from .exchange import ExchangeError
from .averaging import TERMINAL,filled_quantity
from .short_tp import ShortTakeProfit,split_plan,partial_quantity


def limit_checks(info,qty,price,last):
    if info.get('order_types') and 'limit_order' not in info['order_types']:
        raise ValueError('PARTIAL_TP_LIMIT_NOT_SUPPORTED')
    if price%decimal(info['price_increment'],True):raise ValueError('PARTIAL_TP_INVALID_PRICE_TICK')
    if not decimal(info['min_price'],True)<=price<=decimal(info['max_price'],True):
        raise ValueError('PARTIAL_TP_OUTSIDE_PRICE_RANGE')
    if qty>decimal(info['max_quantity'],True):raise ValueError('PARTIAL_TP_EXCEEDS_MAXIMUM_QUANTITY')
    # CoinDCX's multiplier_up bounds aggressive BUYs. multiplier_down is for SELLs;
    # applying it here would incorrectly block a resting profit BUY below market.
    if info.get('multiplier_up') is not None and price>last*(1+decimal(info['multiplier_up'])/100):
        raise ValueError('PARTIAL_TP_OUTSIDE_EXCHANGE_LTP_RANGE')


class RestingShortTakeProfit(ShortTakeProfit):
    def latest(self,trade):
        items=self.db.get(trade['id'])['data'].get('tp1_orders',[])
        return items[-1] if items else {}

    def cancel_resting(self,trade,reason):
        a=self.latest(trade)
        if a.get('order_type')!='limit_order' or a.get('status') in TERMINAL:return True
        if not a.get('order_id'):
            self.db.update(trade['id'],'UNCERTAIN');return False
        o=self.ex.find_order(trade['pair'],'BUY',a['order_id'])
        if o is None:
            self.db.update(trade['id'],'UNCERTAIN');return False
        self.update_partial(trade,status=o['status'],filled_quantity=str(filled_quantity(o)))
        if o['status'] in TERMINAL:return True
        attempts=a.get('cancel_attempts',0)
        if attempts>=3:
            self.db.update(trade['id'],'UNCERTAIN')
            self.emit('PARTIAL_TP_UNCERTAIN',pair=trade['pair'],trade_id=trade['id'],reason='LIMIT_CANCEL_NOT_CONFIRMED')
            return False
        if not attempts or time.time()-a.get('cancel_requested_at',0)>=30:
            self.update_partial(trade,cancel_attempts=attempts+1,cancel_requested_at=time.time(),cancel_reason=reason)
            self.ex.cancel(trade['pair'],o)
            self.emit('PARTIAL_TP_CANCEL_REQUESTED',pair=trade['pair'],trade_id=trade['id'],reason=reason)
        return False

    def prepare(self,trade):
        a=self.latest(trade)
        if a.get('order_type')!='limit_order':return super().prepare(trade)
        if a.get('status') in TERMINAL:return True
        if not a.get('order_id'):
            self.db.update(trade['id'],'UNCERTAIN')
            self.emit('PARTIAL_TP_UNCERTAIN',pair=trade['pair'],trade_id=trade['id'],reason='MISSING_LIMIT_ACKNOWLEDGEMENT')
            return False
        o=self.ex.find_order(trade['pair'],'BUY',a['order_id'])
        if o is None:
            self.db.update(trade['id'],'UNCERTAIN');return False
        executed=filled_quantity(o)
        self.update_partial(trade,status=o['status'],filled_quantity=str(executed))
        if (decimal(o['total_quantity'])!=decimal(a['quantity'])
                or decimal(o.get('price') or 0)!=decimal(a['limit_price']) or o.get('order_type')!='limit_order'):
            self.cancel_resting(trade,'LIMIT_CHANGED')
            self.e.conflict(trade,'PARTIAL_TP_LIMIT_CHANGED');return False
        if o['status'] in TERMINAL:return True
        positions=[p for p in self.ex.positions(trade['pair']) if decimal(p['active_pos'])!=0]
        d=self.db.get(trade['id'])['data'];s=d['short_tp']
        reason=a.get('cancel_reason')
        if (len(positions)!=1 or positions[0]['id']!=d['position_id']
                or decimal(positions[0]['active_pos'])>=0):reason='POSITION_CLOSED_OR_REVERSED'
        elif (-decimal(positions[0]['active_pos'])+executed!=decimal(s['base_quantity'])
                or decimal(positions[0]['avg_price'])!=decimal(s['reference'])):reason='POSITION_SIZE_OR_AVERAGE_CHANGED'
        elif (positions[0].get('margin_type')!='crossed' or decimal(positions[0].get('leverage') or 0)!=d['leverage']):
            reason='POSITION_SETTINGS_CHANGED'
        elif decimal(positions[0].get('take_profit_trigger') or 0)!=0:reason='COMPETING_NATIVE_TP'
        elif any(x['id']!=a['order_id'] for x in self.ex.orders(trade['pair'],'BUY')):reason='OTHER_BUY_ORDER_PENDING'
        if reason:
            self.cancel_resting(trade,reason);return False
        return True

    def open_limit(self,trade,s):
        self.db.update(trade['id'],'OPEN',tp=s['tp1'],tp_pct='.07')

    def manage(self,trade,p):
        ident,pair,d=trade['id'],trade['pair'],trade['data']
        s=d.get('short_tp',{});a=self.latest(trade)
        qty=-decimal(p['active_pos']);fill=decimal(p['avg_price'],True)
        # A pre-upgrade market request must finish reconciliation, never be duplicated.
        if s.get('phase')=='closing' and a.get('order_type')!='limit_order':
            return super().manage(trade,p)
        if s.get('phase')=='runner' and qty==decimal(s['runner_quantity']):
            self.arm_runner(trade,p,decimal(s['tp2']));return True
        if s.get('phase')=='resting':
            if a.get('cycle')!=s.get('cycle'):
                self.e.conflict(trade,'PARTIAL_TP_CYCLE_MISMATCH');return True
            if a['status'] not in TERMINAL:
                self.open_limit(trade,s);return True
            complete=(decimal(a.get('filled_quantity') or 0)==decimal(s['tp1_quantity'])
                      and qty==decimal(s['base_quantity'])-decimal(s['tp1_quantity'])
                      and abs(fill-decimal(s['reference']))<=decimal(d['info']['price_increment'],True))
            if complete:
                if not self.e.averager.cancel_own(trade,'FIRST_PROFIT_FILLED'):return True
                self.rearm(trade,p,s)
                self.arm_runner(self.db.get(ident),p,decimal(s['tp2']));return True
            incomplete=(decimal(a.get('filled_quantity') or 0)>0
                        and qty+decimal(a['filled_quantity'])==decimal(s['base_quantity'])
                        and fill==decimal(s['reference']))
            if incomplete or (not a.get('cancel_reason') and a['status']!='filled'):
                self.arm_runner(trade,p,decimal(s['tp2']))
                if self.db.get(ident)['status']!='CLOSING':
                    self.e.conflict(trade,'PARTIAL_TP_LIMIT_INCOMPLETE' if incomplete else 'PARTIAL_TP_LIMIT_CANCELLED_OR_REJECTED')
                return True
            # A verified size/average change starts a fresh split after cancellation.
            s={**s,'phase':'replan'}
        if (not s or s.get('phase') in ('runner','replan') or qty!=decimal(s['base_quantity'])
                or fill!=decimal(s['reference'])):
            if not self.cancel_resting(trade,'REPLAN'):return True
            try:plan=split_plan(d['info'],qty,fill)
            except ValueError as error:
                self.emit('SPLIT_TP_UNAVAILABLE',pair=pair,trade_id=ident,reason=str(error));return False
            s={**plan,'cycle':s.get('cycle',0)+1}
            self.db.update(ident,'PROTECTING',short_tp=s)
        if time.time()<s.get('limit_retry_at',0):return True
        tp1,tp2=decimal(s['tp1']),decimal(s['tp2'])
        ask=self.ex.price(pair,'BUY')
        # The full native exit is safe only after our sized limit is absent.
        if ask<=tp2:
            if self.cancel_resting(trade,'FINAL_TP_EXIT'):self.arm_runner(trade,p,tp2)
            return True
        info=self.e.market.metadata(pair)
        latest=self.e.market.quotes().get(pair)
        if latest is None or not -30<=time.time()-latest.timestamp<=60:
            self.emit('PARTIAL_TP_WAITING',pair=pair,trade_id=ident,reason='STALE_LIMIT_QUOTE');return True
        try:
            part=partial_quantity(info,qty,min(tp1,ask,latest.price,latest.mark_price or latest.price),
                                  preferred=decimal(s['tp1_quantity']))
            limit_checks(info,part,tp1,latest.price)
        except ValueError as error:
            self.save(trade,limit_retry_at=time.time()+60)
            self.arm_runner(trade,p,tp2)
            self.emit('PARTIAL_TP_WAITING',pair=pair,trade_id=ident,reason=str(error));return True
        # Native full-position TP is removed before placing the partial BUY limit.
        if not self.cancel_native(trade,p,self.ex.orders(pair,'BUY')):return True
        fresh=[x for x in self.ex.positions(pair) if decimal(x['active_pos'])!=0]
        if (len(fresh)!=1 or fresh[0]['id']!=p['id'] or decimal(fresh[0]['active_pos'])!=-qty
                or decimal(fresh[0]['avg_price'])!=fill or fresh[0].get('margin_type')!='crossed'
                or decimal(fresh[0].get('leverage') or 0)!=d['leverage']
                or decimal(fresh[0].get('take_profit_trigger') or 0)!=0
                or decimal(fresh[0].get('stop_loss_trigger') or 0)!=0):return True
        if self.ex.orders(pair,'BUY'):return True
        ask=self.ex.price(pair,'BUY')
        if ask<=tp2:
            self.arm_runner(trade,p,tp2);return True
        latest=self.e.market.quotes().get(pair)
        if latest is None or not -30<=time.time()-latest.timestamp<=60:
            self.arm_runner(trade,p,tp2);return True
        try:
            part=partial_quantity(info,qty,min(tp1,ask,latest.price,latest.mark_price or latest.price),preferred=part)
            limit_checks(info,part,tp1,latest.price)
        except ValueError as error:
            self.save(trade,limit_retry_at=time.time()+60)
            self.arm_runner(trade,p,tp2)
            self.emit('PARTIAL_TP_WAITING',pair=pair,trade_id=ident,reason=str(error));return True
        # Limit stays at the requested profit price, including after a downward wick.
        s={**s,'tp1_quantity':str(part),'phase':'resting','native_cancel':None,'attach':{}}
        intent={'status':'submitting','quantity':str(part),'limit_price':str(tp1),'order_type':'limit_order',
                'submitted_at':time.time(),'cycle':s['cycle']}
        items=list(self.db.get(ident)['data'].get('tp1_orders',[]))+[intent]
        self.db.update(ident,'PROTECTING',tp1_orders=items,short_tp=s)
        try:
            oid=self.ex.partial_short_limit(pair,part,d['leverage'],tp1)
            self.update_partial(trade,status='open',order_id=oid)
            self.open_limit(trade,s)
            self.emit('PARTIAL_TP_LIMIT_SUBMITTED',pair=pair,trade_id=ident,quantity=str(part),
                      limit_price=str(tp1),remaining_quantity=str(qty-part),remaining_target=str(tp2),order_id=oid)
        except Exception as error:
            rejected=isinstance(error,ExchangeError) and error.rejected
            self.update_partial(trade,status='rejected' if rejected else 'uncertain',filled_quantity='0')
            self.db.update(ident,'PROTECTING' if rejected else 'UNCERTAIN')
            self.emit('PARTIAL_TP_UNCERTAIN',pair=pair,trade_id=ident,error_type=type(error).__name__,
                      reason=str(error) if isinstance(error,ExchangeError) else '')
        return True
