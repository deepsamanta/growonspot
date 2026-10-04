from decimal import Decimal
import fcntl
import json
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler,HTTPServer
from pathlib import Path
from .config import ScannerConfig
from .market import MarketData,MarketDataError,DAY
from .state import ScannerState
from .strategy import History,evaluate
from .exchange import Exchange
from .engine import Engine
from .alerts import Alerts
from .capacity import account_capacity


def scan(config,outbox,health,emit,stopping):
    db=ScannerState(config.database,config.timezone);market=MarketData();history=History(market,db)
    exchange=Exchange(config,market)
    while not stopping.is_set():
        started=time.time()
        try:
            # Capacity must be confirmed before discovery, quotes or candles.
            capacity=account_capacity(exchange.all_positions(),db.occupied())
            checked=time.time()
            health.update(account_capacity=capacity.report(),capacity_checked=checked,scan_error=None)
            if not config.enabled or not (capacity.allows('BUY') or capacity.allows('SELL')):
                health.update(scan_paused_reason='ACCOUNT_POSITION_LIMIT' if config.enabled else 'ENTRIES_DISABLED',scan_updated=checked)
                stopping.wait(30);continue
            instruments=market.active_instruments();quotes=market.quotes()
            health.update(active_instruments=len(instruments),scan_started=started,scan_error=None,scan_paused_reason=None)
            held={r['pair'] for r in db.rows() if r['status'] not in ('CLOSED','REJECTED')}
            for index,pair in enumerate(instruments):
                if stopping.is_set():break
                if time.time()-checked>15:
                    capacity=account_capacity(exchange.all_positions(),db.occupied());checked=time.time()
                if not (capacity.allows('BUY') or capacity.allows('SELL')):
                    health.update(scan_paused_reason='ACCOUNT_POSITION_LIMIT');break
                health.update(scan_progress=index,scan_updated=time.time())
                if pair=='B-XAU_USDT' or pair in held or pair in capacity.pairs:continue
                try:
                    now=time.time()
                    quote=quotes.get(pair)
                    if quote is None:continue
                    if not capacity.allows('BUY') and (not capacity.allows('SELL') or quote.change_24h<=35):continue
                    if now-quote.timestamp>60:
                        quotes=market.quotes();quote=quotes.get(pair)
                        if quote is None:continue
                    daily=history.daily(pair,now)
                    end=int(now//DAY)*DAY
                    if not daily or len(daily)<100 or daily[-1].timestamp!=end-DAY:continue
                    candidate=evaluate(pair,quote,daily,now,allow_short=capacity.allows('SELL'),short_distance=config.short_distance)
                    if candidate is None and capacity.allows('BUY'):
                        recent_low=min(c.low for c in daily)
                        if quote.price>recent_low*Decimal('1.10'):continue
                        low,first=history.all_time(pair,now,daily)
                        intraday=market.four_hour(pair,now)
                        candidate=evaluate(pair,quote,daily,now,low,first,intraday,allow_short=capacity.allows('SELL'),short_distance=config.short_distance)
                    if candidate:
                        try:outbox.put_nowait(candidate)
                        except queue.Full:pass
                    stopping.wait(.05)
                except Exception as error:
                    emit('MARKET_DATA_SKIPPED',pair=pair,error_type=type(error).__name__,reason=str(error) if isinstance(error,(ValueError,MarketDataError)) else '')
            health.update(scan_completed=time.time())
        except Exception as error:
            health.update(scan_error=type(error).__name__)
            emit('SCANNER_ERROR',error_type=type(error).__name__)
        stopping.wait(max(1,config.scan_interval-(time.time()-started)))
    db.conn.close()


def run(config):
    config.validate();Path(config.database).parent.mkdir(parents=True,exist_ok=True)
    lock=open(config.database+'.lock','w');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    db=ScannerState(config.database,config.timezone)
    alerts=Alerts(config.bot_token,config.chat_id)
    market=MarketData();ex=Exchange(config,market);engine=Engine(config,db,ex,market,alerts.emit)
    stopping=threading.Event();outbox=queue.Queue(maxsize=32)
    health={'status':'starting','enabled':config.enabled,'updated_at':time.time(),'active_positions':0,
            'scan_seconds':config.scan_interval,'short_order_type':'limit_order',
            'short_distance':str(config.short_distance),'short_limit_seconds':config.short_limit_seconds}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path!='/health':self.send_error(404);return
            snapshot=dict(health);snapshot['fresh']=time.time()-snapshot['updated_at']<120
            ok=snapshot['fresh'] and snapshot['status']=='ok'
            self.send_response(200 if ok else 503);self.send_header('Content-Type','application/json');self.end_headers()
            self.wfile.write(json.dumps(snapshot).encode())
        def log_message(self,*args):pass
    server=HTTPServer(('0.0.0.0',config.health_port),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    worker=threading.Thread(target=scan,args=(config,outbox,health,alerts.emit,stopping),daemon=True);worker.start()
    alerts.emit('SCANNER_STARTED',enabled=config.enabled,max_positions=5,short='Resistance LIMIT / 3 USDT / 3x / crossed / TP 7%',
                long='6 USDT (minimum-size cap 6.50) / 1x / isolated / TP 6%',stop_loss='none',
                position_limits='All non-gold positions: 5 total / 3 short / 2 long',long_confirmation='4h consolidation or bullish reversal',
                short_distance=str(config.short_distance),short_limit_seconds=config.short_limit_seconds,scan_seconds=config.scan_interval)
    try:
        while True:
            failed=False
            for trade in db.rows():
                if trade['status'] in ('CLOSED','REJECTED'):continue
                try:engine.reconcile(trade)
                except Exception as error:
                    failed=True
                    alerts.emit('RECONCILE_ERROR',trade_id=trade['id'],pair=trade['pair'],error_type=type(error).__name__,
                                endpoint=getattr(error,'endpoint',''),http_status=getattr(error,'status',None),
                                reason=str(error) if hasattr(error,'endpoint') else '')
            try:
                capacity=engine.capacity()
                health.update(account_capacity=capacity.report(),capacity_checked=time.time())
            except Exception as error:
                failed=True;alerts.emit('SCANNER_ERROR',reason='ACCOUNT_CAPACITY_UNAVAILABLE',error_type=type(error).__name__)
            for _ in range(outbox.qsize()):
                try:candidate=outbox.get_nowait()
                except queue.Empty:break
                try:
                    if not failed:engine.enter(candidate)
                except ValueError as error:
                    alerts.emit('ENTRY_SKIPPED',pair=candidate.pair,reason=str(error))
                except MarketDataError as error:
                    alerts.emit('ENTRY_SKIPPED',pair=candidate.pair,reason=str(error))
                except Exception as error:
                    failed=True;alerts.emit('SCANNER_ERROR',pair=candidate.pair,error_type=type(error).__name__,
                        endpoint=getattr(error,'endpoint',''),http_status=getattr(error,'status',None))
                finally:outbox.task_done()
            if not worker.is_alive():
                failed=True;alerts.emit('SCANNER_ERROR',reason='DATA_WORKER_STOPPED')
            occupied=db.occupied()
            review=any(t['status'] in ('UNCERTAIN','CONFLICT','CLOSING') for t in occupied)
            health.update(status='degraded' if failed or health.get('scan_error') else 'review_required' if review else 'ok',
                          active_positions=len(occupied),updated_at=time.time())
            time.sleep(config.poll)
    finally:
        stopping.set();server.shutdown();alerts.close();db.conn.close();lock.close()
