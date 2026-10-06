import tempfile
import time
import unittest
from dataclasses import replace
from decimal import Decimal as D
from unittest.mock import Mock,patch
from scanner.config import ScannerConfig
from scanner.engine import Engine
from scanner.exchange import Exchange,ExchangeError
from scanner.averaging import validate_quantity
from scanner.market import Quote
from scanner.state import ScannerState
from test_scanner_strategy import INFO,PAIR,bars


class AveragingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=self.tmp.name+'/db';self.db=ScannerState(self.path)
        self.now=time.time();self.c=ScannerConfig(enabled=True,short_split_tp=False);self.ex=Mock();self.market=Mock();self.emit=Mock()
        self.info={**INFO,'price_increment':'.0001'}
        data={'side':'SELL','order_id':'first','position_id':'position','quantity':'17','filled_quantity':'17',
              'fill':'.5264','initial_fill':'.5264','initial_quantity':'17','info':self.info,'leverage':3,
              'mode':'crossed','order_type':'limit_order','submitted_at':self.now-7200,'fill_seen_at':self.now-7100,
              'fill_alerted':True,'tp':'.4895','tp_pct':'.07'}
        self.ident,_=self.db.reserve('first',PAIR,data,5)
        self.db.update(self.ident,'OPEN')
        self.first=self.order('first','17','.5264')
        self.p={'id':'position','pair':PAIR,'active_pos':'-17','avg_price':'.5264',
                'margin_type':'crossed','leverage':3,'take_profit_trigger':'.4895','stop_loss_trigger':0}
        self.orders={'first':self.first}
        self.ex.find_order.side_effect=lambda pair,side,oid:self.orders.get(oid)
        self.ex.positions.return_value=[self.p];self.ex.all_positions.return_value=[self.p]
        self.ex.wallet_balance.return_value=D(50);self.ex.orders.return_value=[];self.ex.recent_entries.return_value=[];self.ex.transactions.return_value=[]
        self.ex.create.return_value='addition';self.ex.price.return_value=D('.70')
        self.market.quotes.return_value={PAIR:Quote(PAIR,D('.70'),D(0),self.now,D('.6'),D('.70'))}
        self.market.metadata.return_value=self.info;self.market.eligible_metadata.return_value=True
        self.market.candles.return_value=[]
        self.ex.take_profit.side_effect=lambda pair,p,tp:p.update(take_profit_trigger=str(tp))
        self.reset_engine()
    def reset_engine(self):
        self.e=Engine(self.c,self.db,self.ex,self.market,self.emit)
        self.e.averager.history.daily=Mock(return_value=bars([.60,.65,.72,.65,.60]))
    def tearDown(self):self.db.conn.close();self.tmp.cleanup()
    def order(self,oid,qty,price,status='filled',remaining='0',cancelled='0'):
        return {'id':oid,'pair':PAIR,'side':'sell','stage':'default','order_type':'limit_order',
                'status':status,'total_quantity':qty,'remaining_quantity':remaining,'cancelled_quantity':cancelled,
                'avg_price':price,'created_at':int((self.now-100)*1000)}
    def row(self):return self.db.get(self.ident)
    def poll(self):self.e.reconcile(self.row());return self.row()
    def place(self):
        self.poll();self.orders['addition']=self.order('addition','17','0','open','17')
        self.assertEqual(self.ex.create.call_count,1)
    def manual(self,qty='17',price='.68',pending=False,linked=True):
        o=self.order('manual',qty,price,'open' if pending else 'filled',qty if pending else '0')
        if pending:self.ex.orders.return_value=[o]
        else:
            self.ex.recent_entries.return_value=[o]
            if linked:self.ex.transactions.return_value=[self.tx('manual')]
        return o
    def tx(self,oid,amount='0',stage='default'):
        return {'pair':PAIR,'position_id':'position','parent_type':'Derivatives::Futures::Order',
                'created_at':int(self.now*1000),'parent_id':oid,'amount':amount,'fee_amount':'.01','stage':stage}
    def test_exact_same_quantity_and_leverage_at_two_percent_above_resistance(self):
        row=self.poll();a=row['data']['average_order']
        self.ex.create.assert_called_once_with(PAIR,'SELL',D(17),3,'crossed',D('.6829'),limit_price=D('.7344'))
        self.assertGreater(D(a['estimated_margin']),D(3));self.assertAlmostEqual(a['expires_at']-a['submitted_at'],14400,delta=.1)
        self.assertEqual(len(self.db.occupied()),1);self.ex.prepare.assert_not_called()
    def test_strict_thirty_percent_from_original_actual_fill(self):
        self.ex.price.return_value=D('.5264')*D('1.30');self.poll();self.ex.create.assert_not_called()
        self.assertFalse(self.e.averager.history.daily.called)
    def test_nearest_resistance_and_ten_percent_band(self):
        self.e.averager.history.daily.return_value=bars([.6,.65,.80,.65,.6]);self.poll()
        self.ex.create.assert_not_called()
    def test_no_confirmed_weekly_resistance_waits(self):
        self.e.averager.history.daily.return_value=[];self.poll();self.ex.create.assert_not_called()
    def test_partial_first_fill_uses_only_executed_quantity(self):
        self.first.update(status='partially_cancelled',total_quantity='20',cancelled_quantity='3')
        self.db.update(self.ident,'OPEN',quantity='20');self.poll()
        self.assertEqual(self.ex.create.call_args.args[2],D(17))
    def test_capacity_full_but_within_three_short_two_long_allows_same_pair_addition(self):
        self.ex.all_positions.return_value=[self.p]+[{'pair':f'B-L{i}_USDT','active_pos':1} for i in range(2)]+[
            {'pair':f'B-S{i}_USDT','active_pos':-1} for i in range(2)]
        self.poll();self.ex.create.assert_called_once()
    def test_over_capacity_blocks_before_resistance_analysis(self):
        self.ex.all_positions.return_value=[self.p]+[{'pair':f'B-L{i}_USDT','active_pos':1} for i in range(3)]
        self.poll();self.ex.create.assert_not_called();self.e.averager.history.daily.assert_not_called()
    def test_disabled_never_adds_but_updates_manual_tp(self):
        self.c=replace(self.c,enabled=False);self.reset_engine();self.manual()
        self.p.update(active_pos='-34',avg_price='.6032');self.poll()
        self.ex.create.assert_not_called();self.assertEqual(self.p['take_profit_trigger'],'0.5609')
    def test_atomic_claim_precedes_network_and_restart_does_not_duplicate(self):
        def create(*args,**kwargs):
            self.assertTrue(self.row()['data']['averaging_used'])
            self.assertEqual(self.row()['data']['average_order']['status'],'submitting');return 'addition'
        self.ex.create.side_effect=create;self.place()
        self.db.conn.close();self.db=ScannerState(self.path);self.reset_engine()
        self.poll();self.poll();self.ex.create.assert_called_once()
        self.assertFalse(self.db.claim_average(self.ident,{}))
    def test_timeout_never_retries(self):
        self.ex.create.side_effect=ExchangeError('timeout','orders/create')
        self.poll();self.reset_engine();self.poll();self.poll()
        self.ex.create.assert_called_once();self.assertEqual(self.row()['status'],'UNCERTAIN')
    def test_rejected_addition_consumes_allowance(self):
        self.ex.create.side_effect=ExchangeError('invalid','orders/create',400)
        self.poll();self.poll();self.ex.create.assert_called_once()
        self.assertEqual(self.row()['status'],'OPEN')
    def test_full_fill_sets_weighted_average_tp(self):
        self.place();self.orders['addition'].update(status='filled',remaining_quantity='0',avg_price='.7344')
        self.p.update(active_pos='-34',avg_price='.6304');self.poll()
        self.assertEqual(self.p['take_profit_trigger'],'0.5862')
        self.assertEqual(self.row()['data']['initial_fill'],'0.5264')
        self.assertEqual(self.row()['data']['filled_quantity'],'34');self.ex.create.assert_called_once()
    def test_partial_addition_cancels_remainder_and_uses_weighted_average(self):
        self.place();self.orders['addition'].update(status='partially_filled',remaining_quantity='10',avg_price='.7344')
        avg=(D(17)*D('.5264')+D(7)*D('.7344'))/24
        self.p.update(active_pos='-24',avg_price=str(avg));self.poll()
        self.ex.cancel.assert_called_once_with(PAIR,self.orders['addition'])
        self.assertEqual(self.row()['data']['filled_quantity'],'24')
        self.assertEqual(self.p['take_profit_trigger'],'0.5459')
    def test_expiry_cancel_is_persistent_and_no_replacement(self):
        self.place();a=self.row()['data']['average_order'];a['expires_at']=self.now-1
        self.db.update(self.ident,'OPEN',average_order=a);self.poll();self.ex.cancel.assert_called_once()
        self.orders['addition'].update(status='cancelled',remaining_quantity='0',cancelled_quantity='17')
        self.reset_engine();self.poll();self.ex.create.assert_called_once();self.assertEqual(self.row()['status'],'OPEN')
    def test_manual_fill_recovers_existing_conflict_and_recalculates_tp(self):
        self.db.update(self.ident,'CONFLICT',reason='POSITION_QUANTITY_OR_ID_CHANGED')
        self.manual();self.p.update(active_pos='-34',avg_price='.6032');row=self.poll()
        self.assertEqual(row['status'],'OPEN');self.assertTrue(row['data']['averaging_used'])
        self.assertEqual(row['data']['averaging_source'],'MANUAL');self.assertIn('manual',row['data']['manual_entries'])
        self.assertEqual(self.p['take_profit_trigger'],'0.5609');self.ex.create.assert_not_called()
        self.reset_engine();self.poll();self.ex.create.assert_not_called()
    def test_pending_manual_order_blocks_even_before_fill_and_after_cancellation(self):
        self.manual(pending=True);self.poll();self.ex.wallet_balance.return_value=D(50);self.ex.orders.return_value=[];self.reset_engine();self.poll()
        self.ex.create.assert_not_called();self.ex.cancel.assert_not_called()
        self.assertTrue(self.row()['data']['averaging_used'])
    def test_manual_pending_cancels_only_own_pending_average(self):
        self.place();manual=self.manual(pending=True);self.poll()
        self.ex.cancel.assert_called_once_with(PAIR,self.orders['addition']);self.ex.create.assert_called_once()
        self.assertNotEqual(self.ex.cancel.call_args.args[1]['id'],manual['id'])
    def test_manual_fill_while_bot_pending_cancels_and_updates_combined_tp(self):
        self.place();self.manual();self.p.update(active_pos='-34',avg_price='.6032');self.poll()
        self.ex.cancel.assert_called_once_with(PAIR,self.orders['addition'])
        self.assertEqual(self.p['take_profit_trigger'],'0.5609');self.ex.create.assert_called_once()
    def test_unattributed_increase_blocks_averaging_and_waits_for_ledger(self):
        self.manual(linked=False);self.p.update(active_pos='-34',avg_price='.6032');self.poll()
        self.ex.create.assert_not_called();self.ex.take_profit.assert_not_called()
        self.assertEqual(self.row()['status'],'CONFLICT');self.assertTrue(self.row()['data']['averaging_used'])
        self.ex.transactions.return_value=[self.tx('manual')];self.poll();self.assertEqual(self.row()['status'],'OPEN')
    def test_unexplained_quantity_increase_persistently_consumes_allowance(self):
        self.p['active_pos']='-34';self.poll();self.ex.create.assert_not_called()
        self.assertTrue(self.row()['data']['averaging_used']);self.assertEqual(self.row()['status'],'CONFLICT')
    def test_wrong_position_or_direction_is_never_adopted(self):
        self.manual();self.p.update(active_pos='34',avg_price='.6032',id='another');self.poll()
        self.ex.take_profit.assert_not_called();self.ex.create.assert_not_called();self.assertEqual(self.row()['status'],'CONFLICT')
    def test_margin_change_cancels_own_order_and_prevents_tp_update(self):
        self.place();self.p['leverage']=5;self.poll()
        self.ex.cancel.assert_called_once();self.assertEqual(self.row()['status'],'CONFLICT')
        self.ex.take_profit.assert_not_called()
    def test_flat_position_cancels_average_before_releasing_reservation(self):
        self.place();self.ex.positions.return_value=[];self.poll()
        self.ex.cancel.assert_called_once();self.assertEqual(len(self.db.occupied()),1)
        self.orders['addition'].update(status='cancelled',remaining_quantity='0',cancelled_quantity='17')
        self.poll();self.assertEqual(self.row()['status'],'PNL_PENDING');self.ex.create.assert_called_once()
    def test_profit_exit_waits_for_average_cancel_confirmation(self):
        self.place();self.p['take_profit_trigger']='0';self.ex.price.return_value=D('.40');self.poll()
        self.ex.cancel.assert_called_once();self.ex.exit.assert_not_called()
        self.orders['addition'].update(status='cancelled',remaining_quantity='0',cancelled_quantity='17')
        self.poll();self.ex.exit.assert_called_once()
    def test_cancel_race_that_reopens_only_addition_is_not_adopted(self):
        self.place();self.ex.positions.return_value=[];self.poll()
        self.orders['addition'].update(status='filled',remaining_quantity='0',avg_price='.7344')
        self.ex.positions.return_value=[self.p];self.p['avg_price']='.7344';self.poll()
        self.assertEqual(self.row()['status'],'CONFLICT');self.ex.take_profit.assert_not_called();self.ex.create.assert_called_once()
    def test_cancel_retries_are_bounded_across_restart(self):
        self.place();self.manual(pending=True);self.ex.cancel.side_effect=ExchangeError('timeout')
        for n in range(3):
            with patch('scanner.averaging.time.time',return_value=self.now+31*n):
                with self.assertRaises(ExchangeError):self.poll()
        self.reset_engine()
        with patch('scanner.averaging.time.time',return_value=self.now+124):self.poll();self.poll()
        self.assertEqual(self.ex.cancel.call_count,3);self.assertTrue(self.row()['data']['average_order']['cancel_unconfirmed'])
    def test_entry_ledger_rows_are_not_misclassified_as_exits(self):
        self.manual();self.p.update(active_pos='-34',avg_price='.6032');self.poll()
        self.ex.positions.return_value=[];self.ex.transactions.return_value=[self.tx('first'),self.tx('manual')]
        self.poll();self.assertEqual(self.row()['status'],'PNL_PENDING')
        self.ex.transactions.return_value.append(self.tx('tp','.8','tpsl_exit'));self.poll()
        self.assertEqual(self.row()['status'],'CLOSED');self.assertEqual(self.row()['realized_pnl'],'0.77')
    def test_once_per_position_resets_for_a_new_episode(self):
        self.place();self.db.record_flat(self.ident,time.time(),D(-1))
        ident,reason=self.db.reserve('second',PAIR,{'side':'SELL'},5)
        self.assertIsNone(reason);self.assertNotIn('averaging_used',self.db.get(ident)['data'])
    def test_final_quantity_recheck_blocks_manual_race(self):
        calls=0
        def positions(pair):
            nonlocal calls
            calls+=1
            if calls==3:self.p['active_pos']='-34'
            return [self.p]
        self.ex.positions.side_effect=positions;self.poll();self.ex.create.assert_not_called()


class AverageAdapterTests(unittest.TestCase):
    def test_exact_quantity_and_combined_tier_validation(self):
        with self.assertRaisesRegex(ValueError,'COMBINED_LEVERAGE'):
            validate_quantity({**INFO,'dynamic_position_leverage_details':{'3':'10','1':'100'}},D(17),D('.7344'),D(17),3,D('.7'))
        with self.assertRaisesRegex(ValueError,'MINIMUM'):
            validate_quantity(INFO,D('17.5'),D('.7344'),D(17),3,D('.7'))
    def test_recent_entries_filters_pair_side_time_and_aggregates_partial_fills(self):
        ex=Exchange(Mock(),Mock());common={'pair':PAIR,'side':'sell','timestamp':2000,'quantity':'2','price':'1'}
        ex.pages=Mock(return_value=iter([[{**common,'order_id':'yes'},{**common,'order_id':'old','timestamp':0},
            {**common,'order_id':'wrong','pair':'B-XAU_USDT'},{**common,'order_id':'buy','side':'buy'}],
            [{**common,'order_id':'yes','timestamp':3000,'quantity':'3','price':'2'}]]))
        rows=ex.recent_entries(PAIR,'SELL',1)
        self.assertEqual([o['id'] for o in rows],['yes'])
        self.assertEqual((rows[0]['total_quantity'],rows[0]['avg_price']),('5','1.6'))
        endpoint,body=ex.pages.call_args.args
        self.assertEqual(endpoint,'trades');self.assertEqual(body['pair'],PAIR)
        self.assertEqual(body['from_date'],'1970-01-01');self.assertIn('to_date',body)
