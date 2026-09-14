import asyncio
import fcntl
import hashlib
import json
import sys
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from telethon import TelegramClient
from .config import Config
from .database import Database
from .exchange import CoinDCX
from .signals import extract_image, should_close_from_text
from .trading import Trader

async def main():
    c = Config()
    login = '--login' in sys.argv
    c.validate(login=login)
    Path(c.session).parent.mkdir(parents=True, exist_ok=True)
    Path(c.database).parent.mkdir(parents=True, exist_ok=True)
    # Prevent concurrent containers using the same persistent state/session.
    lock = open(c.database + '.lock', 'w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    client = TelegramClient(c.session, c.api_id, c.api_hash, auto_reconnect=True, flood_sleep_threshold=60)
    if login:
        await client.start(phone=c.phone or None)
        await client.get_dialogs()  # Cache channel access hash for later numeric-ID lookup.
        print('Telegram user session saved. Start the trader service next.')
        await client.disconnect()
        return
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError('Run docker compose run --rm trader python -m app.main --login first')
    await client.get_dialogs()
    channel = await client.get_entity(c.channel)
    db = Database(c.database)
    ex = CoinDCX(c)
    info = await asyncio.to_thread(ex.discover)
    db.event('INSTRUMENT_DISCOVERED', pair=ex.pair, quantity_step=info['quantity_increment'], price_tick=info['price_increment'])
    # All lifecycle work below runs in one dedicated worker, preserving serialization.
    db.conn.close()
    health = {'status': 'starting', 'telegram': True, 'coindcx': True, 'database': True,
              'dry_run': c.dry, 'trading_enabled': c.enabled, 'active_trade': False,
              'last_telegram_message_processed': None, 'updated_at': time.time()}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != '/health':
                self.send_error(404)
                return
            state = dict(health)
            fresh = time.time() - state['updated_at'] < 90
            ok = fresh and state['status'] == 'ok'
            self.send_response(200 if ok else 503)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({**state, 'fresh': fresh}).encode())
        def log_message(self, *_):
            pass
    server = HTTPServer(('0.0.0.0', 8080), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    # All DB/exchange work uses one executor thread; Telegram stays on the asyncio loop.
    from concurrent.futures import ThreadPoolExecutor
    executor = ThreadPoolExecutor(max_workers=1)
    loop = asyncio.get_running_loop()
    async def worker(fn, *args):
        return await loop.run_in_executor(executor, fn, *args)
    db = await worker(Database, c.database)
    trader = Trader(c, db, ex)
    await worker(db.event, 'TELEGRAM_CONNECTED')
    offset = await worker(db.offset, c.channel)
    if offset is None:
        latest = await client.get_messages(channel, limit=1)
        offset = latest[0].id if latest else 0
        await worker(db.advance, c.channel, offset)
    try:
        while True:
            try:
                await worker(trader.reconcile)
                # Ordered polling backfills gaps after disconnects and restarts.
                # Bound each batch so position reconciliation cannot be starved by a long backlog.
                async for message in client.iter_messages(channel, min_id=offset, reverse=True, limit=100):
                    text = message.raw_text or ''
                    if message.photo:
                        age = time.time() - message.date.timestamp()
                        if age <= c.max_age:
                            path = Path(c.database).parent / 'images' / f'{message.id}.jpg'
                            path.parent.mkdir(parents=True, exist_ok=True)
                            await message.download_media(file=str(path))
                            try:
                                signal, raw = await worker(extract_image, path)
                                image_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                                data = {**signal.data(), 'raw_ocr_text': raw, 'image_path': str(path),
                                        'reply_to_message_id': message.reply_to_msg_id}
                                signal_id = await worker(db.signal, c.channel, message.id, image_hash, signal.digest(), data)
                                if signal_id is None:
                                    await worker(db.event, 'SIGNAL_DUPLICATE')
                                elif time.time() - message.date.timestamp() <= c.max_age:
                                    await worker(trader.enter, signal_id, message.id, signal)
                            except ValueError as error:
                                await worker(db.event, str(error))
                        else:
                            await worker(db.event, 'STALE_IMAGE_SKIPPED')
                    elif not message.media and should_close_from_text(text, c.keywords):
                        trade = await worker(db.active)
                        # Text from before the active image cannot close it.
                        if trade and message.id > trade['data']['message_id']:
                            keyword = next(k for k in c.keywords if should_close_from_text(text, (k,)))
                            await worker(trader.close, 'TELEGRAM_' + keyword.upper(), message.reply_to_msg_id)
                    offset = message.id
                    await worker(db.advance, c.channel, offset)
                active = await worker(db.active)
                status = active['status'] if active else None
                health.update(status='ok' if status not in ('UNCERTAIN', 'ISOLATION_CONFLICT', 'CLOSING') else 'review_required',
                              telegram=client.is_connected(), coindcx=time.time()-ex.last_ok < 90,
                              database=True, active_trade=bool(active), active_status=status,
                              last_telegram_message_processed=offset, updated_at=time.time())
                if not health['telegram'] or not health['coindcx']:
                    health['status'] = 'degraded'
            except Exception as error:
                health.update(status='degraded', updated_at=time.time())
                # Exception class only: never leak session credentials or response bodies.
                await worker(db.event, 'LOOP_ERROR_' + type(error).__name__)
            await asyncio.sleep(c.poll)
    finally:
        server.shutdown()
        await client.disconnect()
        await worker(db.conn.close)
        executor.shutdown()
        lock.close()

if __name__ == '__main__':
    asyncio.run(main())
