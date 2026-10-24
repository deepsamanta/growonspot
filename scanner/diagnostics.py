"""Read-only evidence for short signals blocked by account capacity."""
import time
from decimal import Decimal
from .market import DAY
from .strategy import weekly_resistances


def blocked_shorts(config, capacity, market, history, emit, stopping):
    # No exchange/order access and no candidate queue: observations cannot trade.
    if capacity.allows('SELL') or not config.enabled:
        return
    checked_at = time.time()
    instruments = set(market.active_instruments())
    quotes = market.quotes()
    for pair, quote in quotes.items():
        if stopping.is_set() or not config.enabled:
            return
        if time.time()-checked_at > 30:
            emit('SCAN_DIAGNOSTIC_INCOMPLETE',reason='READ_BUDGET_EXHAUSTED')
            return
        if pair not in instruments or pair == 'B-XAU_USDT' or quote.change_24h <= 35:
            continue
        fields = dict(pair=pair, side='SELL', price=str(quote.price),
                      change_24h=str(quote.change_24h), observed_at=quote.timestamp,
                      capacity_checked_at=checked_at, **capacity.report())
        try:
            if pair in capacity.pairs:
                reason = 'POSITION_OR_PENDING_ORDER_EXISTS'
            elif not -30 <= time.time()-quote.timestamp <= 60:
                reason = 'STALE_QUOTE_NOT_EVALUATED'
            else:
                now = time.time()
                daily = history.daily(pair, now)
                if time.time()-quote.timestamp > 60:
                    reason = 'STALE_QUOTE_NOT_EVALUATED'
                elif (not daily or len(daily) < 100 or now-daily[0].timestamp < 100*DAY
                        or daily[-1].timestamp != int(now//DAY)*DAY-DAY):
                    reason = 'HISTORY_INSUFFICIENT_OR_INCOMPLETE'
                else:
                    levels = [r for r in weekly_resistances(daily, now) if r > quote.price]
                    if not levels:
                        reason = 'NO_WEEKLY_RESISTANCE_ABOVE_PRICE'
                    else:
                        resistance = levels[0]
                        distance = (resistance-quote.price)/resistance
                        fields.update(resistance=str(resistance), distance_percent=str(distance*100),
                                      raw_limit_price=str(resistance * Decimal('1.02')))
                        reason = ('SIGNAL_MATCHED_CAPACITY_BLOCKED' if distance <= config.short_distance
                                  else 'OUTSIDE_RESISTANCE_RANGE')
            emit('SHORT_CAPACITY_DIAGNOSTIC', reason=reason,
                 execution_checks='NOT_RUN', **fields)
        except Exception as error:
            emit('SHORT_CAPACITY_DIAGNOSTIC', reason='DATA_ERROR_NOT_EVALUATED',
                 error_type=type(error).__name__, **fields)
