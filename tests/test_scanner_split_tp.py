import tempfile
import time
import unittest
from dataclasses import replace
from decimal import Decimal as D
from unittest.mock import Mock,patch
from scanner.capacity import BalanceCapacity
from scanner.exchange import Exchange,ExchangeError
from scanner.state import ScannerState
from scanner.short_tp import split_plan,partial_quantity,replay_short
import test_scanner_averaging as fixtures
from test_scanner_strategy import INFO,PAIR,bars
from scanner.market import Quote


class SplitTests(unittest.TestCase):
    row=fixtures.AveragingTests.row;poll=fixtures.AveragingTests.poll;order=fixtures.AveragingTests.order
    tx=fixtures.AveragingTests.tx;reset_engine=fixtures.AveragingTests.reset_engine
    tearDown=fixtures.AveragingTests.tearDown
    def setUp(self):
        fixtures.AveragingTests.setUp(self)
        self.c=replace(self.c,short_split_tp=True)
        self.info={**self.info,'quantity_increment':'.1','min_quantity':'.1','min_trade_size':'.1'}
        self.db.update(self.ident,'OPEN',info=self.info)
        self.market.metadata.return_value=self.info
        self.reset_engine();self.price('.55')
        self.fills=[self.fill('first','sell','17','.5264',self.now-7000)]
        self.ex.trade_fills.side_effect=lambda *args:self.fills
        self.native_count=0
        self.native('.4895')
        self.ex.orders.side_effect=lambda pair,side:[o for o in self.orders.values() if o['side']==side.lower()
             and o['status'] not in ('filled','cancelled','partially_cancelled','rejected')]
        self.ex.take_profit.side_effect=lambda pair,p,tp:self.native(str(tp))
        self.ex.cancel.side_effect=self.cancel
        self.ex.partial_short_exit.side_effect=self.submit_partial
    def price(self,value):
        self.ex.price.return_value=D(value)
        self.market.quotes.return_value={PAIR:Quote(PAIR,D(value),D(0),self.now,None,D(value))}
    def native(self,tp):
        for o in self.orders.values():
            if o.get('stage')=='tpsl_exit':o['status']='cancelled'
        self.native_count+=1;oid='native'+str(self.native_count)
        self.orders[oid]={**self.order(oid,'0','0','untriggered'),'side':'buy','stage':'tpsl_exit',
                          'order_category':'complete_tpsl','order_type':'take_profit_market','stop_price':tp}
        self.p['take_profit_trigger']=tp
    def cancel(self,pair,o):
        o['status']='cancelled';o['cancelled_quantity']=o['remaining_quantity'];o['remaining_quantity']='0'
        if o['stage']=='tpsl_exit':self.p['take_profit_trigger']='0'
    def fill(self,oid,side,qty,price,stamp):
        return {'order_id':oid,'pair':PAIR,'side':side,'quantity':qty,'price':price,'timestamp':stamp*1000}
    def submit_partial(self,pair,qty,leverage):
        self.assertEqual(self.row()['data']['short_tp']['phase'],'closing')
        self.assertEqual(self.row()['data']['tp1_orders'][-1]['status'],'submitting')
        self.assertEqual(D(self.row()['data']['short_tp']['tp1_quantity']),qty)
        self.assertGreaterEqual(qty*self.ex.price.return_value,D(self.info['min_notional']))
        self.assertGreater(-D(self.p['active_pos']),qty)
        oid='partial'+str(self.ex.partial_short_exit.call_count)
        self.orders[oid]={**self.order(oid,str(qty),'0','open',str(qty)),'side':'buy','order_type':'market_order'}
        return oid
    def finish_partial(self,quantity=None,status='filled'):
        a=self.row()['data']['tp1_orders'][-1];o=self.orders[a['order_id']]
        qty=D(a['quantity']) if quantity is None else D(quantity)
        o.update(status=status,remaining_quantity='0',cancelled_quantity=str(D(a['quantity'])-qty),avg_price=str(self.ex.price.return_value))
        self.p['active_pos']=str(D(self.p['active_pos'])+qty)
        self.fills.append(self.fill(o['id'],'buy',str(qty),o['avg_price'],self.now+len(self.fills)))
    def tp1(self):
        self.poll();self.price('.48');self.poll();self.poll()
        self.assertEqual(self.ex.partial_short_exit.call_count,1)
        self.finish_partial();self.poll()
        self.assertEqual(self.row()['data']['short_tp']['phase'],'runner')
    def fill_average(self):
        self.price('.715');self.poll()
        a=self.row()['data']['average_order'];qty=D(a['quantity']);price=D(a['limit_price'])
        self.orders[a['order_id']]=self.order(a['order_id'],str(qty),str(price))
        previous=-D(self.p['active_pos']);avg=(previous*D(self.p['avg_price'])+qty*price)/(previous+qty)
        self.p.update(active_pos=str(-(previous+qty)),avg_price=str(avg))
        self.fills.append(self.fill(a['order_id'],'sell',str(qty),str(price),self.now+len(self.fills)))
        self.poll()
    def test_native_runner_target_is_twenty_percent_before_first_exit(self):
        self.poll();self.assertEqual(self.p['take_profit_trigger'],'0.4211')
        self.assertEqual(self.row()['data']['short_tp']['tp1_quantity'],'12.7')
        self.ex.partial_short_exit.assert_not_called()
    def test_cancel_native_confirm_then_exit_seventy_five_percent(self):
        self.poll();self.price('.48');self.poll()
        self.ex.cancel.assert_called_once();self.ex.partial_short_exit.assert_not_called()
        self.poll();self.ex.partial_short_exit.assert_called_once_with(PAIR,D('12.7'),3)
    def test_confirmed_partial_rearms_once_and_retains_twenty_percent(self):
        self.tp1();d=self.row()['data']
        self.assertFalse(d['averaging_used']);self.assertTrue(d['average_margin_mode'])
        self.assertEqual(d['filled_quantity'],'4.3');self.assertEqual(self.p['take_profit_trigger'],'0.4211')
        self.poll();self.poll();self.ex.partial_short_exit.assert_called_once()
        self.assertEqual(len([c for c in self.emit.call_args_list if c.args[0]=='PARTIAL_TP_FILLED']),1)
    def test_rearmed_average_uses_three_dollars_not_initial_quantity(self):
        self.tp1();self.price('.715');self.poll()
        a=self.row()['data']['average_order']
        self.assertEqual(a['quantity'],'12.2');self.assertLessEqual(D(a['estimated_margin']),D(3))
        self.assertEqual(self.ex.create.call_args.args[3:5],(3,'crossed'))
        self.assertEqual(self.ex.create.call_args.kwargs['limit_price'],D('.7344'))
    def test_rearmed_trigger_uses_remaining_average(self):
        self.tp1()
        for growth in ('1.30','1.34','1.3499','1.35'):
            self.price(str(D('.5264')*D(growth)));self.poll();self.ex.create.assert_not_called()
        self.price(str(D('.5264')*D('1.3501')));self.poll();self.ex.create.assert_called_once()
    def test_average_fill_starts_another_split_cycle_at_new_weighted_average(self):
        self.tp1();self.fill_average();s=self.row()['data']['short_tp']
        self.assertEqual(s['phase'],'waiting');self.assertEqual(s['cycle'],2)
        self.assertEqual(s['base_quantity'],'16.5');self.assertEqual(s['reference'],self.p['avg_price'])
        self.assertTrue(self.row()['data']['averaging_used']);self.ex.create.assert_called_once()
    def test_second_partial_archives_average_and_rearms_without_recounting_old_orders(self):
        self.tp1();self.fill_average()
        s=self.row()['data']['short_tp'];self.price(str(D(s['tp1'])-D('.001')))
        self.poll();self.poll();self.finish_partial();self.poll()
        d=self.row()['data'];self.assertEqual(d['short_tp']['phase'],'runner')
        self.assertEqual(len(d['average_history']),1);self.assertFalse(d['averaging_used'])
        self.poll();self.assertFalse(self.row()['data']['averaging_used'])
    def test_manual_average_after_partial_uses_allowance_and_resets_targets(self):
        self.tp1();o=self.order('manual','17','.70');self.ex.recent_entries.return_value=[o]
        self.ex.transactions.return_value=[self.tx('manual')]
        self.fills.append(self.fill('manual','sell','17','.70',self.now+len(self.fills)))
        avg=(D('4.3')*D('.5264')+D(17)*D('.70'))/D('21.3')
        self.p.update(active_pos='-21.3',avg_price=str(avg));self.price('.72');self.poll()
        self.assertTrue(self.row()['data']['averaging_used']);self.ex.create.assert_not_called()
        self.assertEqual(self.row()['data']['short_tp']['phase'],'waiting')
    def test_old_manual_fill_does_not_consume_rearmed_allowance(self):
        o=self.order('manual','17','.68');self.ex.recent_entries.return_value=[o]
        self.ex.transactions.return_value=[self.tx('manual')]
        self.fills.append(self.fill('manual','sell','17','.68',self.now-5000))
        self.p.update(active_pos='-34',avg_price='.6032');self.price('.65');self.poll()
        self.price('.55');self.poll();self.poll();self.finish_partial();self.poll();self.poll()
        d=self.row()['data'];self.assertFalse(d['averaging_used']);self.assertEqual(d['manual_cycle_baseline'],{'manual':'17'})
        self.assertEqual(d['short_tp']['runner_quantity'],'8.5')
    def test_unfilled_or_partial_exit_does_not_rearm(self):
        self.poll();self.price('.48');self.poll();self.poll();self.finish_partial('7','partially_cancelled');self.poll()
        self.assertEqual(self.row()['status'],'CONFLICT')
        self.assertNotEqual(self.row()['data']['short_tp']['phase'],'runner')
        self.ex.create.assert_not_called();self.assertEqual(self.p['take_profit_trigger'],'0.4211')
    def test_timeout_partial_exit_never_resubmits_across_restart(self):
        self.ex.partial_short_exit.side_effect=ExchangeError('timeout')
        self.poll();self.price('.48');self.poll();self.poll();self.reset_engine();self.poll();self.poll()
        self.ex.partial_short_exit.assert_called_once();self.assertEqual(self.row()['status'],'UNCERTAIN')
    def test_native_cancel_timeout_cannot_allow_sized_exit(self):
        self.poll();self.ex.cancel.side_effect=ExchangeError('timeout');self.price('.48')
        with self.assertRaises(ExchangeError):self.poll()
        self.poll();self.ex.partial_short_exit.assert_not_called()
    def test_runner_tp_errors_are_bounded_and_cannot_spawn_entries(self):
        self.ex.take_profit.side_effect=ExchangeError('invalid TP','positions/create_tpsl',400)
        for n in range(3):
            with patch('scanner.short_tp.time.time',return_value=self.now+n*31):
                with self.assertRaises(ExchangeError):self.poll()
        with patch('scanner.short_tp.time.time',return_value=self.now+100):self.poll();self.poll()
        self.assertEqual(self.ex.take_profit.call_count,3);self.assertEqual(self.row()['status'],'UNCERTAIN')
        self.ex.create.assert_not_called();self.ex.partial_short_exit.assert_not_called()
    def test_pending_manual_buy_blocks_partial_without_cancelling_manual_order(self):
        self.poll();o={**self.order('manual-buy','2','0','open','2'),'side':'buy'};self.orders[o['id']]=o
        self.price('.48');self.poll();self.ex.partial_short_exit.assert_not_called();self.ex.cancel.assert_not_called()
    def test_unexpected_manual_reduction_is_not_mistaken_for_tp1(self):
        self.tp1();self.p['active_pos']='-2.3'
        self.fills.append(self.fill('unknown-buy','buy','2','.51',self.now+len(self.fills)));self.poll()
        self.assertEqual(self.row()['status'],'CONFLICT');self.ex.create.assert_not_called()
    def test_pending_average_cancelled_before_partial_exit(self):
        self.poll();self.price('.715');self.poll()
        a=self.row()['data']['average_order'];self.orders[a['order_id']]=self.order(a['order_id'],a['quantity'],'0','open',a['quantity'])
        self.price('.48');self.poll();self.ex.partial_short_exit.assert_not_called()
        self.assertEqual(self.orders[a['order_id']]['status'],'cancelled')
    def test_restart_preserves_partial_completion(self):
        self.tp1();self.db.conn.close();self.db=ScannerState(self.path);self.reset_engine();self.poll()
        self.assertEqual(self.row()['data']['short_tp']['phase'],'runner');self.ex.partial_short_exit.assert_called_once()
    def test_runner_at_twenty_percent_without_native_tp_requests_full_close_once(self):
        self.tp1();self.p['take_profit_trigger']='0';self.price('.40');self.poll();self.poll()
        self.ex.exit.assert_called_once();self.assertEqual(self.row()['status'],'CLOSING')
    def test_size_rounding_keeps_exact_remainder_and_checks_minimum(self):
        plan=split_plan(self.info,D('34'),D('.6032'))
        self.assertEqual((plan['tp1_quantity'],plan['tp1'],plan['tp2']),('25.5','0.5609','0.4825'))
        self.assertEqual(split_plan(INFO,D(8),D(1))['tp1_quantity'],'7')
        with self.assertRaisesRegex(ValueError,'TOO_SMALL'):split_plan(INFO,D(7),D(1))
    def small_position(self,qty='.5',fill='16.767'):
        self.info={**self.info,'price_increment':'.001'}
        self.market.metadata.return_value=self.info
        tp=str((D(fill)*D('.93')).quantize(D('.001'),rounding='ROUND_DOWN'))
        self.db.update(self.ident,'OPEN',quantity=qty,filled_quantity=qty,initial_quantity=qty,
                       fill=fill,initial_fill=fill,info=self.info,tp=tp)
        self.first.update(total_quantity=qty,avg_price=fill)
        self.p.update(active_pos=str(-D(qty)),avg_price=fill)
        self.fills=[self.fill('first','sell',qty,fill,self.now-7000)]
        self.native(tp);self.price(str(D(fill)*D('1.01')))
    def test_small_short_migrates_then_minimum_exit_rearms_with_runner_intact(self):
        self.small_position();self.poll()
        s=self.row()['data']['short_tp']
        self.assertEqual((s['tp1_quantity'],s['tp1'],s['tp2']),('0.4','15.593','13.413'))
        self.assertEqual(self.p['take_profit_trigger'],'13.413')
        self.price('15.5');self.poll();self.poll()
        self.ex.partial_short_exit.assert_called_once_with(PAIR,D('.4'),3)
        self.finish_partial();self.reset_engine();self.poll();self.poll()
        self.assertEqual(self.row()['data']['short_tp']['phase'],'runner')
        self.assertEqual(D(self.row()['data']['short_tp']['runner_quantity']),D('.1'))
        self.assertFalse(self.row()['data']['averaging_used'])
        self.assertEqual(self.p['take_profit_trigger'],'13.413')
        self.ex.partial_short_exit.assert_called_once()
    def test_execution_price_recalculates_minimum_before_persisting_exit(self):
        self.small_position(qty='.7',fill='13');self.poll()
        self.assertEqual(self.row()['data']['short_tp']['tp1_quantity'],'0.5')
        self.price('11.5');self.poll();self.poll()
        self.ex.partial_short_exit.assert_called_once_with(PAIR,D('.6'),3)
        self.assertEqual(self.row()['data']['short_tp']['tp1_quantity'],'0.6')
        self.finish_partial();self.poll()
        self.assertEqual(D(self.row()['data']['short_tp']['runner_quantity']),D('.1'))
    def test_price_gap_without_room_for_runner_does_not_send_invalid_order(self):
        self.small_position(qty='.6',fill='13');self.poll();self.price('11.5');self.poll();self.poll()
        self.ex.partial_short_exit.assert_not_called()
        self.ex.cancel.assert_not_called();self.ex.take_profit.assert_called_once()
        self.assertEqual(D(self.p['take_profit_trigger']),D('10.4'))
        self.assertTrue(any(c.args[0]=='PARTIAL_TP_WAITING' for c in self.emit.call_args_list))
    def test_crossing_final_target_during_cancel_uses_full_exit_once(self):
        self.poll();self.price('.48');self.poll()
        self.ex.price.side_effect=[D('.48'),D('.40'),D('.40')]
        self.poll();self.poll()
        self.ex.partial_short_exit.assert_not_called();self.ex.exit.assert_called_once()
        self.assertEqual(self.row()['status'],'CLOSING')
    def test_smallest_unsplittable_legacy_short_keeps_full_seven_percent(self):
        self.small_position(qty='.4');self.poll()
        self.assertNotIn('short_tp',self.row()['data'])
        self.assertEqual(self.p['take_profit_trigger'],'15.593');self.ex.partial_short_exit.assert_not_called()
    def test_minimum_quantity_and_trade_size_round_up_and_leave_valid_remainder(self):
        info={**self.info,'min_trade_size':'.25','min_notional':'.01'}
        self.assertEqual(partial_quantity(info,D('.8'),D(1)),D('.3'))
        with self.assertRaisesRegex(ValueError,'TOO_SMALL'):partial_quantity(info,D('.5'),D(1))
    def test_split_rejects_invalid_lots_and_exchange_maximum(self):
        with self.assertRaisesRegex(ValueError,'INVALID'):split_plan(self.info,D('.55'),D(20))
        with self.assertRaisesRegex(ValueError,'MAXIMUM'):
            split_plan({**self.info,'max_market_order_quantity':'1'},D(10),D(20))
        with self.assertRaisesRegex(ValueError,'MAXIMUM'):
            split_plan({**self.info,'max_quantity':'.3'},D('.5'),D('16.767'))
    def test_replay_handles_reductions_before_additions_and_rejects_reopening(self):
        fills=[self.fill('first','sell','10','1',1),self.fill('partial','buy','7.5','.93',2),self.fill('add','sell','10','2',3)]
        self.assertEqual(replay_short(fills,{'first','add'},{'partial'}),(D('12.5'),D('1.8')))
        fills[1]['quantity']='10'
        with self.assertRaisesRegex(ValueError,'REOPENED'):replay_short(fills,{'first','add'},{'partial'})


class BalanceSlotTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=ScannerState(self.tmp.name+'/db')
        self.ex=Mock();self.ex.all_positions.return_value=[];self.ex.wallet_balance.return_value=D('52.24483095')
        self.capacity=BalanceCapacity(self.ex,self.db)
    def tearDown(self):self.db.conn.close();self.tmp.cleanup()
    def get(self,balance):
        self.capacity.checked=0;self.ex.wallet_balance.return_value=D(balance);return self.capacity.get()
    def test_fifty_dollar_steps_alternate_short_then_long(self):
        self.capacity.get()
        for growth,limits in [('49.99',(5,2,3)),('50',(6,2,4)),('100',(7,3,4)),('150',(8,3,5)),('200',(9,4,5))]:
            c=self.get(str(D('52.24483095')+D(growth)))
            self.assertEqual((c.max_total,c.max_longs,c.max_shorts),limits)
    def test_baseline_persists_and_withdrawal_only_blocks_new_entries(self):
        self.capacity.get();self.capacity=BalanceCapacity(self.ex,self.db)
        self.assertEqual(self.get('152.24483095').max_total,7)
        self.ex.all_positions.return_value=[{'pair':f'B-L{i}_USDT','active_pos':1} for i in range(3)]
        c=self.get('50');self.assertEqual(c.max_total,5);self.assertFalse(c.within_limits);self.ex.exit.assert_not_called()
    def test_opening_position_does_not_change_total_wallet_baseline(self):
        self.capacity.get();self.ex.all_positions.return_value=[{'pair':'B-C_USDT','active_pos':-1}]
        self.assertEqual(self.get('52.24483095').max_total,5)
        self.assertEqual(self.db.cache('capacity:baseline')['balance'],'52.24483095')
    def test_failed_balance_read_blocks_capacity(self):
        self.ex.wallet_balance.side_effect=ExchangeError('balance unavailable')
        with self.assertRaises(ExchangeError):self.capacity.get()
        self.assertIsNone(self.db.cache('capacity:baseline'))
    def test_two_connections_keep_first_baseline(self):
        other=ScannerState(self.tmp.name+'/db')
        try:
            self.assertEqual(self.db.balance_baseline(D(50)),D(50));self.assertEqual(other.balance_baseline(D(200)),D(50))
        finally:other.conn.close()
    def test_dynamic_database_reservation_can_exceed_five_without_overbooking(self):
        for i in range(6):self.assertIsNotNone(self.db.reserve(str(i),f'B-C{i}_USDT',{},6)[0])
        self.assertEqual(self.db.reserve('last','B-LAST_USDT',{},6)[1],'POSITION_LIMIT')
    def test_wallet_uses_signed_get_and_total_balance_field(self):
        http=Mock();r=http.get.return_value;r.ok=True
        r.json.return_value={'total_wallet_balance':'52.24483095','available_balance_cross':'5.19874774'}
        from scanner.config import ScannerConfig
        ex=Exchange(ScannerConfig(key='test',secret='test'),Mock(),http)
        self.assertEqual(ex.wallet_balance(),D('52.24483095'))
        http.get.assert_called_once();http.post.assert_not_called()
    def test_partial_exit_payload_has_no_new_entry_tp_or_sl(self):
        ex=Exchange(Mock(),Mock());ex.request=Mock(return_value=[{'id':'partial'}])
        self.assertEqual(ex.partial_short_exit(PAIR,D('25.5'),3),'partial')
        o=ex.request.call_args.args[1]['order'];self.assertEqual(o['side'],'buy')
        self.assertEqual(o['total_quantity'],25.5);self.assertIsNone(o['price'])
        self.assertFalse(any('profit' in k or 'loss' in k for k in o))
