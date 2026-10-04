"""Weekly swing-high resistance and full-history ATL eligibility. No VWAP rules."""
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from .market import DAY, Candle, Quote, MarketDataError

D = Decimal


@dataclass(frozen=True)
class Candidate:
    pair: str
    side: str
    observed_at: float
    reference: Decimal
    history_first: int
    change_24h: Decimal
    confirmation: str = ''


FOUR_HOURS=4*3600


def long_confirmation(candles,now,price):
    """Completed 4h consolidation or a confirmed bullish higher-low breakout."""
    end=int(now//FOUR_HOURS)*FOUR_HOURS
    bars=[c for c in candles if c.timestamp+FOUR_HOURS<=end][-6:]
    if len(bars)!=6 or [c.timestamp for c in bars]!=list(range(end-6*FOUR_HOURS,end,FOUR_HOURS)):
        return None
    last,previous=bars[-1],bars[-2]
    if price<min(last.low,previous.low):return None
    if (last.close>last.open and last.close>max(c.high for c in bars[-4:-1])
            and last.low>previous.low and price>=last.low):
        return 'BULLISH_REVERSAL'
    low=min(c.low for c in bars)
    if (max(c.high for c in bars)<=low*D('1.04')
            and min(c.low for c in bars[-2:])>=min(c.low for c in bars[:4])):
        return 'CONSOLIDATION'
    return None


def weekly_resistances(candles, now):
    """Confirmed weekly swing highs: two lower weeks on each side, UTC weeks."""
    weeks = defaultdict(list)
    for c in candles[-1000:]:
        dt = datetime.fromtimestamp(c.timestamp, timezone.utc)
        monday = c.timestamp - dt.weekday() * DAY
        weeks[monday].append(c)
    completed = []
    for start, bars in sorted(weeks.items()):
        stamps = {b.timestamp for b in bars}
        if start + 7 * DAY > now or stamps != {start + n * DAY for n in range(7)}:
            continue
        completed.append((start,max(b.high for b in bars)))
    levels = set()
    for i in range(2,len(completed)-2):
        window = completed[i-2:i+3]
        if any(window[n+1][0]-window[n][0] != 7*DAY for n in range(4)):
            continue
        high = completed[i][1]
        if high > max(v for _,v in completed[i-2:i]) and high > max(v for _,v in completed[i+1:i+3]):
            levels.add(high)
    return sorted(levels)


def evaluate(pair, quote, daily, now, historical_low=None, historical_first=None,intraday=None,allow_short=True,short_distance=D('.10')):
    if pair == 'B-XAU_USDT' or not daily:
        return None
    first = historical_first if historical_first is not None else daily[0].timestamp
    if now-first < 100*DAY or len(daily)<100:
        return None
    # Reserve a resting short limit before price reaches weekly resistance.
    if allow_short and quote.change_24h > 35:
        above = [r for r in weekly_resistances(daily,now) if r > quote.price]
        if above and (above[0]-quote.price)/above[0] <= short_distance:
            return Candidate(pair,'SELL',quote.timestamp,above[0],first,quote.change_24h)
    if historical_low is not None:
        # Include today's low so a fresh ATL is not mistaken for an older higher ATL.
        low = min(historical_low,quote.low_24h or quote.price,quote.price)
        confirmation=long_confirmation(intraday or [],now,quote.price)
        if quote.price <= low * D('1.10') and confirmation:
            return Candidate(pair,'BUY',quote.timestamp,low,first,quote.change_24h,confirmation)
    return None


def serialize(candles):
    return [[c.timestamp,*[str(getattr(c,k)) for k in ('open','high','low','close','volume')]] for c in candles]


def deserialize(rows):
    return [Candle(int(row[0]),*(D(x) for x in row[1:])) for row in rows]


class History:
    START = 1262304000  # 2010-01-01; before CoinDCX futures history exists.

    def __init__(self, market, state):
        self.market,self.state=market,state

    @staticmethod
    def complete(bars, end, start=None):
        if not bars:
            raise MarketDataError('Daily history is incomplete')
        first = bars[0].timestamp if start is None else start
        if ([c.timestamp for c in bars] != list(range(first, end, DAY))
                or first % DAY != 0):
            raise MarketDataError('Daily history contains missing days')

    def daily(self,pair,now):
        end=int(now//DAY)*DAY
        key='daily:'+pair
        saved=self.state.cache(key)
        if saved and saved['end']==end:
            return deserialize(saved['bars'])
        bars=self.market.candles(pair,end-1000*DAY,end)
        if bars:self.complete(bars,end)
        self.state.save_cache(key,{'end':end,'bars':serialize(bars)})
        return bars

    def all_time(self,pair,now,daily):
        end=int(now//DAY)*DAY
        key='atl:'+pair
        saved=self.state.cache(key)
        if saved and saved['end']==end:
            return D(saved['low']),saved['first']
        if saved:
            extra=self.market.candles(pair,saved['end'],end) if saved['end']<end else []
            if saved['end']<end:self.complete(extra,end,saved['end'])
            low=min([D(saved['low'])]+[c.low for c in extra]);first=saved['first']
        else:
            if not daily:
                return None,None
            older=self.market.candles(pair,self.START,daily[0].timestamp) if daily[0].timestamp>self.START else []
            all_bars=older+daily
            self.complete(all_bars,end)
            low=min(c.low for c in all_bars);first=min(c.timestamp for c in all_bars)
        self.state.save_cache(key,{'first':first,'low':str(low),'end':end,'coverage_start':self.START})
        return low,first
