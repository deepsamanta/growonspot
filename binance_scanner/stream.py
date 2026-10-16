"""Binance account events wake the existing serialized REST reconciliation."""
import json
import time
from decimal import Decimal
from urllib.parse import quote
from scanner.stream import AccountStream as EventWake
from .client import Client
from .market import pair_name


class AccountStream(EventWake):
    def __init__(self,key,secret,emit,enabled=True,client=None,connect=None):
        super().__init__(key,secret,emit,enabled)
        self.client=client or Client(key,secret)
        self.connect=connect
        self.socket=None

    def receive_binance(self,payload):
        if isinstance(payload,str):
            if len(payload)>262144:return
            try:payload=json.loads(payload)
            except ValueError:return
        if not isinstance(payload,dict):return
        event=payload.get('e')
        if event=='listenKeyExpired':raise ConnectionError('Binance listen key expired')
        if event=='ORDER_TRADE_UPDATE':
            o=payload.get('o',{});symbol=o.get('s','')
            if not symbol.endswith('USDT'):return
            self.receive('df-order-update',dict(pair=pair_name(symbol),id=str(o.get('i','')),side=o.get('S'),
                status=o.get('X'),total_quantity=o.get('q'),remaining_quantity=str(Decimal(o.get('q','0'))-Decimal(o.get('z','0'))),
                avg_price=o.get('ap'),price=o.get('p'),order_type=o.get('o')))
        elif event=='ACCOUNT_UPDATE':
            a=payload.get('a',{})
            for p in a.get('P',[]):
                if not p.get('s','').endswith('USDT'):continue
                self.receive('df-position-update',dict(pair=pair_name(p['s']),id=p['s']+':'+p.get('ps','BOTH'),
                    active_pos=p.get('pa'),avg_price=p.get('ep'),margin_type=p.get('mt')))
            for b in a.get('B',[]):
                self.receive('balance-update',dict(currency_short_name=b.get('a'),balance=b.get('wb')))
        elif event in ('ALGO_UPDATE','ACCOUNT_CONFIG_UPDATE','MARGIN_CALL'):
            self.wake.set()

    def run(self):
        import websocket
        connect=self.connect or websocket.create_connection
        failures=0
        while not self.stop.is_set():
            started=time.monotonic();sock=None
            try:
                key=self.client.call('POST','/fapi/v1/listenKey',signed=False,api_key=True).get('listenKey')
                if not isinstance(key,str) or not key or len(key)>512:raise ValueError('Invalid Binance listen key')
                sock=connect('wss://fstream.binance.com/private/ws/'+quote(key,safe=''),timeout=10)
                self.socket=sock;renew=time.monotonic()+1800
                with self.lock:
                    reconnect=self.state['last_connected_at'] is not None
                    self.state.update(connected=True,subscription_requested=True,private_event_seen=False,
                                      last_connected_at=time.time(),last_error=None)
                    self.state['reconnects']+=int(reconnect)
                self.wake.set();self.emit('WEBSOCKET_CONNECTED',exchange='BINANCE',rest_verification=True)
                while not self.stop.is_set():
                    if time.monotonic()-started>=23*3600:break
                    if time.monotonic()>=renew:
                        self.client.call('PUT','/fapi/v1/listenKey',signed=False,api_key=True)
                        renew=time.monotonic()+1800
                    try:payload=sock.recv()
                    except websocket.WebSocketTimeoutException:continue
                    if not payload:raise ConnectionError('Binance stream disconnected')
                    self.receive_binance(payload)
                failures=0
            except Exception as error:
                with self.lock:self.state['last_error']=type(error).__name__
                if not self.stop.is_set():self.emit('WEBSOCKET_UNAVAILABLE',exchange='BINANCE',error_type=type(error).__name__,fallback='REST polling continues')
            finally:
                self.disconnected();self.socket=None
                if sock is not None:
                    try:sock.close()
                    except Exception:pass
            failures=1 if time.monotonic()-started>=60 else min(failures+1,5)
            self.stop.wait(min(60,5*2**(failures-1)))

    def close(self):
        self.stop.set();self.wake.set()
        if self.socket is not None:
            try:self.socket.close()
            except Exception:pass
        if self.thread is not None:self.thread.join(timeout=2)
        # Do not DELETE the listen key: another user connection may share it.
