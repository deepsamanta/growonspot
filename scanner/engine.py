"""Serialized execution for the independent scanner, with durable order intent."""
import time
from decimal import Decimal
from .exchange import ExchangeError, quantity, target, short_limit_price
from .market import decimal
from .capacity import BalanceCapacity
from .strategy import long_confirmation
from .averaging import ShortAverager, RECOVERABLE
from .short_tp import ShortTakeProfit, split_plan

D=Decimal


class Engine:
    def __init__(self,config,state,exchange,market,emit):
        self.c,self.db,self.ex,self.market,self.emit=config,state,exchange,market,emit
        self.ledger_at=0
        self.averager=ShortAverager(self)
        self.balanced_capacity=BalanceCapacity(exchange,state)
        self.short_tp=ShortTakeProfit(self)

    def capacity(self):
        return self.balanced_capacity.get()

    def ledger(self,since):
        rows=self.ex.transactions(since)
        for row in rows:
            if (row.get('pair')!='B-XAU_USDT' and row.get('stage')!='funding'
                    and decimal(row.get('amount',0))>0):
                self.db.lock_profit(row['pair'],float(row['created_at'])/1000)
        return rows

    def enter(self,candidate):
        if not self.c.enabled:
            return
        pair,side=candidate.pair,candidate.side
        if pair=='B-XAU_USDT':raise ValueError('Gold is excluded')
        capacity=self.capacity()
        if not capacity.allows(side) or pair in capacity.pairs:
            self.emit('ENTRY_SKIPPED',pair=pair,reason='ACCOUNT_POSITION_LIMIT',**capacity.report());return
        if any(r['pair']==pair and r['status'] not in ('CLOSED','REJECTED') for r in self.db.rows()):return
        now=time.time()
        cooldown=self.db.cache('rejection:'+pair)
        if cooldown and cooldown['until']>now:return
        if now-candidate.observed_at>600:return
        if side not in ('BUY','SELL'):raise ValueError('Invalid side')
        # Refresh quotes at execution, after a potentially lengthy universe scan.
        quote=self.market.quotes().get(pair)
        if quote is None:return
        price=self.ex.price(pair,side)
        if side=='SELL':
            current=max(price,quote.price,quote.mark_price or quote.price)
            if quote.change_24h<=35 or not candidate.reference*(1-self.c.short_distance)<=current<candidate.reference:return
            margin,leverage,mode,pct=self.c.short_margin,self.c.short_leverage,'crossed',self.c.short_tp
            cap=None
        else:
            low=min(candidate.reference,quote.low_24h or quote.price,quote.price)
            if price>low*D('1.10'):return
            margin,leverage,mode,pct=self.c.long_margin,self.c.long_leverage,'isolated',self.c.long_tp
            cap=self.c.long_margin_cap
            intraday=self.market.four_hour(pair,now)
            confirmation=long_confirmation(intraday,now,price)
            if not confirmation:return
        if now-candidate.history_first<100*86400:return
        info=self.market.metadata(pair)
        if not self.market.eligible_metadata(info):return
        side_limit=info.get('max_leverage_short' if side=='SELL' else 'max_leverage_long')
        # CoinDCX often leaves these legacy fields null; quantity() enforces the
        # current dynamic position/leverage table for every entry.
        if side_limit is not None and decimal(side_limit)<leverage:
            raise ValueError('LEVERAGE_NOT_SUPPORTED')
        limit=short_limit_price(info,candidate.reference,quote.price) if side=='SELL' else None
        if limit is not None and info.get('order_types') and 'limit_order' not in info['order_types']:
            raise ValueError('LIMIT_ORDERS_NOT_SUPPORTED')
        sizing_price=max(limit or price,quote.price,quote.mark_price or quote.price)
        minimum_price=min(price,quote.price,quote.mark_price or quote.price)
        qty=quantity(info,sizing_price,margin,leverage,cap,minimum_price)
        if side=='SELL' and self.c.short_split_tp:split_plan(info,qty,limit)
        tp=target(info,side,limit or price,pct)
        if any(decimal(p.get(k,0))!=0 for p in self.ex.positions(pair)
               for k in ('active_pos','inactive_pos_buy','inactive_pos_sell')):return
        if self.ex.orders(pair,'BUY') or self.ex.orders(pair,'SELL'):return
        if now-self.ledger_at>30:
            start=self.db.day(now)
            from datetime import datetime
            midnight=datetime.fromisoformat(start).replace(tzinfo=self.db.zone).timestamp()
            self.ledger(midnight);self.ledger_at=now
        data={'pair':pair,'side':side,'quantity':str(qty),'leverage':leverage,'mode':mode,
              'margin_budget':str(margin),'margin_cap':str(cap if cap is not None else margin),
              'estimated_margin':str(qty*sizing_price/leverage),'tp_pct':str(pct),'tp':str(tp),
              'reference':str(candidate.reference),'submitted_at':now,'market_price':str(price),
              'order_type':'limit_order' if limit is not None else 'market_order',
              'limit_price':str(limit) if limit is not None else None,
              'expires_at':now+self.c.short_limit_seconds if limit is not None else None,
              'confirmation':confirmation if side=='BUY' else 'WEEKLY_RESISTANCE','info':info}
        key=f'{pair}:{side}:{int(candidate.observed_at//300)}'
        ident,reason=self.db.reserve(key,pair,data,capacity.max_total,now,{'BUY':capacity.max_longs,'SELL':capacity.max_shorts})
        if ident is None:
            self.emit('ENTRY_SKIPPED',pair=pair,reason=reason);return
        submitted=False
        try:
            if not self.capacity().within_limits:
                self.db.update(ident,'REJECTED',reason='ACCOUNT_CAPACITY_CHANGED');return
            self.ex.prepare(pair,leverage,mode)
            # Private preflight calls take time: refresh sizing immediately before
            # submission, using last/mark as well as the executable book price.
            latest=self.market.quotes().get(pair)
            if latest is None:raise ValueError('FRESH_QUOTE_UNAVAILABLE')
            price=self.ex.price(pair,side)
            if side=='SELL':
                current=max(price,latest.price,latest.mark_price or latest.price)
                if latest.change_24h<=35 or not candidate.reference*(1-self.c.short_distance)<=current<candidate.reference:
                    raise ValueError('SHORT_LEFT_RESISTANCE_BAND')
                limit=short_limit_price(info,candidate.reference,latest.price)
            elif (price>min(candidate.reference,latest.low_24h or latest.price,latest.price)*D('1.10')
                    or not long_confirmation(intraday,time.time(),price)):
                raise ValueError('LONG_CONFIRMATION_NO_LONGER_VALID')
            sizing_price=max(limit or price,latest.price,latest.mark_price or latest.price)
            minimum_price=min(price,latest.price,latest.mark_price or latest.price)
            qty=quantity(info,sizing_price,margin,leverage,cap,minimum_price)
            if side=='SELL' and self.c.short_split_tp:split_plan(info,qty,limit)
            tp=target(info,side,limit or price,pct)
            self.db.update(ident,'RESERVED',quantity=str(qty),tp=str(tp),market_price=str(price),
                           estimated_margin=str(qty*sizing_price/leverage),submitted_at=time.time(),
                           limit_price=str(limit) if limit is not None else None,
                           expires_at=time.time()+self.c.short_limit_seconds if limit is not None else None)
            if not self.capacity().within_limits:
                raise ValueError('ACCOUNT_CAPACITY_CHANGED_BEFORE_SUBMISSION')
            self.db.update(ident,'SUBMITTING')
            submitted=True
            order_id=(self.ex.create(pair,side,qty,leverage,mode,tp,limit_price=limit) if limit is not None
                      else self.ex.create(pair,side,qty,leverage,mode,tp))
            self.db.update(ident,'SUBMITTED',order_id=order_id)
            self.emit('LIMIT_SUBMITTED' if limit is not None else 'ORDER_SUBMITTED',pair=pair,side=side,trade_id=ident,
                      order_id=order_id,limit_price=str(limit) if limit is not None else None,
                      expires_in_seconds=self.c.short_limit_seconds if limit is not None else None)
        except Exception as error:
            rejected=not submitted or isinstance(error,ExchangeError) and error.rejected
            self.db.update(ident,'REJECTED' if rejected else 'UNCERTAIN',error_type=type(error).__name__)
            if isinstance(error,ExchangeError) and error.rejected:
                self.db.save_cache('rejection:'+pair,{'until':time.time()+900,'reason':str(error)})
            self.emit('ORDER_REJECTED' if rejected else 'ORDER_UNCERTAIN',pair=pair,trade_id=ident,
                      error_type=type(error).__name__,endpoint=getattr(error,'endpoint',''),http_status=getattr(error,'status',None),
                      reason=str(error) if isinstance(error,(ExchangeError,ValueError)) else '')

    def request_cancel(self,trade,order,reason):
        """Cancel only the known entry ID; bounded retries after fresh status reads."""
        d=trade['data'];now=time.time()
        attempts=d.get('cancel_attempts',1 if d.get('cancel_requested') else 0)
        if attempts and now-d.get('cancel_requested_at',d['submitted_at'])<30:return
        if attempts>=3:
            if not d.get('cancel_unconfirmed'):
                self.db.update(trade['id'],'UNCERTAIN',cancel_unconfirmed=True)
                self.emit('CANCEL_UNCERTAIN',pair=trade['pair'],trade_id=trade['id'],reason=reason)
            return
        self.db.update(trade['id'],'SUBMITTED',cancel_requested=True,cancel_requested_at=now,
                       cancel_attempts=attempts+1,cancel_reason=reason)
        self.ex.cancel(trade['pair'],order)
        if d.get('order_type')=='limit_order':
            self.emit('LIMIT_CANCEL_REQUESTED',pair=trade['pair'],trade_id=trade['id'],reason=reason)

    def reconcile(self,trade):
        ident,pair,d=trade['id'],trade['pair'],trade['data']
        status=trade['status']
        if status in ('CLOSED','REJECTED'):return
        if status=='CONFLICT' and not (d.get('side')=='SELL' and d.get('position_id') and d.get('reason') in RECOVERABLE):
            if d.get('average_order'):self.averager.cancel_own(trade,'POSITION_CONFLICT')
            return
        if status=='RESERVED':
            # Create cannot have been called before the SUBMITTING commit.
            self.db.update(ident,'REJECTED',reason='PREPARATION_INTERRUPTED')
            self.emit('ORDER_REJECTED',pair=pair,trade_id=ident,reason='PREPARATION_INTERRUPTED');return
        if status=='PNL_PENDING':
            self.reconcile_pnl(trade);return
        if d['side']=='SELL' and self.c.short_split_tp:
            if not self.short_tp.prepare(trade):return
            trade=self.db.get(ident);d=trade['data']
        if not d.get('order_id'):
            if status!='UNCERTAIN':
                self.db.update(ident,'UNCERTAIN');self.emit('ORDER_UNCERTAIN',pair=pair,trade_id=ident)
            return
        order=self.ex.find_order(pair,d['side'],d['order_id'])
        if order is None:
            if status!='UNCERTAIN':
                self.db.update(ident,'UNCERTAIN');self.emit('ORDER_UNCERTAIN',pair=pair,trade_id=ident)
            return
        filled=D(0) if order['status']=='rejected' else decimal(order['total_quantity'])-decimal(order['remaining_quantity'])-decimal(order.get('cancelled_quantity',0))
        if not 0<=filled<=decimal(d['quantity']):
            self.conflict(trade,'UNEXPECTED_ORDER_FILL');return
        if filled>0 and not d.get('fill_seen_at'):
            d={**d,'fill_seen_at':time.time()}
            self.db.update(ident,status,fill_seen_at=d['fill_seen_at'])
            trade=self.db.get(ident)
        if order['status'] not in ('filled','cancelled','partially_cancelled','rejected'):
            reason=None
            if d.get('cancel_requested'):
                reason=d.get('cancel_reason','CANCELLATION_PENDING')
            elif d.get('order_type')=='limit_order':
                if filled>0:reason='PARTIAL_FILL_REMAINDER'
                elif time.time()>=d.get('expires_at',d['submitted_at']+self.c.short_limit_seconds):reason='LIMIT_EXPIRED'
                elif not self.capacity().within_limits:reason='ACCOUNT_CAPACITY_CHANGED'
                elif any(decimal(p.get('active_pos') or 0)!=0 for p in self.ex.positions(pair)):
                    reason='POSITION_APPEARED_WHILE_ENTRY_PENDING'
            elif time.time()-d['submitted_at']>10:reason='MARKET_ORDER_TIMEOUT'
            if reason:self.request_cancel(trade,order,reason)
            return
        if filled==0:
            reason=d.get('cancel_reason','NO_FILL')
            self.db.update(ident,'REJECTED',reason=reason)
            event='LIMIT_CANCELLED' if d.get('order_type')=='limit_order' and order['status']!='rejected' else 'ORDER_REJECTED'
            self.emit(event,pair=pair,trade_id=ident,reason=reason);return
        positions=[p for p in self.ex.positions(pair) if decimal(p['active_pos'])!=0]
        initial_qty=filled
        fill=None
        if d['side']=='SELL' and d.get('position_id'):
            reconciled=self.averager.sync(trade,positions,order,initial_qty)
            if reconciled is None:return
            filled,fill=reconciled
            trade=self.db.get(ident);d=trade['data']
        if not positions:
            if d.get('position_id'):
                self.db.record_flat(ident,time.time())
                self.reconcile_pnl(self.db.get(ident))
            elif time.time()-d['fill_seen_at']>60:
                self.reconcile_pnl(trade)  # Can resolve an entry and TP completed before first poll.
                if self.db.get(ident)['status'] not in ('CLOSED','UNCERTAIN'):
                    self.db.update(ident,'UNCERTAIN')
                    self.emit('ORDER_UNCERTAIN',pair=pair,trade_id=ident)
            return
        p=positions[0]
        expected=filled*(1 if d['side']=='BUY' else -1)
        if (len(positions)!=1 or decimal(p['active_pos'])!=expected
                or d.get('position_id') and d['position_id']!=p['id']):
            # Brief position-endpoint lag after a fill does not justify another order.
            if not d.get('position_id') and time.time()-d['fill_seen_at']<60:return
            self.conflict(trade,'POSITION_QUANTITY_OR_ID_CHANGED');return
        if (str(p.get('margin_type','')).lower()!=d['mode'] or decimal(p.get('leverage',0))!=d['leverage']):
            self.conflict(trade,'MARGIN_MODE_OR_LEVERAGE_MISMATCH');return
        if status=='CLOSING':
            if time.time()-d.get('exit_requested_at',0)>30 and not d.get('exit_alerted'):
                self.db.update(ident,'CLOSING',exit_alerted=True)
                self.emit('EXIT_UNCERTAIN',pair=pair,trade_id=ident)
            return
        fill=fill if fill is not None else decimal(order.get('avg_price') or p['avg_price'],True)
        pct=self.c.long_tp if d['side']=='BUY' else self.c.short_tp
        tp=target(d['info'],d['side'],fill,pct)
        self.db.update(ident,'PROTECTING',position_id=p['id'],filled_quantity=str(filled),fill=str(fill),tp=str(tp),tp_pct=str(pct),
                       initial_quantity=str(initial_qty),initial_fill=str(decimal(order.get('avg_price') or p['avg_price'],True)))
        if not d.get('fill_alerted'):
            self.db.update(ident,'PROTECTING',fill_alerted=True)
            self.emit('ENTRY_FILLED',pair=pair,side=d['side'],trade_id=ident,quantity=str(filled),
                      fill=str(fill),tp=str(tp),leverage=d['leverage'],mode=d['mode'],margin=str(filled*fill/d['leverage']),
                      profit_plan='75% at 7%, remainder at 20%' if d['side']=='SELL' and self.c.short_split_tp else 'Full position at TP')
        if decimal(p.get('stop_loss_trigger') or 0)!=0:
            self.conflict(self.db.get(ident),'UNEXPECTED_EXISTING_STOP_LOSS');return
        if d['side']=='SELL' and self.c.short_split_tp and self.short_tp.manage(self.db.get(ident),p):
            self.averager.maybe_place(self.db.get(ident));return
        if decimal(p.get('take_profit_trigger') or 0)!=tp:
            # If the requested profit is already executable, take it once at market.
            exit_price=self.ex.price(pair,'SELL' if d['side']=='BUY' else 'BUY')
            hit=exit_price>=tp if d['side']=='BUY' else exit_price<=tp
            if hit:
                if d['side']=='SELL' and not self.averager.cancel_own(trade,'TP_EXIT'):return
                self.db.update(ident,'CLOSING',exit_requested_at=time.time())
                self.ex.exit(pair,p)
                self.emit('TP_EXIT_REQUESTED',pair=pair,trade_id=ident);return
            self.ex.take_profit(pair,p,tp)
            refreshed=next((x for x in self.ex.positions(pair) if x['id']==p['id']),None)
            if refreshed is None or decimal(refreshed['active_pos'])==0:return
            if (decimal(refreshed['active_pos'])!=expected or refreshed.get('margin_type')!=d['mode']
                    or decimal(refreshed.get('leverage',0))!=d['leverage']):
                self.conflict(self.db.get(ident),'POSITION_CHANGED_DURING_TP_UPDATE');return
            if decimal(refreshed.get('take_profit_trigger') or 0)!=tp:
                self.emit('TP_PENDING',pair=pair,trade_id=ident);return
        self.db.update(ident,'OPEN')
        if d['side']=='SELL':self.averager.maybe_place(self.db.get(ident))

    def conflict(self,trade,reason):
        self.db.update(trade['id'],'CONFLICT',reason=reason)
        self.emit('OWNERSHIP_CONFLICT',pair=trade['pair'],trade_id=trade['id'],reason=reason)

    def reconcile_pnl(self,trade):
        d=trade['data'];pair=trade['pair']
        rows=self.ledger(d['submitted_at']-2)
        matching=[r for r in rows if r.get('pair')==pair and r.get('stage')!='funding'
                  and (not d.get('position_id') or r.get('position_id')==d['position_id'])
                  and 'Order' in r.get('parent_type','')]
        entry_ids={d.get('order_id'),d.get('average_order',{}).get('order_id'),*d.get('manual_entries',{}),
                   *[a.get('order_id') for a in d.get('average_history',[])]}
        entries=[r for r in matching if r.get('parent_id') in entry_ids]
        exits=[r for r in matching if r.get('parent_id') not in entry_ids]
        if not entries or not exits:return
        if any(decimal(p.get(k,0))!=0 for p in self.ex.positions(pair)
               for k in ('active_pos','inactive_pos_buy','inactive_pos_sell')):return
        pnl=sum([decimal(r.get('amount',0))-abs(decimal(r.get('fee_amount',0))) for r in matching],D(0))
        closed_at=max(float(r['created_at'])/1000 for r in exits)
        self.db.record_flat(trade['id'],closed_at,pnl)
        self.emit('EXIT_CONFIRMED',pair=pair,trade_id=trade['id'],realized_pnl=str(pnl))
