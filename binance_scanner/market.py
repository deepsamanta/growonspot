"""Binance public market data normalized for the unchanged scanner strategy."""
import time
from scanner.market import MarketData as BaseMarket, MarketDataError, Quote, Candle, DAY, decimal, GOLD
from .client import Client


def pair_name(symbol):
    if not isinstance(symbol, str) or not symbol.endswith('USDT') or not symbol[:-4].isalnum():
        raise ValueError('Only Binance USDT symbols are supported')
    return 'B-'+symbol[:-4]+'_USDT'


def symbol_name(pair):
    if not isinstance(pair, str) or not pair.startswith('B-') or not pair.endswith('_USDT'):
        raise ValueError('Invalid Binance pair')
    symbol=pair[2:-5]+'USDT'
    if pair_name(symbol)!=pair:raise ValueError('Invalid Binance pair')
    return symbol


class MarketData(BaseMarket):
    def __init__(self, client=None, clock=time.time):
        self.client=client or Client();self.clock=clock
        self.symbols={};self.info_at=0;self.tiers={}

    def instruments(self):
        if not self.symbols or self.clock()-self.info_at>=300:
            data=self.client.call('GET','/fapi/v1/exchangeInfo',signed=False)
            if not isinstance(data,dict) or not isinstance(data.get('symbols'),list):
                raise MarketDataError('Invalid Binance exchange information')
            self.symbols={pair_name(s['symbol']):s for s in data['symbols']
                          if s.get('quoteAsset')=='USDT' and s.get('marginAsset')=='USDT'
                          and s.get('contractType')=='PERPETUAL'}
            self.info_at=self.clock()
        return self.symbols

    def active_instruments(self):
        return sorted(p for p,s in self.instruments().items() if s['status']=='TRADING'
                      and s['baseAsset'] not in GOLD and s.get('underlyingType','COIN')=='COIN')

    def metadata(self,pair):
        s=self.instruments().get(pair)
        if s is None:raise MarketDataError('Unknown Binance USDT perpetual')
        f={r['filterType']:r for r in s['filters']};lot=f['LOT_SIZE'];market=f.get('MARKET_LOT_SIZE',lot);price=f['PRICE_FILTER']
        # The shared quantity checker handles a common step. Refuse incompatible
        # filters instead of submitting an invalid market order.
        step=max(decimal(lot['stepSize'],True),decimal(market['stepSize'],True))
        if step%decimal(lot['stepSize'],True) or step%decimal(market['stepSize'],True):
            raise MarketDataError('Incompatible Binance quantity steps')
        tiers=self.tiers.get(pair)
        if tiers is None or self.clock()-tiers[0]>3600:
            rows=self.client.call('GET','/fapi/v1/leverageBracket',{'symbol':s['symbol']})
            row=rows[0] if isinstance(rows,list) and len(rows)==1 else rows
            if not isinstance(row,dict) or row.get('symbol')!=s['symbol']:raise MarketDataError('Invalid leverage brackets')
            values={str(b['initialLeverage']):str(b['notionalCap']) for b in row['brackets']}
            if not values:raise MarketDataError('Missing Binance leverage brackets')
            tiers=(self.clock(),values);self.tiers[pair]=tiers
        percent=f.get('PERCENT_PRICE',{})
        return dict(pair=pair,underlying_currency_short_name=s['baseAsset'],position_currency_short_name=s['baseAsset'],
            status='active' if s['status']=='TRADING' else 'inactive',quote_currency_short_name='USDT',unit_contract_value=1,
            exit_only=s.get('underlyingType','COIN')!='COIN',quantity_increment=str(step),
            min_quantity=str(max(decimal(lot['minQty']),decimal(market['minQty']))),min_trade_size=lot['minQty'],
            max_quantity=lot['maxQty'],max_market_order_quantity=market['maxQty'],
            price_increment=price['tickSize'],min_price=price['minPrice'],max_price=price['maxPrice'],
            min_notional=f['MIN_NOTIONAL']['notional'],close_min_notional='0',dynamic_position_leverage_details=tiers[1],
            multiplier_up=str((decimal(percent.get('multiplierUp',100))-1)*100),
            multiplier_down=str((1-decimal(percent.get('multiplierDown',0)))*100),
            order_types=[t.lower()+'_order' for t in s['orderTypes'] if t in ('LIMIT','MARKET')])

    def quotes(self,max_age=90):
        rows=self.client.call('GET','/fapi/v1/ticker/24hr',signed=False)
        marks=self.client.call('GET','/fapi/v1/premiumIndex',signed=False)
        if not isinstance(rows,list) or not isinstance(marks,list):raise MarketDataError('Invalid Binance quote response')
        now=self.clock();known=self.instruments();mark={r['symbol']:r for r in marks};result={}
        for row in rows:
            try:
                pair=pair_name(row['symbol']);stamp=float(row['closeTime'])/1000
                if pair not in known or not -30<=now-stamp<=max_age:continue
                m=mark.get(row['symbol'],{});mp=None
                if -30<=now-float(m.get('time',0))/1000<=max_age:mp=decimal(m['markPrice'],True)
                result[pair]=Quote(pair,decimal(row['lastPrice'],True),decimal(row['priceChangePercent']),stamp,
                                   decimal(row['lowPrice'],True),mp)
            except (KeyError,ValueError,TypeError,MarketDataError):continue
        return result

    def candles(self,pair,start,end,resolution='1D',seconds=DAY):
        if seconds not in (DAY,14400,3600) or start>=end:raise ValueError('Invalid Binance candle interval')
        symbol=symbol_name(pair);interval={DAY:'1d',14400:'4h',3600:'1h'}[seconds]
        s=self.instruments().get(pair)
        if not s:raise MarketDataError('Unknown Binance instrument')
        cursor=max(int(start),int(s['onboardDate'])//1000//seconds*seconds);found={}
        while cursor<end:
            stop=min(cursor+1000*seconds,int(end))
            rows=self.client.call('GET','/fapi/v1/klines',dict(symbol=symbol,interval=interval,
                startTime=cursor*1000,endTime=stop*1000-1,limit=1000),signed=False)
            if not isinstance(rows,list):raise MarketDataError('Invalid Binance candle response')
            for r in rows:
                c=Candle.from_api(dict(time=r[0],open=r[1],high=r[2],low=r[3],close=r[4],volume=r[5]))
                if not cursor<=c.timestamp<stop:raise MarketDataError('Binance candle outside requested window')
                if c.timestamp+seconds<=self.clock():
                    if c.timestamp in found and found[c.timestamp]!=c:raise MarketDataError('Conflicting Binance candles')
                    found[c.timestamp]=c
            cursor=stop
        return sorted(found.values(),key=lambda c:c.timestamp)

    def four_hour(self,pair,now):
        end=int(now//14400)*14400
        return self.candles(pair,end-8*14400,end,'4h',14400)

    def price(self,pair,side):
        row=self.client.call('GET','/fapi/v1/ticker/bookTicker',{'symbol':symbol_name(pair)},signed=False)
        if row.get('symbol')!=symbol_name(pair) or not -30<=self.clock()-float(row['time'])/1000<=30:
            raise MarketDataError('Stale Binance order book')
        return decimal(row['askPrice' if side=='BUY' else 'bidPrice'],True)

    def validate_order(self,pair,side,qty,price=None,reduce=False):
        """Check Binance's actual order filters against a fresh mark price."""
        s=self.instruments().get(pair)
        if s is None or s['status']!='TRADING':raise MarketDataError('Binance symbol is not trading')
        filters={f['filterType']:f for f in s['filters']}
        qty=decimal(qty,True)
        lots=[filters['LOT_SIZE']]
        if price is None and 'MARKET_LOT_SIZE' in filters:lots.append(filters['MARKET_LOT_SIZE'])
        for lot in lots:
            minimum,maximum,step=(decimal(lot[k]) for k in ('minQty','maxQty','stepSize'))
            if qty<minimum or maximum and qty>maximum or step and (qty-minimum)%step:
                raise MarketDataError('Binance quantity filter failed')
        mark=self.client.call('GET','/fapi/v1/premiumIndex',{'symbol':s['symbol']},signed=False)
        if mark.get('symbol')!=s['symbol'] or not -30<=self.clock()-float(mark.get('time',0))/1000<=30:
            raise MarketDataError('Fresh Binance mark price unavailable')
        reference=decimal(mark['markPrice'],True)
        if price is not None:
            price=decimal(price,True);f=filters['PRICE_FILTER']
            low,high,tick=(decimal(f[k]) for k in ('minPrice','maxPrice','tickSize'))
            if low and price<low or high and price>high or tick and (price-low)%tick:
                raise MarketDataError('Binance price filter failed')
            f=filters.get('PERCENT_PRICE',{})
            if side=='BUY' and 'multiplierUp' in f and price>reference*decimal(f['multiplierUp']):
                raise MarketDataError('Binance buy exceeds mark-price bound')
            if side=='SELL' and 'multiplierDown' in f and price<reference*decimal(f['multiplierDown']):
                raise MarketDataError('Binance sell is below mark-price bound')
        if not reduce and qty*(price or reference)<decimal(filters['MIN_NOTIONAL']['notional'],True):
            raise MarketDataError('Binance entry minimum notional exceeds selected quantity')
