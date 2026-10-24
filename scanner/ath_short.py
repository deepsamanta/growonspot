"""Prior-day futures ATH breakout plus a 35% open-to-high 15-minute surge."""
from datetime import datetime
from zoneinfo import ZoneInfo
from decimal import Decimal as D
import time
import queue
from .market import DAY, MarketDataError
from .strategy import Candidate

RULE='ATH_15M_SURGE'


def day_start(now):
    return int(datetime.fromtimestamp(now,ZoneInfo('Asia/Kolkata')).replace(hour=0,minute=0,second=0,microsecond=0).timestamp())


def surge_bar(bars,now,candle_at=None):
    current=int(now//900)*900
    # Old spikes cannot become a fresh signal hours later.
    valid=[c for c in bars if c.timestamp in (current-900,current)
           and (candle_at is None or c.timestamp==candle_at) and c.high>=c.open*D('1.35')]
    return min(valid,key=lambda c:c.timestamp) if valid else None


def prior_ath(history,pair,now,daily):
    cutoff=day_start(now);key='ath_before_ist_today:'+pair
    saved=history.state.cache(key)
    if saved and saved['end']==cutoff:return D(saved['high']),saved['first'],cutoff
    utc_end=cutoff//DAY*DAY
    base=[c for c in daily if c.timestamp+DAY<=utc_end]
    if not base:raise MarketDataError('ATH daily history unavailable')
    # Cache a complete daily maximum separately from the partial UTC boundary day.
    daily_key='ath_daily:'+pair;old=history.state.cache(daily_key)
    if old and old['end']<=utc_end:
        extra=history.market.candles(pair,old['end'],utc_end) if old['end']<utc_end else []
        if extra:history.complete(extra,utc_end,old['end'])
        elif old['end']<utc_end:raise MarketDataError('ATH history update missing')
        high=max([D(old['high'])]+[c.high for c in extra]);first=old['first']
    else:
        older=history.market.candles(pair,history.START,base[0].timestamp) if base[0].timestamp>history.START else []
        all_bars=older+base;history.complete(all_bars,utc_end)
        high=max(c.high for c in all_bars);first=all_bars[0].timestamp
    history.state.save_cache(daily_key,{'end':utc_end,'high':str(high),'first':first})
    # IST midnight is 18:30 UTC. Include the previous day's final 18.5 UTC hours,
    # excluding every candle from today's IST session.
    tail=history.market.candles(pair,utc_end,cutoff,'5',300)
    if [c.timestamp for c in tail]!=list(range(utc_end,cutoff,300)):
        raise MarketDataError('ATH midnight boundary history incomplete')
    high=max([high]+[c.high for c in tail])
    history.state.save_cache(key,{'end':cutoff,'high':str(high),'first':first})
    return high,first,cutoff


def detect(pair,quote,market,history,now,daily=None):
    if pair=='B-XAU_USDT' or not -30<=now-quote.timestamp<=60:return None
    # A 35% candle necessarily spans at least this much of the rolling daily range.
    if (quote.high_24h is not None and quote.low_24h is not None
            and quote.high_24h<quote.low_24h*D('1.35')):return None
    bar=surge_bar(market.fifteen_minute(pair,now),now)
    if bar is None:return None
    daily=history.daily(pair,now) if daily is None else daily
    if not daily or len(daily)<100 or now-daily[0].timestamp<100*DAY:return None
    high,first,cutoff=prior_ath(history,pair,now,daily)
    if min(quote.price,quote.mark_price or quote.price)<=high:return None
    return Candidate(pair,'SELL',quote.timestamp,high,first,quote.change_24h,RULE,bar.timestamp,cutoff,bar.open,bar.high)


def valid_now(candidate,quote,price,market,now):
    if (candidate.reference_end!=day_start(now) or not -30<=now-quote.timestamp<=60
            or min(price,quote.price,quote.mark_price or quote.price)<=candidate.reference):return False
    bars=market.fifteen_minute(candidate.pair,now)
    checked=time.time()
    return (checked-quote.timestamp<=60 and candidate.reference_end==day_start(checked)
            and surge_bar(bars,checked,candidate.candle_at) is not None)


def watch(config,outbox,health,emit,stopping):
    """Independent public-data watcher; order writes remain in the main engine."""
    from .state import ScannerState
    from .market import MarketData
    from .strategy import History
    from .exchange import Exchange
    from .capacity import BalanceCapacity
    db=ScannerState(config.database,config.timezone);market=MarketData();history=History(market,db)
    capacity_reader=BalanceCapacity(Exchange(config,market),db)
    instruments=set();instruments_at=0
    try:
        while not stopping.is_set():
            started=time.time()
            try:
                capacity=capacity_reader.get();checked=time.time()
                health.update(ath_checked_at=checked,ath_error=None)
                if not config.enabled or not capacity.allows('SELL'):
                    health['ath_paused_reason']='USER_PAUSED' if not config.enabled else 'SHORT_CAPACITY_FULL'
                else:
                    health['ath_paused_reason']=None
                    if checked-instruments_at>=300:
                        instruments=set(market.active_instruments());instruments_at=time.time()
                    quotes=market.quotes()
                    held={r['pair'] for r in db.rows() if r['status'] not in ('CLOSED','REJECTED')}
                    for pair,quote in quotes.items():
                        if stopping.is_set() or not config.enabled:break
                        if time.time()-checked>15:
                            capacity=capacity_reader.get();checked=time.time()
                        if not capacity.allows('SELL'):break
                        if pair not in instruments or pair in held or pair in capacity.pairs:continue
                        try:
                            if time.time()-quote.timestamp>60:
                                quote=market.quotes().get(pair)
                                if quote is None:continue
                            signal=detect(pair,quote,market,history,time.time())
                            if signal:
                                # Match the durable engine key; never replay a consumed candle.
                                key=f'{pair}:ATH:{signal.candle_at}'
                                if db.conn.execute('SELECT 1 FROM scanner_trades WHERE signal_key=?',(key,)).fetchone():continue
                                emit('SIGNAL_MATCHED',pair=pair,side='SELL',rule=RULE,observed_at=quote.timestamp,
                                     price=str(quote.price),prior_ath=str(signal.reference),candle_at=signal.candle_at,
                                     candle_open=str(signal.candle_open),candle_high=str(signal.candle_high))
                                try:outbox.put_nowait(signal)
                                except queue.Full:emit('ENTRY_SKIPPED',pair=pair,reason='CANDIDATE_QUEUE_FULL')
                        except Exception as error:
                            emit('ATH_SIGNAL_NOT_EVALUATED',pair=pair,reason='DATA_ERROR',error_type=type(error).__name__)
                    health['ath_completed_at']=time.time()
            except Exception as error:
                health['ath_error']=type(error).__name__
                emit('SCANNER_ERROR',reason='ATH_WATCHER_READ_FAILED',error_type=type(error).__name__)
            stopping.wait(max(1,config.ath_scan_interval-(time.time()-started)))
    finally:
        db.conn.close()
