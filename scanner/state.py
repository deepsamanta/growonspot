"""Durable scanner reservations and per-coin daily profit lockouts."""
import json
import sqlite3
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

OCCUPIED = ('RESERVED', 'SUBMITTING', 'SUBMITTED', 'OPEN', 'CLOSING', 'UNCERTAIN', 'PROTECTING', 'CONFLICT')


class ScannerState:
    def __init__(self, path, timezone='Asia/Kolkata'):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.zone = ZoneInfo(timezone)
        self.conn = sqlite3.connect(path, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript('''
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS scanner_trades (
                id INTEGER PRIMARY KEY, signal_key TEXT UNIQUE NOT NULL,
                pair TEXT NOT NULL, status TEXT NOT NULL, data_json TEXT NOT NULL,
                created_at REAL NOT NULL, closed_at REAL, realized_pnl TEXT);
            CREATE UNIQUE INDEX IF NOT EXISTS one_scanner_trade_per_coin
                ON scanner_trades(pair) WHERE status NOT IN ('CLOSED','REJECTED');
            CREATE TABLE IF NOT EXISTS scanner_profit_days (
                pair TEXT NOT NULL, day TEXT NOT NULL, trade_id INTEGER NOT NULL,
                PRIMARY KEY(pair,day));
            CREATE TABLE IF NOT EXISTS scanner_cache (
                key TEXT PRIMARY KEY, data_json TEXT NOT NULL, updated_at REAL NOT NULL);
        ''')

    def day(self, when):
        return datetime.fromtimestamp(when, self.zone).date().isoformat()

    def rows(self):
        return [{**dict(r), 'data': json.loads(r['data_json'])}
                for r in self.conn.execute('SELECT * FROM scanner_trades ORDER BY id')]

    def reserve(self, signal_key, pair, data, max_positions, now=None, side_limits=None):
        """Pending/uncertain submissions occupy slots before exchange writes."""
        if max_positions < 1 or max_positions > 5:
            raise ValueError('Scanner position cap must be between 1 and 5')
        if pair == 'B-XAU_USDT' or pair.rsplit('-', 1)[-1].split('_')[0] in ('XAU','XAUUSD'):
            raise ValueError('Gold is reserved for the existing bot')
        now = time.time() if now is None else now
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            if self.conn.execute('SELECT 1 FROM scanner_profit_days WHERE pair=? AND day=?',
                                 (pair, self.day(now))).fetchone():
                self.conn.rollback()
                return None, 'PROFIT_LOCKED_TODAY'
            count = self.conn.execute('SELECT count(*) FROM scanner_trades WHERE status IN ('+','.join('?' for _ in OCCUPIED)+')', OCCUPIED).fetchone()[0]
            if count >= max_positions:
                self.conn.rollback()
                return None, 'POSITION_LIMIT'
            if side_limits:
                side=data['side']
                same=sum(1 for r in self.occupied() if r['data'].get('side') in (side,None))
                if same>=side_limits[side]:
                    self.conn.rollback()
                    return None, 'SIDE_POSITION_LIMIT'
            cur = self.conn.execute("INSERT INTO scanner_trades(signal_key,pair,status,data_json,created_at) VALUES(?,?,'RESERVED',?,?)",
                                    (signal_key,pair,json.dumps(data),now))
            self.conn.commit()
            return cur.lastrowid, None
        except sqlite3.IntegrityError:
            self.conn.rollback()
            return None, 'DUPLICATE_SIGNAL_OR_ACTIVE_COIN'
        except Exception:
            self.conn.rollback()
            raise

    def update(self, trade_id, status, **changes):
        row = self.conn.execute('SELECT data_json FROM scanner_trades WHERE id=?',(trade_id,)).fetchone()
        if row is None:
            raise ValueError('Unknown scanner trade')
        data = {**json.loads(row[0]), **changes}
        with self.conn:
            self.conn.execute('UPDATE scanner_trades SET status=?,data_json=? WHERE id=?',(status,json.dumps(data),trade_id))

    def claim_average(self, trade_id, intent):
        """Commit the one addition allowance before any create request can leave."""
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            row=self.get(trade_id)
            if row is None or row['status']!='OPEN' or row['data'].get('averaging_used'):
                self.conn.rollback();return False
            data={**row['data'],'averaging_used':True,'averaging_source':'BOT','average_order':intent}
            self.conn.execute('UPDATE scanner_trades SET data_json=? WHERE id=?',(json.dumps(data),trade_id))
            self.conn.commit();return True
        except Exception:
            self.conn.rollback();raise

    def record_flat(self, trade_id, closed_at, realized_pnl=None):
        """Unknown PnL blocks that coin until transaction reconciliation completes."""
        pnl = None if realized_pnl is None else Decimal(str(realized_pnl))
        if pnl is not None and not pnl.is_finite():
            raise ValueError('Invalid realized PnL')
        row = self.conn.execute('SELECT pair FROM scanner_trades WHERE id=?',(trade_id,)).fetchone()
        if row is None:
            raise ValueError('Unknown scanner trade')
        with self.conn:
            self.conn.execute('UPDATE scanner_trades SET status=?,closed_at=?,realized_pnl=? WHERE id=?',
                ('PNL_PENDING' if pnl is None else 'CLOSED',closed_at,None if pnl is None else str(pnl),trade_id))
            if pnl is not None and pnl > 0:
                self.conn.execute('INSERT OR IGNORE INTO scanner_profit_days VALUES(?,?,?)',
                                  (row[0],self.day(closed_at),trade_id))

    def cache(self, key, max_age=None):
        row = self.conn.execute('SELECT data_json,updated_at FROM scanner_cache WHERE key=?',(key,)).fetchone()
        if row is None or (max_age is not None and time.time()-row[1]>max_age):
            return None
        return json.loads(row[0])

    def save_cache(self, key, data):
        with self.conn:
            self.conn.execute('INSERT INTO scanner_cache VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET data_json=excluded.data_json,updated_at=excluded.updated_at',
                              (key,json.dumps(data),time.time()))

    def lock_profit(self, pair, when, trade_id=0):
        with self.conn:
            self.conn.execute('INSERT OR IGNORE INTO scanner_profit_days VALUES(?,?,?)',
                              (pair,self.day(when),trade_id))

    def occupied(self):
        return [r for r in self.rows() if r['status'] in OCCUPIED]

    def get(self, trade_id):
        row=self.conn.execute('SELECT * FROM scanner_trades WHERE id=?',(trade_id,)).fetchone()
        return {**dict(row),'data':json.loads(row['data_json'])} if row else None
