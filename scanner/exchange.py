"""CoinDCX scanner adapter. All mutations are scoped to a non-gold pair."""
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP
import requests
from .market import MarketDataError, decimal

BASE='https://api.coindcx.com/exchange/v1/derivatives/futures/'
D=Decimal
SHORT_RESISTANCE_OFFSET=D('0.02')


class ExchangeError(RuntimeError):
    def __init__(self,message,endpoint='',status=None):
        super().__init__(message)
        self.endpoint,self.status=endpoint,status

    @property
    def rejected(self):
        return self.status in (400,401,403,404,422)


def nongold(pair):
    if not isinstance(pair,str) or not pair.endswith('_USDT') or pair.split('-',1)[-1].split('_')[0] in ('XAU','XAUUSD','XAUUSDT'):
        raise ValueError('Scanner pair is invalid or reserved for gold')


def quantity(info, price, margin, leverage, cap=None, minimum_price=None):
    price=decimal(price,True);budget=margin*leverage
    minimum_price=price if minimum_price is None else decimal(minimum_price,True)
    step=decimal(info['quantity_increment'],True)
    q=(budget/price/step).to_integral_value(rounding=ROUND_DOWN)*step
    minimum=max(decimal(info['min_quantity'],True),decimal(info.get('min_trade_size',info['min_quantity']),True))
    min_notional=decimal(info['min_notional'],True)
    if cap is not None and (q<minimum or q*minimum_price<min_notional):
        q=(max(minimum,min_notional/minimum_price)/step).to_integral_value(rounding=ROUND_UP)*step
    allowed=margin if cap is None else cap
    if q<minimum or q*minimum_price<min_notional or q*price/leverage>allowed:
        raise ValueError('EXCHANGE_MINIMUM_EXCEEDS_MARGIN_CAP')
    if q>min(decimal(info['max_quantity'],True),decimal(info['max_market_order_quantity'],True)):
        raise ValueError('EXCHANGE_MAXIMUM_QUANTITY')
    tiers=info.get('dynamic_position_leverage_details',{})
    supported=[decimal(k,True) for k,v in tiers.items() if decimal(v,True)>=q*price]
    if not supported or leverage>max(supported):
        raise ValueError('LEVERAGE_NOT_SUPPORTED')
    return q


def target(info,side,fill,pct):
    step=decimal(info['price_increment'],True)
    raw=fill*(1-pct if side=='SELL' else 1+pct)
    tp=(raw/step).to_integral_value(rounding=ROUND_DOWN if side=='SELL' else ROUND_UP)*step
    if not decimal(info['min_price'],True)<=tp<=decimal(info['max_price'],True):
        raise ValueError('TP_OUTSIDE_EXCHANGE_PRICE_RANGE')
    return tp


def short_limit_price(info,resistance,last_price):
    """Sell limit 2% above resistance, rounded upward to an exchange tick."""
    step=decimal(info['price_increment'],True)
    raw=decimal(resistance,True)*(1+SHORT_RESISTANCE_OFFSET)
    price=(raw/step).to_integral_value(rounding=ROUND_UP)*step
    if not decimal(info['min_price'],True)<=price<=decimal(info['max_price'],True):
        raise ValueError('LIMIT_OUTSIDE_EXCHANGE_PRICE_RANGE')
    # Check both published LTP-based bounds before sending a limit order.
    if info.get('multiplier_up') is not None and price>last_price*(1+decimal(info['multiplier_up'])/100):
        raise ValueError('LIMIT_OUTSIDE_EXCHANGE_LTP_RANGE')
    if info.get('multiplier_down') is not None and price<last_price*(1-decimal(info['multiplier_down'])/100):
        raise ValueError('LIMIT_OUTSIDE_EXCHANGE_LTP_RANGE')
    return price


class Exchange:
    def __init__(self,config,market,session=None):
        self.c,self.market=config,market
        self.http=session or requests.Session()

    def request(self,path,body,read=False,method='POST'):
        if method not in ('GET','POST') or method=='GET' and not read:raise ValueError('Invalid request method')
        for attempt in range(3 if read else 1):
            payload=json.dumps({**body,'timestamp':int(time.time()*1000)},separators=(',',':'))
            signature=hmac.new(self.c.secret.encode(),payload.encode(),hashlib.sha256).hexdigest()
            try:
                send=self.http.get if method=='GET' else self.http.post
                r=send(BASE+path,data=payload,headers={'Content-Type':'application/json',
                    'X-AUTH-APIKEY':self.c.key,'X-AUTH-SIGNATURE':signature},timeout=(5,15))
                if not r.ok:
                    if read and attempt<2 and (r.status_code==429 or r.status_code>=500):
                        time.sleep(2**attempt);continue
                    detail=''
                    try:
                        parsed=r.json()
                        if isinstance(parsed,dict):detail=str(parsed.get('message') or parsed.get('error') or '')
                    except ValueError:pass
                    for secret in (self.c.key,self.c.secret,getattr(self.c,'bot_token','')):
                        if isinstance(secret,str) and secret:detail=detail.replace(secret,'[redacted]')
                    raise ExchangeError(f'CoinDCX HTTP {r.status_code}: {detail[:300]}',path,r.status_code)
                data=r.json()
                if isinstance(data,dict) and (data.get('success') is False or str(data.get('status','')).lower() in ('error','failed')
                        or str(data.get('code','')).isdigit() and int(data['code'])>=400):
                    raise ExchangeError('CoinDCX error response',path,r.status_code)
                return data
            except (requests.RequestException,ValueError) as error:
                if not read or attempt==2:
                    raise ExchangeError(type(error).__name__,path) from None
                time.sleep(2**attempt)
        raise ExchangeError('Read retries exhausted',path)

    def pages(self,path,extra):
        seen=set()
        for page in range(1,101):
            rows=self.request(path,{'page':str(page),'size':'100','margin_currency_short_name':['USDT'],**extra},read=True)
            if not isinstance(rows,list):
                raise ExchangeError('Invalid list response',path)
            fingerprint=hashlib.sha256(json.dumps(rows,sort_keys=True).encode()).hexdigest()
            if rows and fingerprint in seen:raise ExchangeError('Repeated pagination page',path)
            seen.add(fingerprint)
            yield rows
            if len(rows)<100:return
        raise ExchangeError('Incomplete pagination; entries blocked',path)

    def positions(self,pair):
        nongold(pair)
        return [p for page in self.pages('positions',{'pairs':pair}) for p in page if p.get('pair')==pair]

    def all_positions(self):
        return [p for page in self.pages('positions',{}) for p in page]

    def wallet_balance(self):
        details=self.request('positions/cross_margin_details',{},read=True,method='GET')
        if not isinstance(details,dict) or 'total_wallet_balance' not in details:
            raise ExchangeError('Total USDT wallet balance unavailable','positions/cross_margin_details')
        value=decimal(details['total_wallet_balance'])
        if value<0:raise ExchangeError('Invalid total USDT wallet balance','positions/cross_margin_details')
        return value

    def orders(self,pair,side,status='open,partially_filled,untriggered'):
        nongold(pair)
        return [o for page in self.pages('orders',{'side':side.lower(),'status':status}) for o in page if o.get('pair')==pair]

    def find_order(self,pair,side,order_id):
        nongold(pair)
        for page in self.pages('orders',{'side':side.lower(),'status':'filled,cancelled,rejected,partially_cancelled,open,partially_filled,untriggered'}):
            for order in page:
                if order.get('id')==order_id:
                    if order.get('pair')!=pair or order.get('side')!=side.lower():
                        raise ExchangeError('Order ownership mismatch','orders')
                    return order
        return None

    def recent_entries(self,pair,side,since):
        """Aggregate actual fills using the pair/date-filtered trades endpoint.

        Orders are sorted by update time, not creation time, and account-wide
        history can be enormous. No order-history sorting assumption is needed.
        The caller separately verifies each addition against the position ledger.
        """
        grouped={}
        for fill in self.trade_fills(pair,since):
            if fill['side']!=side.lower():continue
            oid=fill['order_id'];qty=decimal(fill['quantity'],True);price=decimal(fill['price'],True)
            item=grouped.setdefault(oid,{'qty':D(0),'cost':D(0),'stamp':float(fill['timestamp'])})
            item['qty']+=qty;item['cost']+=qty*price
        return [{'id':oid,'pair':pair,'side':side.lower(),'stage':'default','status':'filled',
                 'total_quantity':str(v['qty']),'remaining_quantity':'0','cancelled_quantity':'0',
                 'avg_price':str(v['cost']/v['qty']),'created_at':v['stamp']} for oid,v in grouped.items()]

    def trade_fills(self,pair,since):
        nongold(pair)
        start=datetime.fromtimestamp(since,timezone.utc).date().isoformat()
        end=(datetime.now(timezone.utc)+timedelta(days=1)).date().isoformat()
        return [f for page in self.pages('trades',{'pair':pair,'from_date':start,'to_date':end}) for f in page
                if f.get('pair')==pair and float(f['timestamp'])/1000>=since]

    def transactions(self,since):
        result=[]
        last_time=float('inf')
        ordered=True
        for page in self.pages('positions/transactions',{'stage':'all'}):
            for row in page:
                stamp=float(row['created_at'])/1000
                if stamp>last_time:ordered=False
                last_time=stamp
                if stamp>=since and row.get('margin_currency_short_name','USDT')=='USDT':result.append(row)
            if page and ordered and max(float(r['created_at'])/1000 for r in page)<since:
                return result
        return result

    def price(self,pair,side):
        nongold(pair)
        book=self.market.get('https://public.coindcx.com/market_data/v3/orderbook/'+pair+'-futures/50')
        if not -30<=time.time()-float(book['ts'])/1000<=30:raise MarketDataError('Stale order book')
        levels=[decimal(p,True) for p,q in book['asks' if side=='BUY' else 'bids'].items() if decimal(q)>0]
        if not levels:raise MarketDataError('Empty order book')
        return min(levels) if side=='BUY' else max(levels)

    def prepare(self,pair,leverage,mode):
        nongold(pair)
        positions=self.positions(pair)
        if len(positions)>1 or any(decimal(p.get(k,0))!=0 for p in positions
                for k in ('active_pos','inactive_pos_buy','inactive_pos_sell')):
            raise ExchangeError('Position became occupied before preparation')
        current=positions[0] if positions else {}
        if current.get('margin_type')!=mode:
            self.request('positions/margin_type',{'pair':pair,'margin_type':mode})
        if decimal(current.get('leverage',0))!=leverage:
            self.request('positions/update_leverage',{'pair':pair,'leverage':leverage,'margin_currency_short_name':'USDT'})

    def create(self,pair,side,qty,leverage,mode,tp,limit_price=None):
        nongold(pair)
        if limit_price is not None and side!='SELL':raise ValueError('Only scanner shorts use entry limits')
        order={'pair':pair,'side':side.lower(),'order_type':'limit_order' if limit_price is not None else 'market_order',
            'price':float(decimal(limit_price,True)) if limit_price is not None else None,
            'total_quantity':float(qty),'leverage':leverage,'position_margin_type':mode,
            'margin_currency_short_name':'USDT','notification':'no_notification'}
        if limit_price is not None:
            order['time_in_force']='good_till_cancel'
            # The future short TP can lie above today's market while the entry
            # rests higher still. Attach position TP after the fill, not as a
            # currently invalid trigger on an unfilled entry.
        else:order['take_profit_price']=float(tp)
        result=self.request('orders/create',{'order':order})
        rows=result if isinstance(result,list) else result.get('order',[]) if isinstance(result,dict) else []
        if isinstance(rows,dict):rows=[rows]
        if len(rows)!=1 or not rows[0].get('id'):raise ExchangeError('Ambiguous order acknowledgement','orders/create')
        return rows[0]['id']

    def cancel(self,pair,order):
        nongold(pair)
        if order.get('pair')!=pair:raise ExchangeError('Cancel ownership mismatch')
        self.request('orders/cancel',{'id':order['id']})

    def partial_short_exit(self,pair,qty,leverage):
        nongold(pair)
        order={'pair':pair,'side':'buy','order_type':'market_order','price':None,
               'total_quantity':float(decimal(qty,True)),'leverage':leverage,'position_margin_type':'crossed',
               'margin_currency_short_name':'USDT','notification':'no_notification'}
        result=self.request('orders/create',{'order':order})
        rows=result if isinstance(result,list) else result.get('order',[]) if isinstance(result,dict) else []
        if isinstance(rows,dict):rows=[rows]
        if len(rows)!=1 or not rows[0].get('id'):raise ExchangeError('Ambiguous partial exit acknowledgement','orders/create')
        return rows[0]['id']

    def partial_short_limit(self,pair,qty,leverage,price):
        nongold(pair)
        order={'pair':pair,'side':'buy','order_type':'limit_order','price':float(decimal(price,True)),
               'total_quantity':float(decimal(qty,True)),'leverage':leverage,'position_margin_type':'crossed',
               'margin_currency_short_name':'USDT','notification':'no_notification','time_in_force':'good_till_cancel'}
        result=self.request('orders/create',{'order':order})
        rows=result if isinstance(result,list) else result.get('order',[]) if isinstance(result,dict) else []
        if isinstance(rows,dict):rows=[rows]
        if len(rows)!=1 or not rows[0].get('id'):raise ExchangeError('Ambiguous partial limit acknowledgement','orders/create')
        return rows[0]['id']

    def average_long(self,pair,qty):
        nongold(pair)
        # Keep the existing TP until the combined fill is verified and repriced.
        order={'pair':pair,'side':'buy','order_type':'market_order','price':None,
               'total_quantity':float(decimal(qty,True)),'leverage':1,'position_margin_type':'isolated',
               'margin_currency_short_name':'USDT','notification':'no_notification'}
        result=self.request('orders/create',{'order':order})
        rows=result if isinstance(result,list) else result.get('order',[]) if isinstance(result,dict) else []
        if isinstance(rows,dict):rows=[rows]
        if len(rows)!=1 or not rows[0].get('id'):raise ExchangeError('Ambiguous long averaging acknowledgement','orders/create')
        return rows[0]['id']

    def take_profit(self,pair,position,tp):
        nongold(pair)
        if position.get('pair')!=pair:raise ExchangeError('TP ownership mismatch')
        # Deliberately no stop_loss / stop_loss_price field anywhere in scanner orders.
        return self.request('positions/create_tpsl',{'id':position['id'],
            'take_profit':{'stop_price':str(tp),'order_type':'take_profit_market'}})

    def exit(self,pair,position):
        nongold(pair)
        if position.get('pair')!=pair:raise ExchangeError('Exit ownership mismatch')
        self.request('positions/exit',{'id':position['id']})
