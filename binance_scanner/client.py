"""Signed Binance REST transport. Mutations are never blindly retried."""
import hashlib
import hmac
import time
from urllib.parse import urlencode
import requests
from scanner.exchange import ExchangeError

BASE = 'https://fapi.binance.com'


class BinanceError(ExchangeError):
    def __init__(self, message, endpoint='', status=None, code=None):
        super().__init__(message, endpoint, status)
        self.code = code

    @property
    def rejected(self):
        return self.status in (400, 401, 403, 404, 422) and self.code not in (-1000, -1006, -1007)


class Client:
    def __init__(self, key='', secret='', session=None, clock=time.time):
        self.key, self.secret = key, secret
        self.http = session or requests.Session()
        self.clock = clock
        self.offset = 0
        self.synced = None
        self.blocked_until = 0

    def sync_time(self):
        before = self.clock()
        data = self.call('GET', '/fapi/v1/time', signed=False)
        self.offset = float(data['serverTime']) - (before+self.clock())*500
        self.synced = self.clock()

    def call(self, method, path, params=None, signed=True, api_key=False):
        if not path.startswith('/fapi/') or method not in ('GET', 'POST', 'PUT', 'DELETE'):
            raise ValueError('Invalid Binance request')
        if self.clock() < self.blocked_until:
            raise BinanceError('Binance rate-limit cooldown active', path, 429)
        if signed:
            if not self.key or not self.secret:
                raise BinanceError('Binance credentials are not configured', path, 401)
            if self.synced is None or self.clock()-self.synced > 900:
                self.sync_time()
        for attempt in range(3 if method == 'GET' else 1):
            body = dict(params or {})
            if signed:
                body.update(timestamp=int(self.clock()*1000+self.offset), recvWindow=5000)
            query = urlencode(body)
            if signed:
                query += '&signature='+hmac.new(self.secret.encode(), query.encode(), hashlib.sha256).hexdigest()
            headers = {'X-MBX-APIKEY': self.key} if signed or api_key else {}
            try:
                response = self.http.request(method, BASE+path, params=query, headers=headers, timeout=(5, 15))
                if response.status_code in (418, 429):
                    try:delay = min(3600, max(1, float(response.headers.get('Retry-After', 60))))
                    except ValueError:delay = 60
                    self.blocked_until = self.clock()+delay
                    raise BinanceError('Binance rate limit; requests paused', path, response.status_code)
                data = response.json()
                code = data.get('code') if isinstance(data, dict) else None
                if not response.ok or isinstance(code, int) and code < 0:
                    if method == 'GET' and attempt < 2 and code == -1021:
                        self.sync_time(); continue
                    if method == 'GET' and attempt < 2 and response.status_code >= 500:
                        time.sleep(2**attempt); continue
                    # Never include signed request URLs, raw bodies, or credentials in errors.
                    raise BinanceError('Binance request failed (code '+str(code)+')', path, response.status_code, code)
                return data
            except (requests.RequestException, ValueError) as error:
                if method != 'GET' or attempt == 2:
                    raise BinanceError(type(error).__name__, path) from None
                time.sleep(2**attempt)
        raise BinanceError('Binance read retries exhausted', path)
