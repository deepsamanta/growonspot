"""Public CoinDCX market data. No credentials or order-writing capabilities."""
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import requests

API = 'https://api.coindcx.com/exchange/v1/derivatives/futures/data/'
PUBLIC = 'https://public.coindcx.com/market_data/'
DAY = 86400
GOLD = {'XAU', 'XAUUSD', 'XAUUSDT'}


class MarketDataError(RuntimeError):
    pass


def decimal(value, positive=False):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise MarketDataError('Invalid numeric market data') from None
    if not result.is_finite() or (positive and result <= 0):
        raise MarketDataError('Non-finite or invalid market data')
    return result


@dataclass(frozen=True)
class Quote:
    pair: str
    price: Decimal
    change_24h: Decimal
    timestamp: float
    low_24h: Decimal = None
    mark_price: Decimal = None


@dataclass(frozen=True)
class Candle:
    timestamp: int  # UTC open, seconds
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    @classmethod
    def from_api(cls, row):
        try:
            candle = cls(int(row['time']) // 1000,
                         *(decimal(row[k], positive=True) for k in ('open', 'high', 'low', 'close')),
                         decimal(row['volume']))
        except (KeyError, TypeError, ValueError):
            raise MarketDataError('Incomplete candle') from None
        if (candle.volume < 0 or candle.low > min(candle.open, candle.close)
                or candle.high < max(candle.open, candle.close) or candle.high < candle.low):
            raise MarketDataError('Invalid OHLC candle')
        return candle


class MarketData:
    def __init__(self, session=None, clock=time.time):
        self.http = session or requests.Session()
        self.clock = clock

    def get(self, url, params=None):
        for attempt in range(3):
            try:
                response = self.http.get(url, params=params, timeout=(5, 15))
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < 2:
                        time.sleep(2 ** attempt)
                        continue
                if not response.ok:
                    raise MarketDataError(f'Market data HTTP {response.status_code}')
                return response.json()
            except (requests.RequestException, ValueError) as error:
                if attempt == 2:
                    raise MarketDataError(type(error).__name__) from None
                time.sleep(2 ** attempt)
        raise MarketDataError('Market data unavailable')

    def active_instruments(self):
        data = self.get(API + 'active_instruments', {'margin_currency_short_name[]': 'USDT'})
        if not isinstance(data, list) or not data or any(not isinstance(p, str) for p in data):
            raise MarketDataError('Invalid active-instrument list')
        return sorted(set(data))

    def metadata(self, pair):
        result = self.get(API + 'instrument', {'pair': pair, 'margin_currency_short_name': 'USDT'})
        info = result.get('instrument') if isinstance(result, dict) else None
        if not isinstance(info, dict) or info.get('pair') != pair:
            raise MarketDataError('Instrument identity mismatch')
        return info

    @staticmethod
    def eligible_metadata(info):
        # The scanner never trades the existing gold bot's instrument.
        base = str(info.get('underlying_currency_short_name', '')).upper()
        position = str(info.get('position_currency_short_name', '')).upper()
        return (bool(base) and base not in GOLD and position not in GOLD
                and info.get('pair') != 'B-XAU_USDT'
                and info.get('status') == 'active' and not info.get('exit_only')
                and not info.get('is_inverse') and not info.get('is_quanto')
                and info.get('quote_currency_short_name') == 'USDT'
                and decimal(info.get('unit_contract_value', 0)) == 1)

    def quotes(self, max_age=90):
        data = self.get(PUBLIC + 'v3/current_prices/futures/rt')
        if not isinstance(data, dict) or not isinstance(data.get('prices'), dict):
            raise MarketDataError('Invalid futures quote response')
        now = self.clock()
        timestamp = float(data.get('ts', 0)) / 1000
        if not -30 <= now - timestamp <= max_age:
            raise MarketDataError('Stale futures quote snapshot')
        result = {}
        for pair, row in data['prices'].items():
            if not isinstance(row, dict):
                continue
            try:
                stamp = float(row.get('btST', 0)) / 1000
                if not -30 <= now - stamp <= max_age:
                    continue
                mark=None
                if row.get('mp') is not None and -30<=now-float(row.get('bmST',0))/1000<=max_age:
                    mark=decimal(row['mp'],True)
                result[pair] = Quote(pair, decimal(row['ls'], True), decimal(row['pc']), stamp, decimal(row.get('l',row['ls']),True),mark)
            except (MarketDataError, KeyError, TypeError, ValueError):
                continue
        return result

    def candles(self, pair, start, end, resolution='1D', seconds=DAY):
        """Fetch bounded chunks; errors never masquerade as an empty history."""
        if seconds <= 0 or start >= end:
            raise ValueError('Invalid candle interval')
        found = {}
        cursor = int(start)
        while cursor < end:
            stop = min(cursor + (1000 if seconds == DAY else 200) * seconds, int(end))
            payload = self.get(PUBLIC + 'candlesticks',
                {'pair': pair, 'from': cursor, 'to': stop, 'resolution': resolution, 'pcode': 'f'})
            if not isinstance(payload, dict) or payload.get('s') not in ('ok', 'no_data'):
                raise MarketDataError('Candle history was not confirmed by the API')
            rows = payload.get('data', [])
            if not isinstance(rows, list):
                raise MarketDataError('Invalid candle history')
            for row in rows:
                candle = Candle.from_api(row)
                if cursor <= candle.timestamp < stop and candle.timestamp + seconds <= self.clock():
                    prior = found.get(candle.timestamp)
                    if prior is not None and prior != candle:
                        raise MarketDataError('Conflicting candle records')
                    found[candle.timestamp] = candle
            cursor = stop
        return sorted(found.values(), key=lambda c: c.timestamp)

    def four_hour(self,pair,now):
        # CoinDCX documents 60-minute candles; form 4h bars only from all four
        # completed constituent hours, avoiding unsupported resolutions.
        interval=4*3600;end=int(now//interval)*interval
        hours={c.timestamp:c for c in self.candles(pair,end-8*interval,end,'60',3600)}
        result=[]
        for start in range(end-8*interval,end,interval):
            group=[hours.get(start+n*3600) for n in range(4)]
            if any(c is None for c in group):continue
            result.append(Candle(start,group[0].open,max(c.high for c in group),min(c.low for c in group),
                                group[-1].close,sum(c.volume for c in group)))
        return result
