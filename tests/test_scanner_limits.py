import tempfile
import threading
import time
import unittest
from decimal import Decimal as D
from unittest.mock import Mock,patch
from scanner.capacity import account_capacity
from scanner.config import ScannerConfig
from scanner.market import Candle,MarketData
from scanner.state import ScannerState
from scanner.strategy import long_confirmation
from scanner.exchange import Exchange,ExchangeError,quantity
from scanner.runner import scan
from test_scanner_strategy import INFO


def position(pair,amount,**kwargs):
    return {'pair':pair,'active_pos':amount,**kwargs}

class CapacityTests(unittest.TestCase):
    def test_all_non_gold_positions_count_including_external_doge(self):
        rows=[position(f'B-C{i}_USDT',1) for i in range(5)]+[position('B-DOGE_USDT',256),position('B-XAU_USDT',.005)]
        c=account_capacity(rows,[])
        self.assertEqual((len(c.pairs),len(c.longs),len(c.shorts)),(6,6,0))
        self.assertFalse(c.allows('BUY'));self.assertFalse(c.allows('SELL'))
    def test_three_short_two_long_split(self):
        rows=[position(f'B-S{i}_USDT',-1) for i in range(3)]
        c=account_capacity(rows,[])
        self.assertFalse(c.allows('SELL'));self.assertTrue(c.allows('BUY'))
        c=account_capacity(rows+[position('B-L1_USDT',1),position('B-L2_USDT',1)],[])
        self.assertFalse(c.allows('BUY'));self.assertFalse(c.allows('SELL'))
    def test_pending_and_uncertain_entries_consume_side_slots_without_double_counting(self):
        reservations=[{'pair':'B-A_USDT','data':{'side':'BUY'}},{'pair':'B-B_USDT','data':{'side':'BUY'}}]
        c=account_capacity([position('B-A_USDT',2)],reservations)
        self.assertEqual(len(c.pairs),2);self.assertFalse(c.allows('BUY'));self.assertTrue(c.allows('SELL'))
    def test_external_pending_order_consumes_capacity(self):
        c=account_capacity([position('B-A_USDT',0,inactive_pos_sell=1),position('B-B_USDT',0,inactive_pos_buy=1)],[])
        self.assertEqual((len(c.pairs),len(c.shorts),len(c.longs)),(2,1,1))
    def test_database_side_reservations_are_atomic(self):
        with tempfile.TemporaryDirectory() as f:
            a=ScannerState(f+'/db');b=ScannerState(f+'/db')
            for n in range(2):self.assertIsNotNone(a.reserve(str(n),f'B-C{n}_USDT',{'side':'BUY'},5,side_limits={'BUY':2,'SELL':3})[0])
            self.assertEqual(b.reserve('third','B-C3_USDT',{'side':'BUY'},5,side_limits={'BUY':2,'SELL':3})[1],'SIDE_POSITION_LIMIT')
            self.assertIsNotNone(b.reserve('short','B-C4_USDT',{'side':'SELL'},5,side_limits={'BUY':2,'SELL':3})[0])
            a.conn.close();b.conn.close()
    def test_scanner_checks_account_before_any_coin_discovery(self):
        with tempfile.TemporaryDirectory() as f:
            stop=threading.Event();stop.wait=Mock(side_effect=lambda _:stop.set())
            ex=Mock();ex.wallet_balance.return_value=D(50);ex.all_positions.return_value=[position(f'B-C{i}_USDT',1) for i in range(6)]
            market=Mock();health={}
            with patch('scanner.runner.Exchange',return_value=ex),patch('scanner.runner.MarketData',return_value=market):
                scan(ScannerConfig(database=f+'/db',enabled=True),Mock(),health,Mock(),stop)
            ex.all_positions.assert_called_once();market.active_instruments.assert_not_called();market.quotes.assert_not_called();market.candles.assert_not_called()
            self.assertEqual(health['scan_paused_reason'],'ACCOUNT_POSITION_LIMIT')
    def test_failed_account_read_prevents_scanning(self):
        with tempfile.TemporaryDirectory() as f:
            stop=threading.Event();stop.wait=Mock(side_effect=lambda _:stop.set())
            ex=Mock();ex.all_positions.side_effect=ExchangeError('unavailable')
            market=Mock();health={}
            with patch('scanner.runner.Exchange',return_value=ex),patch('scanner.runner.MarketData',return_value=market):
                scan(ScannerConfig(database=f+'/db',enabled=True),Mock(),health,Mock(),stop)
            market.active_instruments.assert_not_called();self.assertEqual(health['scan_error'],'ExchangeError')

class ConfirmationTests(unittest.TestCase):
    def setUp(self):self.end=int(time.time()//14400)*14400
    def bars(self,rows):
        return [Candle(self.end-(len(rows)-i)*14400,*(D(str(x)) for x in row),D(1)) for i,row in enumerate(rows)]
    def test_consolidation(self):
        bars=self.bars([(100,102,99,101)]*6)
        self.assertEqual(long_confirmation(bars,self.end,D(101)),'CONSOLIDATION')
    def test_falling_knife_cannot_pass(self):
        bars=self.bars([(110-i,111-i,109-i,109.5-i) for i in range(6)])
        self.assertIsNone(long_confirmation(bars,self.end,D(104)))
    def test_new_low_invalidates_consolidation(self):
        bars=self.bars([(100,102,99,101)]*5+[(100,101,98.9,100)])
        self.assertIsNone(long_confirmation(bars,self.end,D(100)))
    def test_higher_low_bullish_breakout(self):
        bars=self.bars([(110,112,104,108),(108,110,102,104),(104,106,98,101),
                        (101,105,99,102),(102,104,100,103),(103,108,101,107)])
        self.assertEqual(long_confirmation(bars,self.end,D(107)),'BULLISH_REVERSAL')
        self.assertIsNone(long_confirmation(bars,self.end,D(99)))
    def test_stale_or_missing_bars_cannot_confirm(self):
        bars=self.bars([(100,102,99,101)]*6)
        self.assertIsNone(long_confirmation(bars[:-1],self.end,D(101)))
        self.assertIsNone(long_confirmation(bars,self.end+14400,D(101)))
    def test_4h_aggregation_requires_all_four_hours(self):
        market=MarketData();start=self.end-8*14400
        hours=[Candle(t,D(100),D(102),D(99),D(101),D(1)) for t in range(start,self.end,3600)]
        market.candles=Mock(return_value=hours)
        self.assertEqual(len(market.four_hour('B-A_USDT',self.end)),8)
        hours.pop();self.assertEqual(len(market.four_hour('B-A_USDT',self.end)),7)
        self.assertEqual(market.candles.call_args.args[-2:],('60',3600))

class MarketMinimumTests(unittest.TestCase):
    def test_mark_price_must_also_meet_min_notional(self):
        q=quantity(INFO,D('.01101'),D(6),1,D('6.5'),D('.01099'))
        self.assertEqual(q,D(546));self.assertGreaterEqual(q*D('.01099'),D(6))
    def test_spread_cannot_exceed_margin_cap(self):
        with self.assertRaises(ValueError):quantity(INFO,D('.35'),D(6),1,D('6.5'),D('.30'))
    def test_http400_message_preserved_and_credentials_redacted(self):
        config=ScannerConfig(key='private-key',secret='private-secret',bot_token='private-token')
        http=Mock();r=http.post.return_value;r.ok=False;r.status_code=400
        r.json.return_value={'message':'Minimum notional invalid private-key private-secret private-token'}
        ex=Exchange(config,Mock(),http)
        with self.assertRaises(ExchangeError) as caught:ex.request('orders/create',{})
        self.assertTrue(caught.exception.rejected)
        self.assertIn('Minimum notional invalid',str(caught.exception))
        self.assertNotIn('private-',str(caught.exception));http.post.assert_called_once()
