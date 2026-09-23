"""CoinDCX REST adapter: same compact JSON + HMAC signing as reference bot."""
import hashlib
import hmac
import json
import time
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
import requests

D = lambda x: Decimal(str(x))
BASE = 'https://api.coindcx.com/exchange/v1/derivatives/futures/'

class APIError(RuntimeError):
    def __init__(self, message, endpoint=None, http_status=None):
        super().__init__(message)
        self.endpoint = endpoint
        self.http_status = http_status

    def details(self):
        return {'error_type': type(self).__name__, 'reason': str(self),
                'endpoint': self.endpoint, 'http_status': self.http_status}

class EntryNotSubmitted(APIError):
    """Preparation failed before the order-create request was attempted."""

class CoinDCX:
    def __init__(self, config):
        self.c = config
        self.http = requests.Session()
        self.pair = None
        self.instrument = None
        self.last_ok = 0

    def request(self, path, body=None, read=False, params=None):
        attempts = 3 if read or body is None else 1
        endpoint = path.split('?')[0]
        for attempt in range(attempts):
            failure = None
            retryable = True
            try:
                url = path if path.startswith('https://') else BASE + path
                if body is None:
                    response = self.http.get(url, params=params, timeout=15)
                else:
                    payload = json.dumps({**body, 'timestamp': int(time.time()*1000)}, separators=(',', ':'))
                    signature = hmac.new(self.c.secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
                    response = self.http.post(url, data=payload, headers={
                        'Content-Type': 'application/json', 'X-AUTH-APIKEY': self.c.key,
                        'X-AUTH-SIGNATURE': signature}, timeout=15)
                if not response.ok:
                    failure = APIError(f'CoinDCX HTTP {response.status_code}', endpoint, response.status_code)
                    retryable = response.status_code == 429 or response.status_code >= 500
                else:
                    result = response.json()
                    if isinstance(result, dict) and (result.get('success') is False or
                            str(result.get('status', '')).lower() in ('error', 'failed') or
                            str(result.get('code', '200')).isdigit() and int(result.get('code', 200)) >= 400):
                        failure = APIError('CoinDCX returned an error response', endpoint, response.status_code)
                        retryable = False
                    else:
                        self.last_ok = time.time()
                        return result
            except (requests.RequestException, ValueError) as error:
                # Exception messages can contain URLs/credentials; retain only their type.
                failure = APIError(type(error).__name__, endpoint)
            if attempt + 1 == attempts or not retryable:
                raise failure from None
            time.sleep(2 ** attempt)

    def discover(self):
        pairs = self.request('data/active_instruments', params={'margin_currency_short_name[]': 'USDT'})
        if not isinstance(pairs, list):
            raise APIError('Invalid active instruments response')
        matches = [p for p in pairs if isinstance(p, str) and p.split('-', 1)[-1] == 'XAU_USDT']
        if len(matches) != 1:
            raise APIError('XAUUSDT unavailable or ambiguous; trading stopped')
        self.pair = matches[0]
        info = self.request('data/instrument', params={'pair': self.pair, 'margin_currency_short_name': 'USDT'})['instrument']
        if info['pair'] != self.pair or info['status'] != 'active' or info.get('exit_only') or info.get('is_inverse') or info.get('is_quanto'):
            raise APIError('Unsupported/inactive instrument')
        if D(info.get('unit_contract_value', 0)) != 1:
            raise APIError('Unsupported contract multiplier')
        for key in ('quantity_increment', 'price_increment', 'min_quantity', 'min_notional'):
            if D(info[key]) <= 0:
                raise APIError(f'Invalid instrument metadata: {key}')
        self.instrument = info
        return info

    def price(self, side):
        book = self.request(f'https://public.coindcx.com/market_data/v3/orderbook/{self.pair}-futures/50')
        if abs(time.time() - float(book['ts'])/1000) > 30:
            raise APIError('Stale order book')
        levels = book['asks' if side == 'BUY' else 'bids']
        values = [D(p) for p, q in levels.items() if D(q) > 0]
        if not values:
            raise APIError('Empty order book')
        return min(values) if side == 'BUY' else max(values)

    def size(self, price):
        info = self.instrument
        notional = self.c.margin * self.c.leverage
        tiers = info.get('dynamic_position_leverage_details', {})
        allowed = [D(k) for k, v in tiers.items() if D(v) >= notional]
        if not allowed or self.c.leverage > max(allowed):
            raise ValueError('LEVERAGE_NOT_ALLOWED')
        step = D(info['quantity_increment'])
        qty = (notional / price / step).to_integral_value(rounding=ROUND_DOWN) * step
        if qty < max(D(info['min_quantity']), D(info.get('min_trade_size', 0))) or qty * price < D(info['min_notional']):
            raise ValueError('ORDER_BELOW_EXCHANGE_MINIMUM: refusing to increase $5 margin')
        if qty > min(D(info['max_quantity']), D(info['max_market_order_quantity'])):
            raise ValueError('ORDER_ABOVE_EXCHANGE_MAXIMUM')
        return qty

    def tick(self, value):
        tick = D(self.instrument['price_increment'])
        result = (value / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick
        if not D(self.instrument['min_price']) <= result <= D(self.instrument['max_price']):
            raise ValueError('PROTECTION_PRICE_OUT_OF_RANGE')
        return result

    def pages(self, path, extra=None):
        seen = set()
        for page in range(1, 101):
            rows = self.request(path, {'page': str(page), 'size': '100',
                'margin_currency_short_name': ['USDT'], **(extra or {})}, read=True)
            if not isinstance(rows, list):
                raise APIError('Unexpected CoinDCX list response', path)
            fingerprint = tuple(row.get('id') for row in rows)
            if rows and fingerprint in seen:
                raise APIError('Exchange repeated a pagination page', path)
            seen.add(fingerprint)
            yield rows
            if len(rows) < 100:
                return
        raise APIError('Pagination exceeded before required record was found', path)

    def rows(self, path, extra=None):
        return [row for page in self.pages(path, extra) for row in page]

    def positions(self):
        # CoinDCX keeps hundreds of inactive pair rows. Query only this instrument.
        return [p for p in self.rows('positions', {'pairs': self.pair}) if p['pair'] == self.pair]

    def orders(self, side, status='open,partially_filled,untriggered'):
        return [o for o in self.rows('orders', {'side': side.lower(), 'status': status}) if o['pair'] == self.pair]

    def find_order(self, side, order_id):
        # Stop immediately on the stored ID instead of exhausting all account history.
        statuses = 'filled,partially_filled,partially_cancelled,cancelled,open,rejected,untriggered'
        for page in self.pages('orders', {'side': side.lower(), 'status': statuses}):
            for order in page:
                if order.get('id') == order_id:
                    if order.get('pair') != self.pair or order.get('side') != side.lower():
                        raise APIError('Stored order does not belong to the expected pair/side', 'orders')
                    return order
        return None

    def create(self, signal, qty, sl, tp):
        try:
            self.request('positions/update_leverage', {'pair': self.pair, 'leverage': self.c.leverage, 'margin_currency_short_name': 'USDT'})
        except APIError as error:
            raise EntryNotSubmitted(str(error), error.endpoint, error.http_status) from None
        # Keep the reference bot's SL/TP fields; also verify exchange-side protection after fill.
        result = self.request('orders/create', {'order': {
            'side': signal.side.lower(), 'pair': self.pair, 'order_type': 'market_order',
            'price': None, 'total_quantity': float(qty), 'leverage': self.c.leverage,
            'take_profit_price': float(tp), 'stop_loss_price': float(sl),
            'notification': 'no_notification', 'margin_currency_short_name': 'USDT',
            'position_margin_type': 'isolated'}})
        rows = result if isinstance(result, list) else result.get('order', [])
        if isinstance(rows, dict):
            rows = [rows]
        if len(rows) != 1 or not rows[0].get('id'):
            raise APIError('Ambiguous order acknowledgement')
        return rows[0]['id']

    def protect(self, position, sl, tp):
        body = {'id': position['id']}
        if D(position.get('stop_loss_trigger') or 0) != sl:
            body['stop_loss'] = {'stop_price': str(sl), 'order_type': 'stop_market'}
        if D(position.get('take_profit_trigger') or 0) != tp:
            body['take_profit'] = {'stop_price': str(tp), 'order_type': 'take_profit_market'}
        if len(body) > 1:
            self.request('positions/create_tpsl', body)

    def cancel(self, order_id):
        return self.request('orders/cancel', {'id': order_id})

    def exit(self, position_id):
        return self.request('positions/exit', {'id': position_id})
