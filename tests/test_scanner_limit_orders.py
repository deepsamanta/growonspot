import tempfile
import time
import unittest
from decimal import Decimal as D
from unittest.mock import Mock,patch
from scanner.config import ScannerConfig
from scanner.engine import Engine
from scanner.exchange import Exchange,ExchangeError,short_limit_price
from scanner.market import DAY,Quote
from scanner.state import ScannerState
from scanner.strategy import Candidate
from test_scanner_strategy import INFO,PAIR


class ShortLimitTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=ScannerState(self.tmp.name+'/db')
        self.now=time.time();self.ex=Mock();self.market=Mock();self.emit=Mock()
        self.ex.wallet_balance.return_value=D(50);self.ex.all_positions.return_value=[];self.ex.positions.return_value=[];self.ex.orders.return_value=[]
        self.ex.transactions.return_value=[];self.ex.price.return_value=D('.95');self.ex.create.return_value='entry1'
        self.ex.recent_entries.return_value=[]
        self.market.quotes.return_value={PAIR:Quote(PAIR,D('.95'),D(36),self.now,D('.7'),D('.95'))}
        self.market.metadata.return_value={**INFO,'max_leverage_short':None,'order_types':['limit_order','market_order']}
        self.market.eligible_metadata.return_value=True
        self.config=ScannerConfig(enabled=True,short_split_tp=False);self.engine=Engine(self.config,self.db,self.ex,self.market,self.emit)
        self.candidate=Candidate(PAIR,'SELL',self.now,D(1),int(self.now-200*DAY),D(36))
    def tearDown(self):self.db.conn.close();self.tmp.cleanup()
    def submit(self):
        self.engine.enter(self.candidate);return self.db.rows()[0]
    def pending(self):
        row=self.submit()
        self.order={'id':'entry1','pair':PAIR,'status':'open','total_quantity':'8','remaining_quantity':'8','cancelled_quantity':0,'avg_price':0}
        self.ex.find_order.return_value=self.order
        return row
    def position(self,quantity='-8',fill='1.02',tp=0):
        p={'id':'p1','pair':PAIR,'active_pos':quantity,'avg_price':fill,'margin_type':'crossed','leverage':3,
           'take_profit_trigger':tp,'stop_loss_trigger':None}
        self.ex.positions.return_value=[p]
        def attach(pair,position,tp):position['take_profit_trigger']=str(tp)
        self.ex.take_profit.side_effect=attach
        return p
    def test_sized_at_resistance_plus_two_percent_with_same_expiry(self):
        row=self.submit();d=row['data']
        self.ex.create.assert_called_once_with(PAIR,'SELL',D(8),3,'crossed',D('.948'),limit_price=D('1.02'))
        self.assertEqual(d['order_type'],'limit_order');self.assertEqual(D(d['estimated_margin']),D('2.72'));self.assertLessEqual(D(d['estimated_margin']),D(3))
        self.assertEqual(D(d['reference']),D(1));self.assertEqual(D(d['limit_price']),D('1.02'))
        self.assertAlmostEqual(d['expires_at']-d['submitted_at'],14400,delta=.1)
        self.assertEqual(len(self.db.occupied()),1)
    def test_pending_limit_survives_market_order_timeout_and_restart(self):
        row=self.pending();self.db.update(row['id'],'SUBMITTED',submitted_at=self.now-3600,expires_at=self.now+3600)
        self.engine=Engine(self.config,self.db,self.ex,self.market,self.emit)
        self.engine.reconcile(self.db.get(row['id']));self.engine.enter(self.candidate)
        self.ex.cancel.assert_not_called();self.ex.create.assert_called_once()
    def test_expiry_cancels_and_holds_slot_until_exchange_confirms(self):
        row=self.pending();self.db.update(row['id'],'SUBMITTED',expires_at=self.now-1)
        self.engine.reconcile(self.db.get(row['id']));self.ex.cancel.assert_called_once_with(PAIR,self.order)
        self.assertEqual(len(self.db.occupied()),1)
        self.order.update(status='cancelled',remaining_quantity=0,cancelled_quantity=8)
        self.engine.reconcile(self.db.get(row['id']))
        self.assertFalse(self.db.occupied());self.assertEqual(self.db.get(row['id'])['data']['reason'],'LIMIT_EXPIRED')
    def test_expired_order_is_cancelled_after_restart(self):
        row=self.pending();self.db.update(row['id'],'SUBMITTED',expires_at=self.now-1)
        path=self.tmp.name+'/db';self.db.conn.close();self.db=ScannerState(path)
        self.engine=Engine(self.config,self.db,self.ex,self.market,self.emit)
        self.engine.reconcile(self.db.get(row['id']));self.ex.cancel.assert_called_once()
    def test_partial_fill_cancels_remainder_then_protects_filled_quantity(self):
        row=self.pending();self.order.update(status='partially_filled',remaining_quantity=5,avg_price='1.02')
        self.position('-3');self.engine.reconcile(row)
        self.ex.cancel.assert_called_once();self.ex.take_profit.assert_not_called()
        self.order.update(status='partially_cancelled',remaining_quantity=0,cancelled_quantity=5)
        self.engine.reconcile(self.db.get(row['id']))
        self.assertEqual(self.db.get(row['id'])['status'],'OPEN')
        self.assertEqual(self.db.get(row['id'])['data']['filled_quantity'],'3')
        self.ex.take_profit.assert_called_once();self.ex.create.assert_called_once()
    def test_fill_while_cancel_is_in_flight_is_still_adopted(self):
        row=self.pending();self.db.update(row['id'],'SUBMITTED',expires_at=self.now-1)
        self.engine.reconcile(self.db.get(row['id']))
        self.order.update(status='filled',remaining_quantity=0,avg_price='1.02')
        self.position();self.engine.reconcile(self.db.get(row['id']))
        self.assertEqual(self.db.get(row['id'])['status'],'OPEN');self.ex.take_profit.assert_called_once()
    def test_cancel_timeouts_are_bounded_and_never_resubmit_entry(self):
        row=self.pending();self.db.update(row['id'],'SUBMITTED',expires_at=self.now-1)
        self.ex.cancel.side_effect=ExchangeError('timeout','orders/cancel')
        for n in range(3):
            with patch('scanner.engine.time.time',return_value=self.now+n*31):
                with self.assertRaises(ExchangeError):self.engine.reconcile(self.db.get(row['id']))
        for n in range(3,6):
            with patch('scanner.engine.time.time',return_value=self.now+n*31):self.engine.reconcile(self.db.get(row['id']))
        self.assertEqual(self.ex.cancel.call_count,3);self.ex.create.assert_called_once()
        self.assertEqual(self.db.get(row['id'])['status'],'UNCERTAIN');self.assertEqual(len(self.db.occupied()),1)
    def test_fill_uses_seven_percent_from_actual_price(self):
        row=self.pending();self.order.update(status='filled',remaining_quantity=0,avg_price='1.03')
        p=self.position(fill='1.03');self.ex.price.return_value=D('.99');self.engine.reconcile(row)
        self.ex.take_profit.assert_called_once_with(PAIR,p,D('.957'))
        self.assertEqual(self.db.get(row['id'])['data']['tp_pct'],'0.07')
    def test_fast_wick_profit_exits_once_at_market(self):
        row=self.pending();self.order.update(status='filled',remaining_quantity=0,avg_price='1.02')
        self.position();self.ex.price.return_value=D('.90')
        self.engine.reconcile(row);self.engine.reconcile(self.db.get(row['id']))
        self.ex.exit.assert_called_once();self.ex.create.assert_called_once()
    def test_late_limit_fill_gets_position_endpoint_grace_period(self):
        row=self.pending();self.db.update(row['id'],'SUBMITTED',submitted_at=self.now-7200)
        self.order.update(status='filled',remaining_quantity=0,avg_price='1.02');self.position('-3')
        self.engine.reconcile(self.db.get(row['id']))
        self.assertEqual(self.db.get(row['id'])['status'],'SUBMITTED');self.ex.take_profit.assert_not_called()
    def test_capacity_change_cancels_pending_limit(self):
        row=self.pending();self.ex.all_positions.return_value=[{'pair':f'B-C{i}_USDT','active_pos':1} for i in range(3)]
        self.engine.reconcile(row);self.ex.cancel.assert_called_once()
        self.assertEqual(self.db.get(row['id'])['data']['cancel_reason'],'ACCOUNT_CAPACITY_CHANGED')
    def test_distance_and_pump_must_still_qualify_at_submission(self):
        for price,change in [('.899',36),('1.001',36),('1',36),('.95',35)]:
            self.market.quotes.return_value={PAIR:Quote(PAIR,D(price),D(change),self.now)}
            self.ex.price.return_value=D(price);self.engine.enter(self.candidate)
        self.ex.create.assert_not_called()
    def test_ambiguous_limit_ack_never_creates_second_order(self):
        self.ex.create.side_effect=ExchangeError('timeout','orders/create')
        row=self.submit();self.engine.enter(self.candidate);self.engine.reconcile(row)
        self.ex.create.assert_called_once();self.assertEqual(self.db.get(row['id'])['status'],'UNCERTAIN')
    def test_pending_limit_reserves_one_of_three_short_slots(self):
        for i in range(3):self.db.reserve(str(i),f'B-S{i}_USDT',{'side':'SELL','order_type':'limit_order'},5)
        self.engine.enter(self.candidate);self.ex.create.assert_not_called();self.market.quotes.assert_not_called()

class LimitWireTests(unittest.TestCase):
    def test_short_limit_has_price_gtc_crossed_and_no_premature_tp_or_sl(self):
        ex=Exchange(Mock(),Mock());ex.request=Mock(return_value=[{'id':'o'}])
        ex.create(PAIR,'SELL',D(9),3,'crossed',D('.93'),limit_price=D(1))
        payload=ex.request.call_args.args[1]['order']
        self.assertEqual((payload['order_type'],payload['price'],payload['time_in_force']),('limit_order',1.0,'good_till_cancel'))
        self.assertEqual((payload['position_margin_type'],payload['leverage']),('crossed',3))
        self.assertNotIn('take_profit_price',payload);self.assertFalse(any('stop_loss' in k for k in payload))
    def test_market_long_keeps_native_tp_and_no_time_in_force(self):
        ex=Exchange(Mock(),Mock());ex.request=Mock(return_value=[{'id':'o'}])
        ex.create(PAIR,'BUY',D(6),1,'isolated',D('1.06'))
        payload=ex.request.call_args.args[1]['order']
        self.assertEqual(payload['order_type'],'market_order');self.assertIsNone(payload['price'])
        self.assertEqual(payload['take_profit_price'],1.06);self.assertNotIn('time_in_force',payload)
    def test_tick_rounding_and_exchange_ltp_bounds(self):
        self.assertEqual(short_limit_price(INFO,D('1.0001'),D('.95')),D('1.021'))
        self.assertEqual(short_limit_price(INFO,D('100'),D('95')),D('102'))
        with self.assertRaisesRegex(ValueError,'PRICE_RANGE'):short_limit_price({**INFO,'max_price':'1.01'},D(1),D('.95'))
        with self.assertRaisesRegex(ValueError,'LTP_RANGE'):short_limit_price({**INFO,'multiplier_up':8},D('1'),D('.9'))
