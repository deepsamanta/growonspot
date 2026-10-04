import tempfile
import unittest
from decimal import Decimal as D
from unittest.mock import Mock
from scanner.market import DAY, Candle, Quote, MarketDataError
from scanner.strategy import weekly_resistances, evaluate, History, long_confirmation
from scanner.state import ScannerState
from scanner.exchange import quantity, target, Exchange

PAIR='B-TEST_USDT'
INFO=dict(quantity_increment='1',min_quantity='1',min_trade_size='1',min_notional='6',
          max_quantity='10000000',max_market_order_quantity='10000000',price_increment='.001',
          min_price='.001',max_price='100000',dynamic_position_leverage_details={'3':'100000'})
START=1704067200  # Monday UTC

def bars(highs):
    return [Candle(START+(w*7+d)*DAY,D(1),D(str(h)),D('.5'),D(1),D(5))
            for w,h in enumerate(highs) for d in range(7)]

class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.daily=bars([2]*8+[3,4,10,4,3]+[2]*4)
        self.now=START+len(self.daily)*DAY
        self.intraday=[Candle(t,D('.54'),D('.55'),D('.53'),D('.54'),D(1)) for t in range(self.now-6*14400,self.now,14400)]
    def quote(self,price,change=36,low=None):
        return Quote(PAIR,D(str(price)),D(str(change)),self.now,D(str(low)) if low else None)
    def test_weekly_pivot_and_near_resistance_short(self):
        self.assertEqual(weekly_resistances(self.daily,self.now),[D(10)])
        signal=evaluate(PAIR,self.quote(9.9),self.daily,self.now)
        self.assertEqual((signal.side,signal.reference),('SELL',D(10)))
    def test_short_band_and_strict_pump(self):
        for price,change in [(8.99,36),(10.01,36),(10,36),(9.99,35)]:
            self.assertIsNone(evaluate(PAIR,self.quote(price,change),self.daily,self.now))
        self.assertEqual(evaluate(PAIR,self.quote(9,36),self.daily,self.now).reference,D(10))
    def test_unconfirmed_and_incomplete_weeks_cannot_create_pivot(self):
        self.assertEqual(weekly_resistances(bars([2,3,10,3]),START+28*DAY),[])
        sample=bars([2,3,10,3,2]);sample.pop(20)
        self.assertEqual(weekly_resistances(sample,START+35*DAY),[])
    def test_forming_week_cannot_confirm_pivot(self):
        self.assertEqual(weekly_resistances(bars([2,3,10,3,2]),START+34*DAY),[])
    def test_age_and_gold_exclusions(self):
        self.assertIsNone(evaluate(PAIR,self.quote(9.99),self.daily[-99:],self.now))
        self.assertIsNone(evaluate('B-XAU_USDT',self.quote(9.99),self.daily,self.now))
    def test_long_uses_full_history_low_and_todays_low(self):
        self.assertEqual(evaluate(PAIR,self.quote(.55,0),self.daily,self.now,D('.5'),intraday=self.intraday).side,'BUY')
        self.assertIsNone(evaluate(PAIR,self.quote(.551,0),self.daily,self.now,D('.5'),intraday=self.intraday))
        self.assertIsNone(evaluate(PAIR,self.quote(.5,0,.4),self.daily,self.now,D('.5'),intraday=self.intraday))
    def test_old_low_cached_across_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            db=ScannerState(folder+'/state.db');market=Mock()
            older=Candle(START-DAY,D(1),D(2),D('.1'),D(1),D(1))
            market.candles.return_value=[older]
            history=History(market,db)
            self.assertEqual(history.all_time(PAIR,self.now,self.daily),(D('.1'),START-DAY))
            db.conn.close();db=ScannerState(folder+'/state.db')
            history=History(market,db)
            self.assertEqual(history.all_time(PAIR,self.now,self.daily)[0],D('.1'))
            self.assertEqual(market.candles.call_count,1);db.conn.close()
    def test_missing_history_day_is_rejected(self):
        sample=self.daily[:];sample.pop(30)
        with self.assertRaises(MarketDataError):History.complete(sample,self.now)
    def test_incremental_history_cannot_skip_its_first_day(self):
        with self.assertRaises(MarketDataError):History.complete(self.daily[1:],self.now,START)

class QuantityTests(unittest.TestCase):
    def test_long_rounds_to_exchange_minimum(self):
        self.assertEqual(quantity(INFO,D('.31'),D(6),1,D('6.5')),D(20))
    def test_long_rounding_never_exceeds_650(self):
        with self.assertRaises(ValueError):quantity(INFO,D('3.3'),D(6),1,D('6.5'))
    def test_short_never_rounds_above_three_margin(self):
        self.assertEqual(quantity(INFO,D('.31'),D(3),3),D(29))
        with self.assertRaises(ValueError):quantity({**INFO,'min_notional':'10'},D('.31'),D(3),3)
    def test_tp_ticks_preserve_minimum_profit(self):
        self.assertEqual(target(INFO,'SELL',D('1.001'),D('.07')),D('.930'))
        self.assertEqual(target(INFO,'BUY',D('1.001'),D('.10')),D('1.102'))
    def test_order_payload_margin_modes_and_no_sl(self):
        ex=Exchange(Mock(),Mock());ex.request=Mock(return_value=[{'id':'o1'}])
        for side,lev,mode in [('SELL',3,'crossed'),('BUY',1,'isolated')]:
            ex.create(PAIR,side,D(10),lev,mode,D(2))
            order=ex.request.call_args.args[1]['order']
            self.assertEqual((order['order_type'],order['position_margin_type'],order['leverage']),('market_order',mode,lev))
            self.assertFalse(any('stop_loss' in key for key in order))
        ex.take_profit(PAIR,{'pair':PAIR,'id':'p1'},D(2))
        self.assertEqual(set(ex.request.call_args.args[1]),{'id','take_profit'})
    def test_every_mutation_rejects_gold(self):
        ex=Exchange(Mock(),Mock());ex.request=Mock()
        for action,args in [(ex.prepare,('B-XAU_USDT',3,'crossed')),
                            (ex.create,('B-XAU_USDT','BUY',D(1),1,'isolated',D(2))),
                            (ex.take_profit,('B-XAU_USDT',{'pair':'B-XAU_USDT','id':'p1'},D(2))),
                            (ex.exit,('B-XAU_USDT',{'pair':'B-XAU_USDT','id':'p1'}))]:
            with self.assertRaises(ValueError):action(*args)
        ex.request.assert_not_called()
    def test_prepare_does_not_reset_existing_correct_margin_mode(self):
        ex=Exchange(Mock(),Mock());ex.request=Mock();ex.positions=Mock(return_value=[
            {'margin_type':'isolated','leverage':1,'active_pos':0}])
        ex.prepare(PAIR,1,'isolated');ex.request.assert_not_called()
    def test_prepare_rechecks_pair_is_flat(self):
        ex=Exchange(Mock(),Mock());ex.request=Mock();ex.positions=Mock(return_value=[{'active_pos':1}])
        from scanner.exchange import ExchangeError
        with self.assertRaises(ExchangeError):ex.prepare(PAIR,1,'isolated')
        ex.request.assert_not_called()
