import sqlite3
import json
import time
from pathlib import Path
from .entry_rules import ticket_from_text

class Database:
    def __init__(self, path, alerts=None):
        self.alerts = alerts
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript('''
          PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
          CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY, channel INTEGER NOT NULL, message INTEGER NOT NULL,
            image_sha256 TEXT UNIQUE, signal_hash TEXT UNIQUE, data_json TEXT NOT NULL,
            status TEXT NOT NULL, created_at REAL NOT NULL, UNIQUE(channel,message));
          CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY, signal_id INTEGER NOT NULL UNIQUE,
            status TEXT NOT NULL, data_json TEXT NOT NULL, opened_at REAL NOT NULL,
            closed_at REAL, loss_charge REAL NOT NULL DEFAULT 0);
          CREATE UNIQUE INDEX IF NOT EXISTS one_active_trade ON trades ((1))
            WHERE status NOT IN ('CLOSED','REJECTED');
          CREATE TABLE IF NOT EXISTS telegram_state (
            channel INTEGER PRIMARY KEY, last_processed_message_id INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS bot_events (
            id INTEGER PRIMARY KEY, event_type TEXT, data_json TEXT, created_at REAL);
          CREATE TABLE IF NOT EXISTS signal_context (
            channel INTEGER PRIMARY KEY, data_json TEXT NOT NULL);
          PRAGMA user_version=2;
        ''')

    def event(self, kind, **data):
        print(json.dumps({'event': kind, 'time': time.time(), **data}), flush=True)
        self.conn.execute('INSERT INTO bot_events(event_type,data_json,created_at) VALUES(?,?,?)', (kind, json.dumps(data), time.time()))
        self.conn.commit()
        if self.alerts:
            try:
                self.alerts.emit(kind, **data)
            except Exception:
                print(json.dumps({'event': 'ALERT_ENQUEUE_FAILED'}), flush=True)

    def offset(self, channel):
        row = self.conn.execute('SELECT last_processed_message_id FROM telegram_state WHERE channel=?', (channel,)).fetchone()
        return row[0] if row else None

    def advance(self, channel, message):
        self.conn.execute('INSERT INTO telegram_state VALUES(?,?) ON CONFLICT(channel) DO UPDATE SET last_processed_message_id=max(last_processed_message_id,excluded.last_processed_message_id)', (channel, message))
        self.conn.commit()

    def signal(self, channel, message, image_hash, signal_hash, data):
        try:
            cur = self.conn.execute('INSERT INTO signals(channel,message,image_sha256,signal_hash,data_json,status,created_at) VALUES(?,?,?,?,?,?,?)', (channel, message, image_hash, signal_hash, json.dumps(data), 'SIGNAL_VALIDATED', time.time()))
            self.conn.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            self.conn.rollback()
            return None

    def active(self):
        row = self.conn.execute("SELECT * FROM trades WHERE status NOT IN ('CLOSED','REJECTED')").fetchone()
        return {**dict(row), 'data': json.loads(row['data_json'])} if row else None

    def reserve(self, signal_id, data):
        cur = self.conn.execute("INSERT INTO trades(signal_id,status,data_json,opened_at) VALUES(?,'SUBMITTING',?,?)", (signal_id, json.dumps(data), time.time()))
        self.conn.commit()
        return cur.lastrowid

    def update(self, trade, status, **changes):
        data = {**trade['data'], **changes}
        self.conn.execute('UPDATE trades SET status=?,data_json=? WHERE id=?', (status, json.dumps(data), trade['id']))
        self.conn.commit()
        if status != trade['status'] and status in ('UNCERTAIN', 'ISOLATION_CONFLICT'):
            kind = 'ORDER_UNCERTAIN' if status == 'UNCERTAIN' else status
            self.event(kind, trade_id=trade['id'], message_id=data.get('message_id'))

    def close(self, trade, reason, loss_charge):
        self.update(trade, 'CLOSED', exit_reason=reason)
        self.conn.execute('UPDATE trades SET closed_at=?,loss_charge=? WHERE id=?', (time.time(), float(loss_charge), trade['id']))
        self.conn.commit()
        self.event('POSITION_CLOSED', trade_id=trade['id'], reason=reason)

    def daily(self):
        start = int(time.time() // 86400) * 86400
        count = self.conn.execute("SELECT count(*) FROM trades WHERE opened_at>=? AND status!='REJECTED'", (start,)).fetchone()[0]
        loss = self.conn.execute('SELECT coalesce(sum(loss_charge),0) FROM trades WHERE closed_at>=?', (start,)).fetchone()[0]
        return count, loss

    def has_traded_ticket(self, channel, ticket_id):
        if not ticket_id:
            return False
        rows = self.conn.execute("""SELECT signals.data_json FROM signals
            JOIN trades ON trades.signal_id=signals.id
            WHERE signals.channel=? AND trades.status!='REJECTED'""", (channel,))
        for row in rows:
            data = json.loads(row[0])
            existing = data.get('ticket_id') or ticket_from_text(data.get('raw_ocr_text', ''))
            if existing == ticket_id:
                return True
        return False

    def context(self, channel):
        row = self.conn.execute('SELECT data_json FROM signal_context WHERE channel=?', (channel,)).fetchone()
        return json.loads(row[0]) if row else {}

    def save_context(self, channel, data):
        self.conn.execute('INSERT INTO signal_context VALUES(?,?) ON CONFLICT(channel) DO UPDATE SET data_json=excluded.data_json',
                          (channel, json.dumps(data)))
        self.conn.commit()
