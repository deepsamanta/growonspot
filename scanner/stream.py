"""Account notifications wake REST reconciliation; they never execute trades.

CoinDCX uses Socket.IO v2 (Python Socket.IO 4 / Engine.IO 3). A separate
supervisor owns the connection; exchange writes and SQLite stay in the runner.
"""
from collections import OrderedDict
import hashlib
import hmac
import json
import threading
import time


ENDPOINT = 'https://stream.coindcx.com'
EVENT_FIELDS = {
    'df-order-update': ('pair', 'id', 'side', 'status', 'total_quantity',
                        'remaining_quantity', 'cancelled_quantity', 'avg_price',
                        'price', 'stop_price', 'order_type'),
    # Mark-price/margin/timestamp ticks alone must not cause a REST request storm.
    'df-position-update': ('pair', 'id', 'active_pos', 'inactive_pos_buy',
                           'inactive_pos_sell', 'avg_price', 'leverage',
                           'margin_type', 'take_profit_trigger', 'stop_loss_trigger'),
    'balance-update': ('currency_short_name', 'balance', 'locked_balance'),
}


def client_factory():
    import socketio
    import websocket
    # Engine.IO 3's request_timeout only bounds HTTP polling. Bound its
    # WebSocket handshake/reads too, leaving room for the 25-second heartbeat.
    websocket.setdefaulttimeout(60)
    return socketio.Client(reconnection=False, request_timeout=10,
                           logger=False, engineio_logger=False)


def rows(payload):
    """Accept the documented envelope and direct list, without logging payloads."""
    for _ in range(4):
        if isinstance(payload, str):
            if len(payload) > 262144:
                return []
            try:
                payload = json.loads(payload)
            except (ValueError, TypeError):
                return []
        elif isinstance(payload, dict) and 'data' in payload:
            payload = payload['data']
        else:
            break
    if isinstance(payload, dict):
        return [payload]
    return [row for row in payload[:1000] if isinstance(row, dict)] if isinstance(payload, list) else []


class AccountStream:
    def __init__(self, key, secret, emit, enabled=True, factory=client_factory):
        self.key, self.secret, self.emit = key, secret, emit
        self.enabled, self.factory = enabled, factory
        self.lock = threading.Lock()
        self.send_lock = threading.Lock()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.thread = None
        self.seen = OrderedDict()
        self.state = dict(enabled=enabled, connected=False,
                          subscription_requested=False, private_event_seen=False,
                          last_event_at=None, last_connected_at=None, events=0,
                          wakeups=0, reconnects=0, last_error=None)

    def snapshot(self):
        with self.lock:
            state = dict(self.state)
        state['mode'] = ('notifications_and_rest' if state['connected'] and state['subscription_requested']
                         else 'rest_polling')
        return state

    def start(self):
        if self.enabled and self.thread is None:
            self.thread = threading.Thread(target=self.run, name='coindcx-account-stream', daemon=True)
            self.thread.start()

    def receive(self, event, payload):
        fields = EVENT_FIELDS[event]
        changed = False
        valid = False
        for row in rows(payload):
            if event == 'balance-update':
                if row.get('currency_short_name') != 'USDT' or 'balance' not in row:
                    continue
                ident = 'USDT'
            else:
                pair = row.get('pair')
                if (not isinstance(pair, str) or pair == 'B-XAU_USDT'
                        or not pair.endswith('_USDT') or not row.get('id')
                        or row.get('margin_currency_short_name', 'USDT') != 'USDT'):
                    continue
                required = 'status' if event == 'df-order-update' else 'active_pos'
                if required not in row:
                    continue
                ident = str(row['id'])
            valid = True
            fingerprint = json.dumps({k: row[k] for k in fields if k in row}, sort_keys=True, default=str)
            key = (event, ident)
            with self.lock:
                if self.seen.get(key) != fingerprint:
                    self.seen[key] = fingerprint
                    self.seen.move_to_end(key)
                    if len(self.seen) > 2048:
                        self.seen.popitem(last=False)
                    changed = True
        if valid:
            with self.lock:
                self.state.update(private_event_seen=True, last_event_at=time.time())
                self.state['events'] += 1
                if changed:
                    self.state['wakeups'] += 1
            if changed:
                self.wake.set()

    def subscribe(self, client):
        signature = hmac.new(self.secret.encode(), b'{"channel":"coindcx"}', hashlib.sha256).hexdigest()
        with self.send_lock:
            client.emit('join', {'channelName': 'coindcx', 'apiKey': self.key,
                                 'authSignature': signature})
        with self.lock:
            reconnect = self.state['last_connected_at'] is not None
            self.state.update(connected=True, subscription_requested=True,
                              private_event_seen=False, last_connected_at=time.time(), last_error=None)
            if reconnect:
                self.state['reconnects'] += 1
        # Refresh authoritative state after every connection, even in a quiet account.
        self.wake.set()
        self.emit('WEBSOCKET_CONNECTED', subscription='requested', rest_verification=True)

    def disconnected(self):
        with self.lock:
            self.state.update(connected=False, subscription_requested=False, private_event_seen=False)
        self.wake.set()

    def run(self):
        failures = 0
        while not self.stop.is_set():
            client = None
            started = time.monotonic()
            try:
                client = self.factory()
                client.on('connect', lambda current=client: self.subscribe(current))
                client.on('disconnect', self.disconnected)
                for event in EVENT_FIELDS:
                    client.on(event, lambda payload, name=event: self.receive(name, payload))
                client.connect(ENDPOINT, transports=['websocket'])
                next_ping = time.monotonic() + 25
                while not self.stop.wait(1):
                    if not client.connected:
                        raise ConnectionError('Disconnected')
                    if not self.snapshot()['subscription_requested'] and time.monotonic() - started > 20:
                        raise TimeoutError('Subscription was not started')
                    if time.monotonic() >= next_ping:
                        with self.send_lock:
                            client.emit('ping', {'data': 'Ping message'})
                        next_ping = time.monotonic() + 25
                break
            except Exception as error:
                with self.lock:
                    self.state['last_error'] = type(error).__name__
                if not self.stop.is_set():
                    self.emit('WEBSOCKET_UNAVAILABLE', error_type=type(error).__name__,
                              fallback='REST polling continues')
            finally:
                self.disconnected()
                if client is not None:
                    try:
                        client.disconnect()
                    except Exception:
                        pass
            # A stable connection resets backoff; rapid reconnect flapping does not.
            failures = 1 if time.monotonic() - started >= 60 else min(failures + 1, 5)
            self.stop.wait(min(60, 5 * 2 ** (failures - 1)))

    def wait(self, timeout):
        """Coalesce bursts; retain regular polling when there are no notifications."""
        started = time.monotonic()
        if self.wake.wait(timeout):
            self.stop.wait(max(0, 1 - (time.monotonic() - started)))
        self.wake.clear()

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.thread is not None:
            self.thread.join(timeout=2)
