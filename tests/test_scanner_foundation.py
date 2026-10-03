import tempfile
import unittest
from decimal import Decimal
from unittest.mock import Mock
from scanner.market import MarketData, MarketDataError
from scanner.state import ScannerState


class DiscoveryTests(unittest.TestCase):
    def test_discovery_uses_active_futures_api(self):
        data=MarketData();data.get=Mock(return_value=['B-ETH_USDT','B-XAU_USDT','B-ETH_USDT'])
        self.assertEqual(data.active_instruments(),['B-ETH_USDT','B-XAU_USDT'])
        self.assertEqual(data.get.call_args.args[1],{'margin_currency_short_name[]':'USDT'})
    def test_quote_filter_rejects_stale_pair_without_discarding_fresh_pairs(self):
        data=MarketData(clock=lambda:1000)
        data.get=Mock(return_value={'ts':1000000,'prices':{
            'B-ETH_USDT':{'ls':'10','pc':'35.1','btST':999000},
            'B-OLD_USDT':{'ls':'1','pc':'60','btST':100000}}})
        self.assertEqual(list(data.quotes()),['B-ETH_USDT'])
        self.assertEqual(data.quotes()['B-ETH_USDT'].change_24h,Decimal('35.1'))
    def test_stale_snapshot_rejected(self):
        data=MarketData(clock=lambda:1000)
        data.get=Mock(return_value={'ts':100000,'prices':{}})
        with self.assertRaises(MarketDataError):data.quotes()
    def test_candle_api_failure_is_not_empty_history(self):
        data=MarketData(clock=lambda:1000);data.get=Mock(return_value={'s':'error','data':[]})
        with self.assertRaises(MarketDataError):data.candles('B-X_USDT',0,600,'1',60)
    def test_forming_candle_excluded(self):
        data=MarketData(clock=lambda:100)
        def candle(t):return dict(time=t*1000,open=2,high=3,low=1,close=2,volume=1)
        data.get=Mock(return_value={'s':'ok','data':[candle(0),candle(60)]})
        self.assertEqual([c.timestamp for c in data.candles('B-X_USDT',0,100,'1',60)],[0])
    def test_gold_metadata_never_eligible(self):
        data=dict(pair='B-XAU_USDT',underlying_currency_short_name='XAU',position_currency_short_name='XAU',
                  status='active',quote_currency_short_name='USDT',unit_contract_value=1)
        self.assertFalse(MarketData.eligible_metadata(data))


class ReservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=self.tmp.name+'/scanner.sqlite3';self.db=ScannerState(self.path)
    def tearDown(self):
        self.db.conn.close();self.tmp.cleanup()
    def test_pending_orders_also_count_toward_five(self):
        for n in range(5):self.assertIsNotNone(self.db.reserve(str(n),f'B-C{n}_USDT',{},5)[0])
        self.assertEqual(self.db.reserve('six','B-SIX_USDT',{},5)[1],'POSITION_LIMIT')
    def test_two_connections_cannot_overbook(self):
        other=ScannerState(self.path)
        try:
            for n in range(5):self.db.reserve(str(n),f'B-C{n}_USDT',{},5)
            self.assertEqual(other.reserve('six','B-SIX_USDT',{},5)[1],'POSITION_LIMIT')
        finally:other.conn.close()
    def test_profit_lock_survives_restart_and_expires_next_local_day(self):
        now=1791054000
        ident,_=self.db.reserve('one','B-ETH_USDT',{},5,now)
        self.db.record_flat(ident,now,'0.20')
        self.db.conn.close();self.db=ScannerState(self.path)
        self.assertEqual(self.db.reserve('two','B-ETH_USDT',{},5,now+1)[1],'PROFIT_LOCKED_TODAY')
        self.assertIsNotNone(self.db.reserve('three','B-ETH_USDT',{},5,now+86400)[0])
    def test_unknown_pnl_blocks_coin_but_releases_position_slot(self):
        ident,_=self.db.reserve('one','B-ETH_USDT',{},1)
        self.db.record_flat(ident,1791054000)
        self.assertEqual(self.db.reserve('two','B-ETH_USDT',{},1)[1],'DUPLICATE_SIGNAL_OR_ACTIVE_COIN')
        self.assertIsNotNone(self.db.reserve('three','B-SOL_USDT',{},1)[0])
    def test_no_gold_reservations(self):
        with self.assertRaises(ValueError):self.db.reserve('one','B-XAU_USDT',{},5)
    def test_same_coin_cannot_open_twice(self):
        self.db.reserve('one','B-ETH_USDT',{},5)
        self.assertEqual(self.db.reserve('two','B-ETH_USDT',{},5)[1],'DUPLICATE_SIGNAL_OR_ACTIVE_COIN')
