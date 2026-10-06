"""Account-wide non-gold capacity, including durable in-flight entries."""
from dataclasses import dataclass
import time
from .market import decimal, GOLD


def is_gold(pair):
    return pair.split('-', 1)[-1].split('_')[0].upper() in GOLD


@dataclass(frozen=True)
class Capacity:
    pairs: frozenset
    longs: frozenset
    shorts: frozenset
    max_total: int = 5
    max_longs: int = 2
    max_shorts: int = 3

    @property
    def within_limits(self):
        return len(self.pairs)<=self.max_total and len(self.longs)<=self.max_longs and len(self.shorts)<=self.max_shorts

    def allows(self, side):
        if not self.within_limits or len(self.pairs)>=self.max_total:return False
        return len(self.longs)<self.max_longs if side=='BUY' else len(self.shorts)<self.max_shorts

    def report(self):
        return {'total':len(self.pairs),'longs':len(self.longs),'shorts':len(self.shorts),
                'max_total':self.max_total,'max_longs':self.max_longs,'max_shorts':self.max_shorts,
                'long_available':self.allows('BUY'),'short_available':self.allows('SELL')}


def account_capacity(positions, reservations, limits=(5,2,3)):
    pairs,longs,shorts=set(),set(),set()
    for p in positions:
        pair=p['pair']
        if is_gold(pair):continue
        active=decimal(p.get('active_pos') or 0)
        buy=decimal(p.get('inactive_pos_buy') or 0)
        sell=decimal(p.get('inactive_pos_sell') or 0)
        if active or buy or sell:pairs.add(pair)
        if active>0 or buy>0:longs.add(pair)
        if active<0 or sell>0:shorts.add(pair)
    for row in reservations:
        pair=row['pair']
        if is_gold(pair):continue
        pairs.add(pair)
        side=row['data'].get('side')
        if side!='SELL':longs.add(pair)
        if side!='BUY':shorts.add(pair)
    return Capacity(frozenset(pairs),frozenset(longs),frozenset(shorts),*limits)


class BalanceCapacity:
    """Each $50 above a durable baseline adds a slot: short, long, short, long."""
    def __init__(self,exchange,state):
        self.ex,self.db=exchange,state
        self.checked=0;self.balance=None

    def get(self):
        positions=self.ex.all_positions()  # Before any instrument discovery.
        if self.balance is None or time.time()-self.checked>=15:
            self.balance=decimal(self.ex.wallet_balance())
            if self.balance<0:raise ValueError('Invalid wallet balance')
            self.checked=time.time()
        baseline=self.db.balance_baseline(self.balance)
        extra=int(max(0,self.balance-baseline)//50)
        return account_capacity(positions,self.db.occupied(),(5+extra,2+extra//2,3+(extra+1)//2))
