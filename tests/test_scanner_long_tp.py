import unittest
from dataclasses import replace
from decimal import Decimal as D
from unittest.mock import Mock,patch

from scanner.exchange import Exchange, ExchangeError
from scanner.long_tp import minimum_partial, replay_long
from scanner.state import ScannerState
import test_scanner_long_averaging as fixtures
from test_scanner_strategy import PAIR, INFO


class LongProfitTests(unittest.TestCase):
    tearDown=fixtures.LongAverageTests.tearDown
    reset=fixtures.LongAverageTests.reset
    row=fixtures.LongAverageTests.row
    poll=fixtures.LongAverageTests.poll
    price=fixtures.LongAverageTests.price
    bars=fixtures.LongAverageTests.bars
    order=fixtures.LongAverageTests.order
    manual=fixtures.LongAverageTests.manual

    def setUp(self):
        fixtures.LongAverageTests.setUp(self)
        self.c=replace(self.c,enabled=False,long_split_tp=True,long_tp=D('.07'))
        self.info={**self.info,'quantity_increment':'.1','min_quantity':'.1','min_trade_size':'.1'}
        self.db.update(self.ident,'OPEN',info=self.info)
        self.market.metadata.return_value=self.info
        self.fills=[self.fill('first','buy','6','1',self.now-7100)]
        self.ex.trade_fills.side_effect=lambda *args:self.fills
        self.native_count=0;self.native('1.06')
        self.ex.take_profit.side_effect=lambda pair,p,tp:self.native(str(tp))
        self.ex.cancel.side_effect=self.cancel
        self.ex.partial_long_limit.side_effect=self.submit
        self.price('1.05');self.reset()

    def fill(self,oid,side,qty,price,stamp):
        return dict(order_id=oid,pair=PAIR,side=side,quantity=qty,price=price,timestamp=stamp*1000)

    def native(self,tp):
        for o in self.orders.values():
            if o.get('stage')=='tpsl_exit':o['status']='cancelled'
        self.native_count+=1;oid='native'+str(self.native_count)
        self.orders[oid]={**self.order(oid,'0','0','untriggered'),'side':'sell','stage':'tpsl_exit',
                          'order_category':'complete_tpsl','order_type':'take_profit_market','stop_price':tp}
        self.p['take_profit_trigger']=tp

    def cancel(self,pair,o):
        o['status']='cancelled';o['cancelled_quantity']=o['remaining_quantity'];o['remaining_quantity']='0'
        if o['stage']=='tpsl_exit':self.p['take_profit_trigger']='0'

    def submit(self,pair,qty,price):
        self.assertEqual(D(self.p['take_profit_trigger']),0)
        self.assertEqual(self.row()['data']['long_tp_orders'][-1]['status'],'submitting')
        oid='limit'+str(self.ex.partial_long_limit.call_count)
        self.orders[oid]={**self.order(oid,str(qty),'0','open',str(qty)),
                          'side':'sell','order_type':'limit_order','price':str(price)}
        return oid

    def rest(self):
        self.poll();self.poll()
        self.ex.partial_long_limit.assert_called_once()
        return self.row()['data']['long_tp_orders'][-1]

    def finish(self,quantity=None,status='filled'):
        a=self.row()['data']['long_tp_orders'][-1];o=self.orders[a['order_id']]
        q=D(a['quantity']) if quantity is None else D(quantity)
        o.update(status=status,remaining_quantity='0',cancelled_quantity=str(D(a['quantity'])-q),avg_price='1.07')
        self.p['active_pos']=str(D(self.p['active_pos'])-q)
        self.fills.append(self.fill(o['id'],'sell',str(q),'1.07',self.now+len(self.fills)))

    def test_existing_six_percent_native_migrates_to_minimum_seven_limit(self):
        self.rest()
        self.ex.partial_long_limit.assert_called_once_with(PAIR,D('5.7'),D('1.07'))
        self.assertEqual(D(self.row()['data']['long_tp']['tp2']),D('1.15'))
        self.assertEqual(self.row()['status'],'OPEN')
        self.ex.average_long.assert_not_called()

    def test_smallest_quantity_is_used_even_when_seventy_five_percent_would_fit(self):
        self.info={**self.info,'min_notional':'1'};self.market.metadata.return_value=self.info
        self.db.update(self.ident,'OPEN',info=self.info)
        self.rest();self.ex.partial_long_limit.assert_called_once_with(PAIR,D('1'),D('1.07'))

    def test_confirmed_partial_attaches_fifteen_percent_and_preserves_average_allowance(self):
        self.db.update(self.ident,'OPEN',averaging_used=True,averaging_source='MANUAL',manual_average_seen=True)
        self.rest();self.finish();self.poll();self.poll()
        self.assertEqual(self.p['active_pos'],'0.3');self.assertEqual(D(self.p['take_profit_trigger']),D('1.15'))
        self.assertTrue(self.row()['data']['averaging_used']);self.assertTrue(self.row()['data']['manual_average_seen'])
        self.assertEqual(self.row()['data']['long_tp']['phase'],'runner')
        self.ex.partial_long_limit.assert_called_once();self.ex.average_long.assert_not_called()

    def test_unused_average_allowance_is_not_consumed_by_partial_profit(self):
        self.rest();self.finish();self.poll()
        self.assertFalse(self.row()['data'].get('averaging_used',False))

    def test_pending_and_runner_survive_restart_without_duplicate_orders(self):
        self.rest();self.db.conn.close();self.db=ScannerState(self.path);self.reset();self.poll()
        self.ex.partial_long_limit.assert_called_once()
        self.finish();self.poll();self.db.conn.close();self.db=ScannerState(self.path);self.reset();self.poll()
        self.ex.partial_long_limit.assert_called_once();self.assertEqual(D(self.p['take_profit_trigger']),D('1.15'))

    def test_partial_fill_keeps_remaining_limit_and_does_not_attach_full_tp(self):
        a=self.rest();o=self.orders[a['order_id']]
        o.update(status='partially_filled',remaining_quantity='3.7',avg_price='1.07')
        self.p['active_pos']='4';self.fills.append(self.fill(o['id'],'sell','2','1.07',self.now+1))
        self.poll();self.poll()
        self.assertEqual(self.row()['status'],'OPEN');self.assertEqual(D(self.p['take_profit_trigger']),0)
        self.assertEqual(self.row()['data']['long_tp']['phase'],'resting')
        self.ex.partial_long_limit.assert_called_once()

    def test_timeout_remains_uncertain_and_never_resubmits(self):
        self.ex.partial_long_limit.side_effect=ExchangeError('timeout')
        self.poll();self.poll();self.reset();self.poll();self.poll()
        self.assertEqual(self.row()['status'],'UNCERTAIN');self.ex.partial_long_limit.assert_called_once()

    def test_rejected_limit_restores_full_seven_tp_and_review_without_resubmission(self):
        self.ex.partial_long_limit.side_effect=ExchangeError('minimum invalid','orders/create',400)
        self.poll();self.poll();self.poll();self.poll()
        self.assertEqual(self.row()['status'],'CONFLICT');self.ex.partial_long_limit.assert_called_once()
        self.assertEqual(D(self.p['take_profit_trigger']),D('1.07'))

    def test_manual_closing_order_is_never_cancelled_or_overlapped(self):
        self.orders['manual']={**self.order('manual','1','0','open','1'),'side':'sell','price':'1.1','order_type':'limit_order'}
        self.poll();self.poll()
        self.ex.cancel.assert_not_called();self.ex.partial_long_limit.assert_not_called()
        self.assertEqual(self.p['take_profit_trigger'],'1.06')

    def test_manual_addition_cancels_old_limit_then_reprices_without_auto_average(self):
        self.rest();self.manual();self.p.update(active_pos='16',avg_price='.625')
        self.fills.append(self.fill('manual','buy','10','.4',self.now+1));self.price('.65')
        self.poll();self.poll()
        self.assertEqual(self.ex.partial_long_limit.call_count,2)
        self.assertEqual(self.ex.partial_long_limit.call_args.args[2],D('.6688'))
        self.assertTrue(self.row()['data']['averaging_used']);self.ex.average_long.assert_not_called()

    def test_own_closing_limit_does_not_block_existing_sixty_percent_average_rule(self):
        self.rest();self.c=replace(self.c,enabled=True);self.reset();self.price('.35')
        self.bars([('.35','.355','.345','.35')]*6)
        self.poll();self.ex.average_long.assert_called_once_with(PAIR,D('17.2'))
        self.assertTrue(self.row()['data']['averaging_used'])

    def test_unattributed_reduction_is_not_treated_as_tp(self):
        self.rest();self.p['active_pos']='5';self.fills.append(self.fill('manual-close','sell','1','1.05',self.now+1))
        self.poll();self.poll()
        self.assertEqual(self.row()['status'],'CONFLICT');self.ex.partial_long_limit.assert_called_once()

    def test_closed_position_cancels_resting_exit_before_releasing_slot(self):
        a=self.rest();self.ex.positions.return_value=[]
        self.poll();self.assertEqual(self.orders[a['order_id']]['status'],'cancelled')
        self.poll();self.assertEqual(self.row()['status'],'PNL_PENDING')

    def test_too_small_uses_full_seven_tp_and_does_not_increase_position(self):
        self.info={**self.info,'quantity_increment':'1','min_quantity':'1','min_trade_size':'1'}
        self.db.update(self.ident,'OPEN',info=self.info);self.market.metadata.return_value=self.info
        self.price('.9');self.poll();self.poll()
        self.ex.partial_long_limit.assert_not_called();self.ex.average_long.assert_not_called();self.ex.create.assert_not_called()
        self.assertEqual(D(self.p['take_profit_trigger']),D('1.07'))
        self.assertTrue(any(c.args[0]=='LONG_SPLIT_TP_UNAVAILABLE' for c in self.emit.call_args_list))

    def test_limit_rests_before_recovery_sized_at_its_guaranteed_minimum_fill_price(self):
        self.price('.9');self.rest()
        self.ex.partial_long_limit.assert_called_once_with(PAIR,D('5.7'),D('1.07'))

    def test_average_fill_cancels_old_exit_and_replans_from_combined_average(self):
        self.rest();self.c=replace(self.c,enabled=True);self.reset();self.price('.35')
        self.bars([('.35','.355','.345','.35')]*6);self.poll()
        a=self.row()['data']['average_order'];qty=D(a['quantity'])
        self.orders['addition']=self.order('addition',str(qty),'.35')
        self.fills.append(self.fill('addition','buy',str(qty),'.35',self.now+1))
        avg=(D(6)+qty*D('.35'))/(D(6)+qty)
        self.p.update(active_pos=str(D(6)+qty),avg_price=str(avg))
        self.poll();self.poll()
        from scanner.exchange import target
        self.assertEqual(self.ex.partial_long_limit.call_count,2)
        self.assertEqual(self.ex.partial_long_limit.call_args.args[2],target(self.info,'BUY',avg,D('.07')))
        self.ex.average_long.assert_called_once();self.assertTrue(self.row()['data']['averaging_used'])

    def test_manual_add_after_profit_uses_remaining_cost_and_does_not_rearm_average(self):
        self.rest();self.finish();self.poll()
        manual=self.manual();manual['total_quantity']='20'
        self.fills.append(self.fill('manual','buy','20','.4',self.now+10))
        avg=(D('.3')+D(20)*D('.4'))/D('20.3')
        self.p.update(active_pos='20.3',avg_price=str(avg));self.price('.42')
        self.poll();self.poll()
        self.assertEqual(self.row()['status'],'OPEN');self.assertEqual(D(self.row()['data']['fill']),avg)
        self.assertTrue(self.row()['data']['averaging_used']);self.ex.average_long.assert_not_called()

    def test_fast_jump_above_final_target_exits_once(self):
        self.price('1.16');self.poll();self.poll();self.poll()
        self.ex.exit.assert_called_once();self.ex.partial_long_limit.assert_not_called()

    def test_cancelled_partial_fill_is_not_replaced_by_another_first_profit(self):
        self.rest();self.finish('2','partially_cancelled');self.poll();self.poll()
        self.assertEqual(self.row()['status'],'CONFLICT');self.ex.partial_long_limit.assert_called_once()
        self.assertEqual(D(self.p['take_profit_trigger']),D('1.15'))

    def test_limit_cancellation_retries_are_bounded_without_replacement(self):
        self.rest();self.ex.cancel.reset_mock();self.ex.cancel.side_effect=ExchangeError('timeout')
        self.p['active_pos']='5'
        for n in range(3):
            with patch('scanner.long_tp.time.time',return_value=self.now+31*n):
                with self.assertRaises(ExchangeError):self.poll()
        with patch('scanner.long_tp.time.time',return_value=self.now+100):self.poll();self.poll()
        self.assertEqual(self.ex.cancel.call_count,3);self.ex.partial_long_limit.assert_called_once()
        self.assertEqual(self.row()['status'],'UNCERTAIN')


class LongProfitMathAndWireTests(unittest.TestCase):
    def test_minimum_rounds_up_notional_and_keeps_a_valid_remainder(self):
        info={**INFO,'quantity_increment':'.1','min_quantity':'.1','min_trade_size':'.1'}
        self.assertEqual(minimum_partial(info,D('6'),D('1.07')),D('5.7'))
        with self.assertRaises(ValueError):minimum_partial(info,D('5.7'),D('1.07'))

    def test_fill_replay_preserves_average_through_reduction_then_addition(self):
        fills=[dict(order_id='first',side='buy',quantity='6',price='1',timestamp=1),
               dict(order_id='tp',side='sell',quantity='5',price='1.07',timestamp=2),
               dict(order_id='manual',side='buy',quantity='3',price='.4',timestamp=3)]
        self.assertEqual(replay_long(fills,{'first','manual'},{'tp'}),(D(4),D('.55')))
        with self.assertRaises(ValueError):replay_long(fills,{'first'},{'tp'})

    def test_limit_payload_is_isolated_sell_without_tp_sl_or_market_tif(self):
        ex=Exchange(Mock(),Mock());ex.request=Mock(return_value=[{'id':'partial'}])
        self.assertEqual(ex.partial_long_limit(PAIR,D('5.7'),D('1.07')),'partial')
        order=ex.request.call_args.args[1]['order']
        self.assertEqual((order['side'],order['order_type'],order['leverage'],order['position_margin_type']),('sell','limit_order',1,'isolated'))
        self.assertEqual(order['total_quantity'],5.7);self.assertEqual(order['price'],1.07)
        self.assertEqual(order['time_in_force'],'good_till_cancel')
        self.assertFalse(any('profit' in k or 'loss' in k for k in order))
        ex.request.reset_mock()
        with self.assertRaises(ValueError):ex.partial_long_limit('B-XAU_USDT',D(1),D(1))
        ex.request.assert_not_called()
