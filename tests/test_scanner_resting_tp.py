import time
import unittest
from decimal import Decimal as D
from unittest.mock import Mock,patch
from scanner.exchange import Exchange,ExchangeError
from scanner.short_tp import ShortTakeProfit
from scanner.resting_tp import limit_checks
from scanner.state import ScannerState
import test_scanner_split_tp as legacy
import test_scanner_averaging as fixtures
from test_scanner_strategy import INFO,PAIR


class RestingTests(unittest.TestCase):
    row=fixtures.AveragingTests.row;poll=fixtures.AveragingTests.poll;order=fixtures.AveragingTests.order
    tx=fixtures.AveragingTests.tx;reset_engine=fixtures.AveragingTests.reset_engine
    tearDown=fixtures.AveragingTests.tearDown
    native=legacy.SplitTests.native;cancel=legacy.SplitTests.cancel;fill=legacy.SplitTests.fill
    price=legacy.SplitTests.price;finish_partial=legacy.SplitTests.finish_partial
    small_position=legacy.SplitTests.small_position
    def setUp(self):
        legacy.SplitTests.setUp(self)
        self.ex.partial_short_limit.side_effect=self.submit_limit
    def submit_partial(self,*args):raise AssertionError('New profit exits must be limits')
    def submit_limit(self,pair,qty,leverage,price):
        s=self.row()['data']['short_tp'];a=self.row()['data']['tp1_orders'][-1]
        self.assertEqual(s['phase'],'resting');self.assertEqual(a['status'],'submitting')
        self.assertEqual(D(a['quantity']),qty);self.assertEqual(D(a['limit_price']),price)
        self.assertEqual(D(self.p['take_profit_trigger']),D(0))
        oid='limit'+str(self.ex.partial_short_limit.call_count)
        self.orders[oid]={**self.order(oid,str(qty),'0','open',str(qty)),
                          'side':'buy','order_type':'limit_order','price':str(price)}
        return oid
    def rest(self):
        self.poll();self.poll()
        self.ex.partial_short_limit.assert_called_once()
        return self.row()['data']['tp1_orders'][-1]
    def tp1(self):
        self.rest();self.price('.4895');self.finish_partial();self.poll()
        self.assertEqual(self.row()['data']['short_tp']['phase'],'runner')
    def test_place_before_target_cancel_native_first_and_survive_restart(self):
        self.poll();self.ex.cancel.assert_called_once();self.ex.partial_short_limit.assert_not_called()
        self.poll();self.ex.partial_short_limit.assert_called_once_with(PAIR,D('12.7'),3,D('.4895'))
        self.assertEqual(self.row()['status'],'OPEN');self.assertEqual(D(self.p['take_profit_trigger']),D(0))
        self.db.conn.close();self.db=ScannerState(self.path);self.reset_engine();self.poll();self.poll()
        self.ex.partial_short_limit.assert_called_once();self.ex.partial_short_exit.assert_not_called()
    def test_resting_limit_has_no_market_timeout_or_four_hour_expiry(self):
        self.rest();a=self.row()['data']['tp1_orders'][-1];a['submitted_at']=self.now-86400
        self.db.update(self.ident,'OPEN',tp1_orders=[a]);self.ex.cancel.reset_mock()
        self.poll();self.poll();self.ex.cancel.assert_not_called();self.ex.partial_short_limit.assert_called_once()
    def test_wick_fills_while_bot_not_polling_then_arms_runner_and_rearms_average(self):
        self.rest();self.price('.4895');self.finish_partial();self.price('.60')
        self.reset_engine();self.poll()
        d=self.row()['data'];self.assertEqual(d['short_tp']['phase'],'runner')
        self.assertEqual(self.p['take_profit_trigger'],'0.4211');self.assertFalse(d['averaging_used'])
        self.assertTrue(d['average_margin_mode']);self.assertEqual(D(d['short_tp']['runner_quantity']),D('4.3'))
        self.poll();self.ex.partial_short_limit.assert_called_once()
    def test_partial_fill_keeps_unfilled_limit_and_does_not_rearm(self):
        self.rest();a=self.row()['data']['tp1_orders'][-1];o=self.orders[a['order_id']]
        o.update(status='partially_filled',remaining_quantity='7.7',avg_price='.4895')
        self.p['active_pos']='-12';self.fills.append(self.fill(o['id'],'buy','5','.4895',self.now))
        self.poll();self.poll()
        self.assertEqual(self.row()['data']['short_tp']['phase'],'resting')
        self.assertNotIn('average_margin_mode',self.row()['data']);self.ex.partial_short_limit.assert_called_once()
        self.assertEqual(D(self.p['take_profit_trigger']),D(0))
    def test_incomplete_cancelled_limit_does_not_close_another_75_percent(self):
        self.rest();self.price('.4895');self.finish_partial('5','partially_cancelled')
        self.e.short_tp.update_partial(self.row(),cancel_reason='POSITION_SIZE_OR_AVERAGE_CHANGED')
        self.poll();self.poll()
        self.ex.partial_short_limit.assert_called_once();self.assertEqual(self.row()['status'],'CONFLICT')
        self.assertEqual(self.row()['data']['reason'],'PARTIAL_TP_LIMIT_INCOMPLETE')
        self.assertEqual(self.p['take_profit_trigger'],'0.4211')
    def test_minimum_size_fallback_is_a_resting_limit(self):
        self.small_position();self.rest()
        self.ex.partial_short_limit.assert_called_once_with(PAIR,D('.4'),3,D('15.593'))
        self.price('15.593');self.finish_partial();self.poll()
        self.assertEqual(D(self.row()['data']['short_tp']['runner_quantity']),D('.1'))
        self.assertEqual(self.p['take_profit_trigger'],'13.413')
    def test_limit_timeout_never_duplicate_across_restart(self):
        self.ex.partial_short_limit.side_effect=ExchangeError('timeout')
        self.poll();self.poll();self.reset_engine();self.poll();self.poll()
        self.ex.partial_short_limit.assert_called_once();self.assertEqual(self.row()['status'],'UNCERTAIN')
    def test_rejected_limit_restores_native_target_and_stops_resubmissions(self):
        self.ex.partial_short_limit.side_effect=ExchangeError('invalid','orders/create',400)
        self.poll();self.poll();self.poll();self.poll()
        self.ex.partial_short_limit.assert_called_once();self.assertEqual(self.row()['status'],'CONFLICT')
        self.assertEqual(self.p['take_profit_trigger'],'0.4211')
    def test_manual_reduction_cancels_limit_and_requires_review(self):
        self.rest();self.p['active_pos']='-10';self.ex.cancel.reset_mock()
        self.poll();self.ex.cancel.assert_called_once();self.poll()
        self.assertEqual(self.row()['status'],'CONFLICT');self.ex.partial_short_limit.assert_called_once()
    def test_position_closure_cancels_limit_before_releasing_slot(self):
        self.rest();self.ex.positions.return_value=[];self.ex.all_positions.return_value=[];self.ex.cancel.reset_mock()
        self.poll();self.ex.cancel.assert_called_once();self.assertTrue(self.db.occupied())
        self.poll();self.assertEqual(self.row()['status'],'PNL_PENDING');self.ex.partial_short_limit.assert_called_once()
    def test_manual_average_cancels_old_limit_before_new_weighted_target(self):
        self.rest();o=self.order('manual','17','.68');self.ex.recent_entries.return_value=[o]
        self.ex.transactions.return_value=[self.tx('manual')]
        self.fills.append(self.fill('manual','sell','17','.68',self.now))
        self.p.update(active_pos='-34',avg_price='.6032');self.price('.65')
        self.poll();self.ex.partial_short_limit.assert_called_once()
        self.poll();self.ex.partial_short_limit.assert_called_with(PAIR,D('25.5'),3,D('.5609'))
        self.assertEqual(self.ex.partial_short_limit.call_count,2);self.assertTrue(self.row()['data']['averaging_used'])
        self.ex.create.assert_not_called()
    def test_other_buy_order_is_preserved_and_blocks_new_profit_limit(self):
        manual={**self.order('manual-buy','2','0','open','2'),'side':'buy'};self.orders[manual['id']]=manual
        self.poll();self.poll();self.ex.partial_short_limit.assert_not_called();self.ex.cancel.assert_not_called()
    def test_new_manual_buy_cancels_only_bot_limit(self):
        self.rest();manual={**self.order('manual-buy','2','0','open','2'),'side':'buy'};self.orders[manual['id']]=manual
        self.poll();self.poll();self.ex.partial_short_limit.assert_called_once()
        self.assertEqual(manual['status'],'open');self.assertEqual(self.row()['data']['tp1_orders'][-1]['status'],'cancelled')
    def test_cancel_timeouts_are_bounded_and_never_replace_unconfirmed_limit(self):
        self.rest();self.p['active_pos']='-10';self.ex.cancel.reset_mock()
        self.ex.cancel.side_effect=ExchangeError('timeout')
        for n in range(3):
            with patch('scanner.resting_tp.time.time',return_value=self.now+n*31):
                with self.assertRaises(ExchangeError):self.poll()
        with patch('scanner.resting_tp.time.time',return_value=self.now+100):self.poll();self.poll()
        self.ex.cancel.assert_called();self.assertEqual(self.ex.cancel.call_count,3)
        self.ex.partial_short_limit.assert_called_once();self.assertEqual(self.row()['status'],'UNCERTAIN')
    def test_filled_limit_then_35_percent_rebound_uses_three_dollar_average(self):
        self.tp1();self.price('.715');self.poll()
        self.ex.create.assert_called_once();a=self.row()['data']['average_order']
        self.assertEqual(a['quantity'],'12.2');self.assertLessEqual(D(a['estimated_margin']),D(3))
    def test_average_fill_replaces_runner_with_fresh_limit_at_weighted_target(self):
        self.tp1();self.price('.715');self.poll();a=self.row()['data']['average_order']
        qty=D(a['quantity']);price=D(a['limit_price'])
        self.orders[a['order_id']]=self.order(a['order_id'],str(qty),str(price))
        prior=-D(self.p['active_pos']);avg=(prior*D(self.p['avg_price'])+qty*price)/(prior+qty)
        self.p.update(active_pos=str(-(prior+qty)),avg_price=str(avg))
        self.fills.append(self.fill(a['order_id'],'sell',str(qty),str(price),self.now+len(self.fills)))
        self.poll();self.assertEqual(self.ex.partial_short_limit.call_count,1)
        self.poll();self.assertEqual(self.ex.partial_short_limit.call_count,2)
        s=self.row()['data']['short_tp'];self.assertEqual(s['cycle'],2);self.assertEqual(D(s['reference']),avg)
        self.assertEqual(s['phase'],'resting');self.assertEqual(D(self.p['take_profit_trigger']),D(0))
    def test_average_order_is_cancelled_before_confirmed_profit_rearms(self):
        self.rest();self.price('.715');self.poll();a=self.row()['data']['average_order']
        self.orders[a['order_id']]=self.order(a['order_id'],a['quantity'],'0','open',a['quantity'])
        self.price('.4895');self.finish_partial();self.poll()
        self.assertEqual(self.orders[a['order_id']]['status'],'cancelled')
        self.assertEqual(self.row()['data']['short_tp']['phase'],'resting')
        self.poll();self.assertEqual(self.row()['data']['short_tp']['phase'],'runner')
        self.assertFalse(self.row()['data']['averaging_used'])
    def test_outside_exchange_price_band_retains_native_and_backs_off(self):
        self.price('.45');self.market.metadata.return_value={**self.info,'multiplier_up':5}
        self.poll();self.poll();self.ex.partial_short_limit.assert_not_called();self.ex.cancel.assert_not_called()
        self.assertEqual(self.p['take_profit_trigger'],'0.4211')
        self.market.metadata.assert_called_once()
    def test_native_cancel_timeout_does_not_create_limit(self):
        self.ex.cancel.side_effect=ExchangeError('timeout')
        with self.assertRaises(ExchangeError):self.poll()
        self.poll();self.ex.partial_short_limit.assert_not_called()
    def test_inflight_legacy_market_exit_is_reconciled_without_new_limit(self):
        old=ShortTakeProfit(self.e);self.e.short_tp=old
        self.ex.partial_short_exit.side_effect=legacy.SplitTests.submit_partial.__get__(self)
        self.poll();self.price('.48');self.poll();self.poll();self.finish_partial()
        self.reset_engine();self.poll()
        self.ex.partial_short_limit.assert_not_called();self.assertEqual(self.row()['data']['short_tp']['phase'],'runner')


class WireTests(unittest.TestCase):
    def test_close_limit_is_sized_gtc_buy_with_no_entry_tp_or_stop(self):
        ex=Exchange(Mock(),Mock());ex.request=Mock(return_value=[{'id':'close'}])
        self.assertEqual(ex.partial_short_limit(PAIR,D('2.2'),3,D('2.750')),'close')
        o=ex.request.call_args.args[1]['order']
        self.assertEqual((o['side'],o['order_type'],o['price'],o['total_quantity']),('buy','limit_order',2.75,2.2))
        self.assertEqual((o['time_in_force'],o['position_margin_type'],o['leverage']),('good_till_cancel','crossed',3))
        self.assertFalse(any('profit' in k or 'loss' in k for k in o))
    def test_limit_validation_checks_price_band_and_tick(self):
        limit_checks(INFO,D(8),D('.930'),D(1))
        limit_checks({**INFO,'multiplier_down':5},D(8),D('.930'),D(1))
        limit_checks({**INFO,'price_increment':'.0001','multiplier_down':8,'multiplier_up':8},D('25.5'),D('.5609'),D('.77'))
        with self.assertRaisesRegex(ValueError,'LTP_RANGE'):limit_checks({**INFO,'multiplier_up':5},D(8),D('1.060'),D(1))
        with self.assertRaisesRegex(ValueError,'PRICE_TICK'):limit_checks(INFO,D(8),D('.9301'),D(1))
