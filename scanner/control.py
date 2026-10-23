"""Durable Telegram pause/resume; only the execution thread changes trading state."""
import queue
import threading
import time
import requests

KEY = 'telegram:control'


class RuntimeConfig:
    def __init__(self, base, paused=False):
        self.base = base
        self.paused = paused
        self.scan_wakeup = threading.Event()

    def __getattr__(self, name):
        return getattr(self.base, name)

    @property
    def enabled(self):
        return self.base.enabled and not self.paused

    def wait_scan(self, stopping, seconds):
        deadline = time.monotonic() + seconds
        while not stopping.is_set() and time.monotonic() < deadline:
            if self.scan_wakeup.wait(min(1, max(0, deadline-time.monotonic()))):
                self.scan_wakeup.clear()
                return


def parse_command(update, chat_id, username, since):
    message = update.get('message') or update.get('channel_post')
    if not isinstance(message, dict):
        return None
    if str(message.get('chat', {}).get('id')) != str(chat_id):
        return None
    if message.get('date', 0) < since or message.get('forward_origin') or message.get('forward_date'):
        return None
    text = message.get('text', '').strip().lower()
    if text.startswith('/'):
        text = text[1:]
        if '@' in text:
            text, addressed = text.split('@', 1)
            if addressed != username.lower():
                return None
    return text if text in ('start', 'stop', 'status') else None


class TelegramControl:
    def __init__(self, config, db, alerts, session=None):
        self.c, self.db, self.alerts = config, db, alerts
        self.http = session or requests.Session()
        self.state = db.cache(KEY)
        if self.state is None:
            self.state = {'paused': False, 'offset': 0, 'since': int(time.time())}
            db.save_cache(KEY, self.state)
        config.paused = self.state['paused']
        self.pending = queue.Queue(maxsize=1)
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.health = {'connected': False, 'last_error': None}

    def api(self, method, body):
        try:
            response = self.http.post('https://api.telegram.org/bot'+self.c.bot_token+'/'+method,
                                      json=body, timeout=(5, 25))
            result = response.json()
        except (requests.RequestException, ValueError):
            raise RuntimeError('TELEGRAM_TRANSPORT_ERROR') from None
        if not response.ok or not result.get('ok'):
            raise RuntimeError('TELEGRAM_HTTP_'+str(response.status_code))
        return result['result']

    def authorized(self, update, chat_id):
        message = update.get('message') or update.get('channel_post') or {}
        chat = message.get('chat', {})
        if str(chat.get('id')) != str(chat_id):
            return False
        if chat.get('type') == 'channel':
            return 'channel_post' in update
        sender = message.get('from', {})
        if sender.get('is_bot'):
            return False
        if chat.get('type') == 'private':
            return sender.get('id') == chat.get('id')
        # Anonymous administrators post as the configured group itself.
        if message.get('sender_chat', {}).get('id') == chat.get('id'):
            return True
        if not sender.get('id'):
            return False
        member = self.api('getChatMember', {'chat_id': chat_id, 'user_id': sender['id']})
        return member.get('status') in ('creator', 'administrator')

    def run(self):
        initialized = False
        while not self.stopping.is_set():
            try:
                if not initialized:
                    if self.api('getWebhookInfo', {}).get('url'):
                        raise RuntimeError('TELEGRAM_WEBHOOK_ALREADY_CONFIGURED')
                    username = self.api('getMe', {})['username']
                    chat_id = self.api('getChat', {'chat_id': self.c.chat_id})['id']
                    initialized = True
                updates = self.api('getUpdates', {'offset': self.state['offset'], 'timeout': 20,
                                                  'allowed_updates': ['message', 'channel_post']})
                self.health.update(connected=True, last_error=None, checked_at=time.time())
                for update in updates:
                    if update['update_id'] < self.state['offset']:
                        continue
                    command = parse_command(update, chat_id, username, self.state['since'])
                    if command and not self.authorized(update, chat_id):
                        command = None
                    done = threading.Event()
                    self.pending.put((update['update_id'], command, done))
                    while not done.wait(.2):
                        if self.stopping.is_set():
                            return
            except Exception as error:
                # Never log request URLs: they contain the Telegram token.
                reason = str(error) if isinstance(error, RuntimeError) else type(error).__name__
                self.health.update(connected=False, last_error=reason)
                self.alerts.emit('COMMAND_ERROR', reason=reason)
                self.stopping.wait(10)

    def apply_pending(self):
        """Called at execution boundaries, never concurrently with exchange writes."""
        while True:
            try:
                update_id, command, done = self.pending.get_nowait()
            except queue.Empty:
                return
            if update_id < self.state['offset']:
                done.set()
                continue
            state = dict(self.state, offset=update_id+1)
            if command in ('stop', 'start'):
                state['paused'] = command == 'stop'
            self.db.save_cache(KEY, state)  # Persist before acknowledging Telegram.
            self.state = state
            self.c.paused = state['paused']
            if command:
                if command in ('stop', 'start'):
                    self.c.scan_wakeup.set()
                if self.c.enabled:
                    text = '[MARKET SCANNER] ACTIVE — new entries and automatic averaging enabled.'
                elif self.c.paused:
                    text = ('[MARKET SCANNER] INACTIVE — new entries and automatic averaging paused. '
                            'Existing positions and orders are unchanged and profit monitoring continues. '
                            'Pending orders may still fill.')
                else:
                    text = '[MARKET SCANNER] INACTIVE — trading is disabled in server configuration.'
                self.alerts.reply(text)
            done.set()

    def start(self):
        self.thread.start()

    def close(self):
        self.stopping.set()
        self.thread.join(timeout=2)
