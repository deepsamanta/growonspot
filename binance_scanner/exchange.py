"""Binance order/account adapter. Internal pair names match the shared engine."""
import time
import uuid
from decimal import Decimal as D
from scanner.exchange import Exchange as BaseExchange, nongold
from scanner.market import decimal, DAY, MarketDataError
from .client import BinanceError
from .market import pair_name, symbol_name
from .history import FillHistory

TERMINAL={'filled','cancelled','partially_cancelled','rejected'}
STATUS={'NEW':'open','PARTIALLY_FILLED':'partially_filled','FILLED':'filled','CANCELED':'cancelled',
        'CANCELLED':'cancelled','EXPIRED':'cancelled','EXPIRED_IN_MATCH':'cancelled','REJECTED':'rejected',
        'TRIGGERED':'open','TRIGGERING':'open','FINISHED':'filled'}


def normalize(row,algo=False):
    symbol=row['symbol'];pair=pair_name(symbol)
    if row.get('positionSide','BOTH')!='BOTH':raise BinanceError('Binance Hedge Mode is not supported')
    raw_status=row['algoStatus' if algo else 'status']
    if raw_status not in STATUS:raise BinanceError('Unknown Binance order status: '+raw_status)
    status=STATUS[raw_status];qty=decimal(row.get('quantity' if algo else 'origQty',0));filled=decimal(row.get('executedQty',0))
    if qty<0 or filled<0 or filled>qty:raise BinanceError('Invalid Binance executed quantity')
    cancelled=qty-filled if status in ('cancelled','rejected') else D(0)
    if status=='cancelled' and filled:status='partially_cancelled'
    typ=row.get('orderType' if algo else 'type','')
    close_all=row.get('closePosition') in (True,'true')
    own=str(row.get('clientAlgoId' if algo else 'clientOrderId','')).startswith('gosb_')
    return dict(id=('algo:' if algo else '')+str(row['algoId' if algo else 'orderId']),pair=pair,
        side=row['side'].lower(),status=status,total_quantity=str(qty),remaining_quantity=str(qty-filled-cancelled),
        cancelled_quantity=str(cancelled),avg_price=str(row.get('avgPrice',0)),price=str(row.get('price',0)),
        stop_price=str(row.get('triggerPrice' if algo else 'stopPrice',0)),
        order_type={'LIMIT':'limit_order','MARKET':'market_order','TAKE_PROFIT_MARKET':'take_profit_market',
                    'STOP_MARKET':'stop_market'}.get(typ,typ.lower()),
        stage='tpsl_exit' if algo else 'default',order_category='complete_tpsl' if algo and close_all and own else 'external',
        created_at=row.get('createTime' if algo else 'time',row.get('updateTime',0)),
        reduce_only=row.get('reduceOnly') in (True,'true'),close_all=close_all,binance_symbol=symbol)


class Exchange(BaseExchange):
    def __init__(self,config,market,state,client=None):
        self.c,self.market,self.db=config,market,state
        self.client=client or market.client
        self.history=FillHistory(self.client,state)
        self.account_checked=0
        self.fee_checked=0

    def fee_mode(self):
        if time.time()-self.fee_checked<300:return
        data=self.client.call('GET','/fapi/v1/feeBurn')
        if data.get('feeBurn') is not False:
            raise BinanceError('Disable Futures BNB fee payment before entries; USDT fee accounting is required')
        self.fee_checked=time.time()

    def request(self,*args,**kwargs):
        raise RuntimeError('CoinDCX requests are disabled in the Binance adapter')

    def account_mode(self):
        if time.time()-self.account_checked<30:return
        dual=self.client.call('GET','/fapi/v1/positionSide/dual')
        multi=self.client.call('GET','/fapi/v1/multiAssetsMargin')
        if dual.get('dualSidePosition') is not False:raise BinanceError('Set Binance Futures to One-way Mode; bot will not change account mode')
        if multi.get('multiAssetsMargin') is not False:raise BinanceError('Set Binance Futures to Single-Asset Mode for isolated longs')
        self.account_checked=time.time()

    def wallet_balance(self):
        rows=self.client.call('GET','/fapi/v3/balance')
        if not isinstance(rows,list):raise BinanceError('Invalid Binance balances')
        found=[r for r in rows if r.get('asset')=='USDT']
        if len(found)!=1:raise BinanceError('USDT futures wallet unavailable')
        return decimal(found[0]['balance'])

    def _raw_orders(self,symbol=None):
        params={'symbol':symbol} if symbol else {}
        regular=self.client.call('GET','/fapi/v1/openOrders',params)
        algos=self.client.call('GET','/fapi/v1/openAlgoOrders',params)
        if not isinstance(regular,list) or not isinstance(algos,list):raise BinanceError('Invalid Binance open order list')
        return [normalize(o) for o in regular if o['symbol'].endswith('USDT')]+[normalize(o,True) for o in algos if o['symbol'].endswith('USDT')]

    def _positions(self,pair=None):
        self.account_mode()
        symbol=symbol_name(pair) if pair else None
        raw=self.client.call('GET','/fapi/v2/positionRisk',{'symbol':symbol} if symbol else {})
        if not isinstance(raw,list):raise BinanceError('Invalid Binance position list')
        orders=self._raw_orders(symbol);result={}
        for p in raw:
            if not p['symbol'].endswith('USDT'):continue
            if p.get('positionSide')!='BOTH':raise BinanceError('Hedge positions detected; no account mutation')
            name=pair_name(p['symbol'])
            result[name]=dict(id=p['symbol']+':BOTH',pair=name,active_pos=p['positionAmt'],avg_price=p['entryPrice'],
                margin_type='isolated' if p['marginType']=='isolated' else 'crossed',leverage=p['leverage'],
                inactive_pos_buy='0',inactive_pos_sell='0',take_profit_trigger='0',stop_loss_trigger='0')
        for o in orders:
            if o['pair'] not in result:
                raise BinanceError('Pending Binance order has no position configuration')
            p=result[o['pair']];active=decimal(p['active_pos'])
            closing=o['side']==('sell' if active>0 else 'buy') and active!=0
            if o['close_all'] and closing:
                key='take_profit_trigger' if o['order_type']=='take_profit_market' else 'stop_loss_trigger'
                if decimal(p[key])!=0:raise BinanceError('Multiple native closing triggers need review')
                p[key]=o['stop_price']
            # Reduce-only/close-all orders cannot consume new-entry slots.
            if not o['reduce_only'] and not o['close_all']:
                key='inactive_pos_'+o['side']
                p[key]=str(decimal(p[key])+decimal(o['remaining_quantity']))
        return list(result.values())

    def positions(self,pair):
        nongold(pair);return [p for p in self._positions(pair) if p['pair']==pair]

    def all_positions(self):return self._positions()

    def orders(self,pair,side,status='open,partially_filled,untriggered'):
        nongold(pair)
        return [o for o in self._raw_orders(symbol_name(pair)) if o['pair']==pair and o['side']==side.lower() and o['status'] in status.split(',')]

    def find_order(self,pair,side,order_id):
        nongold(pair);order_id=str(order_id)
        cache_key='binance:order:'+symbol_name(pair)+':'+order_id
        saved=self.db.cache(cache_key)
        # Binance prunes terminal order queries after 90 days. Persist final
        # responses so a long-lived position remains reconcilable after that.
        if saved and saved['status'] in TERMINAL:order=saved
        else:
            algo=order_id.startswith('algo:')
            path='/fapi/v1/algoOrder' if algo else '/fapi/v1/order'
            params={'algoId':order_id[5:]} if algo else {'symbol':symbol_name(pair),'orderId':order_id}
            try:order=normalize(self.client.call('GET',path,params),algo)
            except BinanceError as error:
                if error.code in (-2013,-2011):return None
                raise
            self.db.save_cache(cache_key,order)
        if order['pair']!=pair or order['side']!=side.lower():raise BinanceError('Binance order ownership mismatch')
        return order

    def price(self,pair,side):nongold(pair);return self.market.price(pair,side)

    def prepare(self,pair,leverage,mode):
        nongold(pair);self.account_mode();positions=self.positions(pair)
        if any(decimal(p[k]) for p in positions for k in ('active_pos','inactive_pos_buy','inactive_pos_sell')) or self._raw_orders(symbol_name(pair)):
            raise BinanceError('Binance symbol became occupied before entry')
        current=positions[0] if positions else {}
        if current.get('margin_type')!=mode:
            try:self.client.call('POST','/fapi/v1/marginType',{'symbol':symbol_name(pair),'marginType':'ISOLATED' if mode=='isolated' else 'CROSSED'})
            except BinanceError as e:
                if e.code!=-4046:raise
        if decimal(current.get('leverage',0))!=leverage:
            self.client.call('POST','/fapi/v1/leverage',{'symbol':symbol_name(pair),'leverage':leverage})

    def _submit(self,pair,side,qty,price=None,reduce=False):
        nongold(pair)
        if not self.c.enabled and not reduce:raise BinanceError('Binance entries are disabled')
        self.account_mode()
        if not reduce:self.fee_mode()
        try:self.market.validate_order(pair,side,qty,price,reduce)
        except MarketDataError as error:
            raise BinanceError(str(error),'local_order_validation',400) from None
        client_id='gosb_'+uuid.uuid4().hex[:28]
        payload=dict(symbol=symbol_name(pair),side=side,positionSide='BOTH',type='LIMIT' if price is not None else 'MARKET',
                     quantity=format(decimal(qty,True),'f'),newClientOrderId=client_id,newOrderRespType='RESULT')
        if price is not None:payload.update(price=format(decimal(price,True),'f'),timeInForce='GTC')
        if reduce:payload['reduceOnly']='true'
        self.db.save_cache('binance:intent:'+client_id,{'payload':payload,'state':'submitting'})
        try:raw=self.client.call('POST','/fapi/v1/order',payload)
        except BinanceError as error:
            if error.rejected:raise
            # Recover a lost acknowledgement by client ID, never by POSTing again.
            try:raw=self.client.call('GET','/fapi/v1/order',{'symbol':symbol_name(pair),'origClientOrderId':client_id})
            except BinanceError:raise error from None
        order=normalize(raw)
        if order['pair']!=pair or order['side']!=side.lower() or decimal(order['total_quantity'])!=decimal(qty):
            raise BinanceError('Ambiguous Binance order acknowledgement')
        self.db.save_cache('binance:order:'+symbol_name(pair)+':'+order['id'],order)
        self.db.save_cache('binance:intent:'+client_id,{'payload':payload,'state':'acknowledged','order_id':order['id']})
        return order['id']

    def create(self,pair,side,qty,leverage,mode,tp,limit_price=None):
        if (side,leverage,mode) not in (('SELL',3,'crossed'),('BUY',1,'isolated')):raise ValueError('Unexpected Binance entry settings')
        if side=='SELL' and limit_price is None:raise ValueError('Binance shorts require a resistance limit')
        return self._submit(pair,side,qty,limit_price)

    def average_long(self,pair,qty):return self._submit(pair,'BUY',qty)
    def partial_short_limit(self,pair,qty,leverage,price):return self._submit(pair,'BUY',qty,price,True)
    def partial_long_limit(self,pair,qty,price):return self._submit(pair,'SELL',qty,price,True)
    def partial_short_exit(self,pair,qty,leverage):return self._submit(pair,'BUY',qty,reduce=True)

    def cancel(self,pair,order):
        nongold(pair)
        if order['pair']!=pair:raise BinanceError('Cancel ownership mismatch')
        oid=order['id'];algo=oid.startswith('algo:')
        self.client.call('DELETE','/fapi/v1/algoOrder' if algo else '/fapi/v1/order',
                         {'algoId':oid[5:]} if algo else {'symbol':symbol_name(pair),'orderId':oid})

    def take_profit(self,pair,position,tp):
        nongold(pair)
        if position['pair']!=pair:raise BinanceError('TP ownership mismatch')
        side='SELL' if decimal(position['active_pos'])>0 else 'BUY'
        existing=self.orders(pair,side)
        matching=[o for o in existing if o['order_type']=='take_profit_market' and o['close_all'] and decimal(o['stop_price'])==tp]
        if len(existing)==1 and len(matching)==1:return
        if existing:raise BinanceError('Existing closing order blocks Binance TP replacement')
        trade=next((r for r in self.db.occupied() if r['pair']==pair),None)
        if trade is None:raise BinanceError('No Binance-owned trade for TP')
        key='binance:tp-intent:'+str(trade['id'])+':'+str(tp)
        intent=self.db.cache(key)
        if intent:
            try:raw=self.client.call('GET','/fapi/v1/algoOrder',{'clientAlgoId':intent['client_id']})
            except BinanceError:
                # Even not-found may reflect publication lag after a timeout.
                raise BinanceError('Unconfirmed Binance TP intent; no duplicate submission') from None
            previous=normalize(raw,True)
            if previous['status'] not in ('cancelled','rejected'):return
        client_id='gosb_'+uuid.uuid4().hex[:28]
        self.db.save_cache(key,{'client_id':client_id,'target':str(tp)})
        params=dict(algoType='CONDITIONAL',symbol=symbol_name(pair),side=side,positionSide='BOTH',
                    type='TAKE_PROFIT_MARKET',triggerPrice=format(tp,'f'),workingType='CONTRACT_PRICE',
                    closePosition='true',clientAlgoId=client_id)
        raw=self.client.call('POST','/fapi/v1/algoOrder',params)
        order=normalize(raw,True)
        if order['pair']!=pair or order['side']!=side.lower():raise BinanceError('Invalid Binance TP acknowledgement')
        self.db.save_cache('binance:order:'+symbol_name(pair)+':'+order['id'],order)

    def exit(self,pair,position):
        nongold(pair)
        if position['pair']!=pair:raise BinanceError('Exit ownership mismatch')
        q=decimal(position['active_pos']);return self._submit(pair,'SELL' if q>0 else 'BUY',abs(q),reduce=True)

    def trade_fills(self,pair,since,known_order_ids=()):
        nongold(pair)
        rows=self.history.fills(pair,since)
        return [dict(pair=pair,order_id=str(r['orderId']),side=r['side'].lower(),quantity=r['qty'],
                     price=r['price'],timestamp=r['time']) for r in rows]

    def transactions(self,since):
        result=[]
        # Account-wide profitable exits enforce the same daily lockout, including
        # manual coins. Fixed endTime and transaction IDs prevent double counting.
        end=int(time.time()*1000);start=max(int(since*1000),end-7*DAY*1000+1);seen=set()
        for page in range(1,101):
            rows=self.client.call('GET','/fapi/v1/income',dict(incomeType='REALIZED_PNL',startTime=start,endTime=end,limit=1000,page=page))
            if not isinstance(rows,list):raise BinanceError('Invalid Binance income history')
            added=False
            for r in rows:
                key=(r['tranId'],r['incomeType'])
                if key in seen:continue
                seen.add(key);added=True
                if r.get('asset')=='USDT' and r.get('symbol','').endswith('USDT'):
                    result.append(dict(pair=pair_name(r['symbol']),stage='realized',amount=r['income'],fee_amount='0',
                        created_at=r['time'],parent_type='Income',parent_id=str(r['tranId'])))
            if len(rows)<1000:break
            if not added:raise BinanceError('Repeated Binance income page')
        else:raise BinanceError('Incomplete Binance income history')
        for t in self.db.rows():
            if t['status'] in ('CLOSED','REJECTED'):continue
            pair=t['pair'];d=t['data']
            for r in self.history.fills(pair,max(since,d['submitted_at']-2)):
                fee=decimal(r['commission'])
                if fee and r['commissionAsset']!='USDT':
                    raise BinanceError('Non-USDT trade fee requires review; disable Futures BNB fee payment')
                result.append(dict(pair=pair,position_id=symbol_name(pair)+':BOTH',
                    stage='default' if r['side']==d['side'] else 'tpsl_exit',parent_type='BinanceOrder',parent_id=str(r['orderId']),
                    created_at=r['time'],amount=r['realizedPnl'],fee_amount=str(fee),margin_currency_short_name='USDT'))
        return result
