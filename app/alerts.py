"""Best-effort Telegram alerts, isolated from the trading worker."""
import json
import queue
import threading
import time
import requests

PREFIX = '[XAU BOT]'
EVENTS = {
    'SIGNAL_DUPLICATE_TICKET': 'Update ignored: this trade ticket was already traded',
    'CLOSE_KEYWORD_DETECTED': 'Close instruction received from Telegram',
    'MESSAGE_PROCESSING_ERROR': 'Message processing failed; review logs',
    'TELEGRAM_POLL_ERROR': 'Telegram reading failed; retrying',
    'RECONCILIATION_ERROR': 'Exchange reconciliation failed; Telegram processing continues',
    'BOT_STARTED': 'Monitoring started',
    'BOT_FATAL': 'Bot stopped with an error; review VPS logs',
    'ORDER_SUBMITTED': 'Entry submitted; fill not yet confirmed',
    'POSITION_OPEN': 'Position open; exchange SL/TP confirmed',
    'POSITION_CLOSE_REQUESTED': 'Exit requested; closure not yet confirmed',
    'POSITION_CLOSED': 'Position closed',
    'ORDER_UNCERTAIN': 'Entry failed or outcome uncertain; review CoinDCX before retrying',
    'ORDER_UNCERTAIN_REQUIRES_REVIEW': 'Entry still unresolved; new entries blocked',
    'ISOLATION_CONFLICT': 'Position changed unexpectedly; manual review required',
    'PROTECTION_FAILED_EMERGENCY_EXIT': 'SL/TP verification failed; attempting emergency exit',
    'EXIT_REQUEST_FAILED': 'Exit failed or outcome uncertain; review CoinDCX immediately',
    'EXIT_UNCONFIRMED': 'Position remains open after exit request; manual review required',
    'DAILY_LIMIT_REACHED': 'Trade skipped: daily limit reached',
    'EXTERNAL_POSITION_BLOCKS_ENTRY': 'Trade skipped: existing XAU position',
    'EXTERNAL_ORDER_BLOCKS_ENTRY': 'Trade skipped: existing XAU order',
    'TRADING_DISABLED': 'Trade skipped: entries disabled in configuration',
    'SIGNAL_IGNORED_ACTIVE_POSITION': 'Trade skipped: bot already has an active position',
    'SIGNAL_REJECTED': 'Image rejected; no order submitted',
    'WOULD_OPEN': 'DRY RUN: simulated entry only',
}
FIELDS = ('trade_id', 'message_id', 'order_id', 'side', 'quantity', 'actual_fill_price',
          'sl', 'tp', 'margin', 'leverage', 'dry', 'reason', 'error_type', 'endpoint', 'http_status', 'ticket_id')

class TelegramAlerts:
    def __init__(self, token, chat_id, start_worker=True):
        self.token, self.chat_id = token.strip(), chat_id.strip()
        self.enabled = bool(self.token and self.chat_id)
        self.pending = queue.Queue(maxsize=100)
        self.recent = {}
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.http = requests.Session()
        self.thread = None
        if self.enabled and start_worker:
            self.thread = threading.Thread(target=self._run, daemon=True, name='telegram-alerts')
            self.thread.start()

    def emit(self, kind, **data):
        if not self.enabled:
            return
        title = EVENTS.get(kind)
        if title is None and kind.startswith('LOOP_ERROR_'):
            title = 'Bot loop error; retrying (check VPS logs)'
        if title is None:
            return
        key = (kind, data.get('trade_id'), data.get('message_id'))
        now = time.monotonic()
        with self.lock:
            self.recent = {k: t for k, t in self.recent.items() if now - t < 300}
            if key in self.recent:
                return
            lines = [PREFIX + ' ' + title, 'Event: ' + kind]
            lines += [f'{k}: {str(data[k])[:300]}' for k in FIELDS if k in data]
            try:
                self.pending.put_nowait('\n'.join(lines)[:4000])
            except queue.Full:
                self._log('ALERT_QUEUE_FULL')
                return
            self.recent[key] = now

    @staticmethod
    def _log(event):
        # Never log request URLs, response bodies, tokens, or raw exception messages.
        print(json.dumps({'event': event, 'time': time.time()}), flush=True)

    def _deliver(self, text):
        for attempt in range(3):
            delay = 2 ** attempt
            try:
                response = self.http.post(f'https://api.telegram.org/bot{self.token}/sendMessage',
                    json={'chat_id': self.chat_id, 'text': text}, timeout=(3, 5))
                result = response.json()
                if response.status_code == 200 and result.get('ok') is True:
                    return True
                code = result.get('error_code', response.status_code)
                if code == 429:
                    delay = max(1, float(result.get('parameters', {}).get('retry_after', delay)))
                    if delay > 30:
                        break  # Do not block the alert queue for a long flood-wait.
                elif code < 500:
                    break
            except (requests.RequestException, ValueError, TypeError, AttributeError):
                pass
            if attempt == 2 or self.stopping.wait(delay):
                break
        self._log('TELEGRAM_ALERT_FAILED')
        return False

    def _run(self):
        while not self.stopping.is_set() or not self.pending.empty():
            try:
                text = self.pending.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._deliver(text)
            except Exception:
                self._log('TELEGRAM_ALERT_FAILED')
            finally:
                self.pending.task_done()

    def close(self, timeout=10):
        self.stopping.set()
        if self.thread:
            self.thread.join(timeout=timeout)
        if not self.thread or not self.thread.is_alive():
            self.http.close()
