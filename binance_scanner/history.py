"""Durable fills with ID pagination, bounded time windows and coverage checks."""
import json
import time
from scanner.market import DAY
from .client import BinanceError
from .market import symbol_name


class FillHistory:
    def __init__(self,client,state,clock=time.time):
        self.client,self.db,self.clock=client,state,clock
        state.conn.executescript('''
          CREATE TABLE IF NOT EXISTS binance_fills (
            symbol TEXT NOT NULL, id INTEGER NOT NULL, time INTEGER NOT NULL, payload TEXT NOT NULL,
            PRIMARY KEY(symbol,id));
          CREATE TABLE IF NOT EXISTS binance_fill_coverage (
            symbol TEXT PRIMARY KEY, start INTEGER NOT NULL, end INTEGER NOT NULL);
        ''')

    def window(self,symbol,start,end):
        params=dict(symbol=symbol,startTime=start,endTime=end,limit=1000)
        result={};last_id=None
        for _ in range(100):
            rows=self.client.call('GET','/fapi/v1/userTrades',params)
            if not isinstance(rows,list):raise BinanceError('Invalid Binance fill history','userTrades')
            for r in rows:
                ident=int(r['id'])
                if r['symbol']!=symbol or r.get('positionSide','BOTH')!='BOTH':
                    raise BinanceError('Unexpected fill ownership or position mode','userTrades')
                if last_id is not None and ident<=last_id:raise BinanceError('Non-progressing fill pagination','userTrades')
                last_id=ident
                if start<=int(r['time'])<=end:result[ident]=r
            if len(rows)<1000 or rows and int(rows[-1]['time'])>end:return list(result.values())
            params=dict(symbol=symbol,fromId=last_id+1,limit=1000)
        raise BinanceError('Incomplete Binance fill pagination','userTrades')

    def fills(self,pair,since):
        symbol=symbol_name(pair);start=int(since*1000);end=int(self.clock()*1000)
        coverage=self.db.conn.execute('SELECT start,end FROM binance_fill_coverage WHERE symbol=?',(symbol,)).fetchone()
        intervals=[]
        if coverage:
            if start<coverage[0]:intervals.append((start,coverage[0]-1))
            intervals.append((max(coverage[0],coverage[1]-60000),end))
        else:intervals.append((start,end))
        additions=[]
        for low,high in intervals:
            if low<end-89*DAY*1000:
                raise BinanceError('Fill history gap exceeds Binance retention; restore the Binance database','userTrades')
            while low<=high:
                stop=min(high,low+7*DAY*1000-1)
                additions.extend(self.window(symbol,low,stop));low=stop+1
        # Coverage advances only after all reads succeeded. Primary keys prevent
        # overlap/restart double counting without collapsing genuine partial fills.
        with self.db.conn:
            for r in additions:
                self.db.conn.execute('INSERT OR REPLACE INTO binance_fills VALUES(?,?,?,?)',
                    (symbol,int(r['id']),int(r['time']),json.dumps(r)))
            self.db.conn.execute('INSERT OR REPLACE INTO binance_fill_coverage VALUES(?,?,?)',
                (symbol,min(start,coverage[0]) if coverage else start,end))
        return [json.loads(r[0]) for r in self.db.conn.execute(
            'SELECT payload FROM binance_fills WHERE symbol=? AND time>=? AND time<=? ORDER BY time,id',(symbol,start,end))]
