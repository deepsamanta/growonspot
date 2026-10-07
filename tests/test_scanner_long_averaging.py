import tempfile
import time
import unittest
from dataclasses import replace
from decimal import Decimal as D
from unittest.mock import Mock,patch
from scanner.config import ScannerConfig
from scanner.engine import Engine
from scanner.exchange import Exchange,ExchangeError
from scanner.market import Candle,Quote
from scanner.state import ScannerState
from test_scanner_strategy import INFO,PAIR


class LongAverageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=self.tmp.name+'/db';self.db=ScannerState(self.path)
        self.now=time.time();self.c=ScannerConfig(enabled=True);self.ex=Mock();self.market=Mock();self.emit=Mock()
        self.info={**INFO,'price_increment':'.0001'}
        d={'side':'BUY','order_id':'first','position_id':'position','quantity':'6','filled_quantity':'6',
           'fill':'1','initial_fill':'1','initial_quantity':'6','info':self.info,'leverage':1,'mode':'isolated',
           'order_type':'market_order','submitted_at':self.now-7200,'fill_seen_at':self.now-7100,
           'fill_alerted':True,'tp':'1.06','tp_pct':'.06'}
        self.ident,_=self.db.reserve('first',PAIR,d,5);self.db.update(self.ident,'OPEN')
        self.first=self.order('first','6','1');self.orders={'first':self.first}
        self.p={'id':'position','pair':PAIR,'active_pos':'6','avg_price':'1','margin_type':'isolated','leverage':1,
                'take_profit_trigger':'1.06','stop_loss_trigger':0}
        self.ex.positions.return_value=[self.p];self.ex.all_positions.return_value=[self.p]
        self.ex.wallet_balance.return_value=D(50);self.ex.recent_entries.return_value=[];self.ex.transactions.return_value=[]
        self.ex.find_order.side_effect=lambda pair,side,oid:self.orders.get(oid)
        self.ex.orders.side_effect=lambda pair,side:[o for o in self.orders.values() if o['side']==side.lower()
            and o['status'] not in ('filled','cancelled','partially_cancelled','rejected')]
        self.ex.average_long.return_value='addition'
        self.ex.take_profit.side_effect=lambda pair,p,tp:p.update(take_profit_trigger=str(tp))
        self.market.metadata.return_value=self.info;self.market.eligible_metadata.return_value=True
        self.price('.35');self.bars([('.35','.355','.345','.35')]*6);self.reset()
    def tearDown(self):self.db.conn.close();self.tmp.cleanup()
    def reset(self):self.e=Engine(self.c,self.db,self.ex,self.market,self.emit)
    def row(self):return self.db.get(self.ident)
    def poll(self):self.e.reconcile(self.row());return self.row()
    def price(self,p,mark=None):
        self.ex.price.return_value=D(p)
        self.market.quotes.return_value={PAIR:Quote(PAIR,D(p),D(0),self.now,None,D(mark) if mark else D(p))}
    def bars(self,rows):
        end=int(self.now//14400)*14400
        self.market.four_hour.return_value=[Candle(end-(len(rows)-i)*14400,*(D(v) for v in r),D(1)) for i,r in enumerate(rows)]
    def order(self,oid,qty,price,status='filled',remaining='0',cancelled='0'):
        return {'id':oid,'pair':PAIR,'side':'buy','stage':'default','order_type':'market_order','status':status,
                'total_quantity':qty,'remaining_quantity':remaining,'cancelled_quantity':cancelled,'avg_price':price}
    def place(self):
        self.poll();self.ex.average_long.assert_called_once()
        a=self.row()['data']['average_order']
        self.orders['addition']=self.order('addition',a['quantity'],'0','open',a['quantity'])
        return a
    def fill_addition(self,price='.35',qty=None,status='filled'):
        a=self.row()['data']['average_order'];q=D(a['quantity']) if qty is None else D(qty)
        o=self.orders['addition'];o.update(status=status,remaining_quantity=str(D(a['quantity'])-q),avg_price=price)
        self.p.update(active_pos=str(6+q),avg_price=str((D(6)+q*D(price))/(6+q)))
    def manual(self,pending=False,linked=True):
        o=self.order('manual','10','.4','open' if pending else 'filled','10' if pending else '0')
        if pending:self.orders[o['id']]=o
        else:
            self.ex.recent_entries.return_value=[o]
            if linked:self.ex.transactions.return_value=[{'pair':PAIR,'position_id':'position','stage':'default',
                'parent_type':'Derivatives::Futures::Order','parent_id':'manual','amount':'0','fee_amount':'.01','created_at':self.now*1000}]
        return o
    def test_sixty_percent_is_strict_and_candles_not_checked_early(self):
        for p in ('.8','.401','.40'):
            self.price(p);self.poll();self.ex.average_long.assert_not_called()
        self.market.four_hour.assert_not_called()
        self.price('.3999');self.poll();self.ex.average_long.assert_called_once()
    def test_six_dollar_market_addition_rounds_only_to_approved_cap(self):
        a=self.place();self.ex.average_long.assert_called_once_with(PAIR,D(18))
        self.assertEqual(D(a['estimated_margin']),D('6.30'));self.assertEqual(a['confirmation'],'CONSOLIDATION')
        self.assertEqual(len(self.db.occupied()),1);self.ex.prepare.assert_not_called();self.ex.create.assert_not_called()
    def test_falling_market_without_recovery_does_not_average(self):
        self.bars([(str(D('.6')-D(i)*D('.04')),str(D('.61')-D(i)*D('.04')),
                    str(D('.56')-D(i)*D('.04')),str(D('.57')-D(i)*D('.04'))) for i in range(6)])
        self.poll();self.ex.average_long.assert_not_called()
    def test_bullish_reversal_can_confirm_without_consolidation(self):
        self.price('.39');self.bars([('.38','.40','.34','.36'),('.36','.38','.33','.35'),('.35','.37','.30','.32'),
                                    ('.32','.34','.31','.33'),('.33','.34','.32','.33'),('.33','.40','.325','.39')])
        self.place();self.assertEqual(self.row()['data']['average_order']['confirmation'],'BULLISH_REVERSAL')
    def test_stale_missing_or_forming_candles_block_addition(self):
        valid=list(self.market.four_hour.return_value)
        for bars in (valid[:-1],valid[1:],[] ):
            self.reset();self.market.four_hour.return_value=bars;self.poll();self.ex.average_long.assert_not_called()
        self.reset();last=valid[-1];self.market.four_hour.return_value=valid[:-1]+[replace(last,timestamp=last.timestamp+14400)]
        self.poll();self.ex.average_long.assert_not_called()
    def test_fresh_low_invalidates_recovery(self):
        self.price('.34');self.poll();self.ex.average_long.assert_not_called()
    def test_final_price_or_mark_must_still_be_more_than_sixty_percent_down(self):
        self.ex.price.side_effect=[D('.35'),D('.41')];self.poll();self.ex.average_long.assert_not_called()
        self.reset();self.ex.price.side_effect=None;self.price('.35',mark='.41');self.poll();self.ex.average_long.assert_not_called()
    def test_stale_quote_blocks_addition(self):
        self.market.quotes.return_value={PAIR:Quote(PAIR,D('.35'),D(0),self.now-100)}
        self.poll();self.ex.average_long.assert_not_called()
    def test_recovery_rechecked_after_exchange_reads(self):
        self.ex.price.side_effect=[D('.35'),D('.34')];self.poll();self.ex.average_long.assert_not_called()
    def test_same_pair_addition_allowed_at_full_five_position_capacity(self):
        self.ex.all_positions.return_value=[self.p,{'pair':'B-OTHER_USDT','active_pos':1}]+[
            {'pair':f'B-S{i}_USDT','active_pos':-1} for i in range(3)]
        self.place();self.assertEqual(len(self.db.occupied()),1)
    def test_over_side_cap_blocks_before_recovery_analysis(self):
        self.ex.all_positions.return_value=[self.p]+[{'pair':f'B-L{i}_USDT','active_pos':1} for i in range(2)]
        self.poll();self.market.four_hour.assert_not_called();self.ex.average_long.assert_not_called()
    def test_capacity_change_before_send_blocks_order(self):
        self.ex.all_positions.side_effect=[[self.p],[self.p]+[{'pair':f'B-L{i}_USDT','active_pos':1} for i in range(2)]]
        self.poll();self.ex.average_long.assert_not_called()
    def test_inactive_or_exit_only_instrument_cannot_add(self):
        self.market.eligible_metadata.return_value=False;self.poll();self.ex.average_long.assert_not_called()
    def test_minimum_above_cap_and_combined_tier_reject_before_send(self):
        self.market.metadata.return_value={**self.info,'min_notional':'7'}
        self.poll();self.ex.average_long.assert_not_called()
        self.reset();self.market.metadata.return_value={**self.info,'dynamic_position_leverage_details':{'1':'7'}}
        self.poll();self.ex.average_long.assert_not_called()
    def test_claim_is_durable_before_request_and_restart_cannot_duplicate(self):
        def send(pair,qty):
            self.assertTrue(self.row()['data']['averaging_used'])
            self.assertEqual(self.row()['data']['average_order']['status'],'submitting');return 'addition'
        self.ex.average_long.side_effect=send;self.place()
        self.db.conn.close();self.db=ScannerState(self.path);self.reset();self.poll();self.poll()
        self.ex.average_long.assert_called_once();self.assertFalse(self.db.claim_average(self.ident,{}))
    def test_fill_updates_six_percent_tp_from_combined_actual_average(self):
        self.place();self.fill_addition();self.poll();self.poll()
        self.assertEqual(D(self.p['avg_price']),D('.5125'));self.assertEqual(D(self.p['take_profit_trigger']),D('.5433'))
        self.assertEqual(self.row()['data']['initial_fill'],'1');self.assertEqual(D(self.row()['data']['filled_quantity']),D(24))
        self.ex.average_long.assert_called_once()
        self.assertEqual(len([c for c in self.emit.call_args_list if c.args[0]=='LONG_AVERAGE_FILLED']),1)
    def test_slippage_uses_actual_fill_for_tp(self):
        self.place();self.fill_addition('.36');self.poll()
        self.assertEqual(D(self.p['take_profit_trigger']),D('.5512'))
    def test_partial_fill_cancels_unfilled_remainder_and_updates_actual_tp(self):
        self.place();self.fill_addition(qty='10',status='partially_filled');self.poll()
        self.ex.cancel.assert_called_once_with(PAIR,self.orders['addition'])
        self.orders['addition'].update(status='partially_cancelled',remaining_quantity='0',cancelled_quantity='8')
        self.poll();self.assertEqual(D(self.p['take_profit_trigger']),D('.6294'));self.ex.average_long.assert_called_once()
    def test_timeout_or_rejection_never_retries(self):
        self.ex.average_long.side_effect=ExchangeError('timeout','orders/create')
        self.poll();self.reset();self.poll();self.poll()
        self.ex.average_long.assert_called_once();self.assertEqual(self.row()['status'],'UNCERTAIN')
    def test_definite_rejection_consumes_allowance(self):
        self.ex.average_long.side_effect=ExchangeError('invalid','orders/create',400)
        self.poll();self.poll();self.poll();self.ex.average_long.assert_called_once()
        self.assertTrue(self.row()['data']['averaging_used']);self.assertEqual(self.row()['status'],'OPEN')
    def test_manual_fill_consumes_allowance_and_updates_combined_tp(self):
        self.manual();self.p.update(active_pos='16',avg_price='.625');self.poll()
        self.assertTrue(self.row()['data']['averaging_used']);self.assertEqual(self.p['take_profit_trigger'],'0.6625')
        self.assertEqual(self.row()['data']['averaging_source'],'MANUAL');self.ex.average_long.assert_not_called()
        self.reset();self.poll();self.ex.average_long.assert_not_called()
    def test_pending_manual_addition_consumes_allowance_even_after_cancelled(self):
        self.manual(pending=True);self.poll();self.orders['manual']['status']='cancelled';self.reset();self.poll()
        self.assertTrue(self.row()['data']['averaging_used']);self.ex.average_long.assert_not_called();self.ex.cancel.assert_not_called()
    def test_unverified_manual_addition_blocks_tp_changes_until_ledger_confirms(self):
        self.manual(linked=False);self.p.update(active_pos='16',avg_price='.625');self.poll()
        self.assertEqual(self.row()['status'],'CONFLICT');self.ex.take_profit.assert_not_called()
        self.manual();self.poll();self.assertEqual(self.row()['status'],'OPEN');self.assertEqual(self.p['take_profit_trigger'],'0.6625')
    def test_unexplained_increase_consumes_allowance_without_mutating_tp(self):
        self.p['active_pos']='10';self.poll()
        self.assertTrue(self.row()['data']['averaging_used']);self.assertEqual(self.row()['status'],'CONFLICT')
        self.ex.average_long.assert_not_called();self.ex.take_profit.assert_not_called()
    def test_manual_pending_order_appearing_at_last_check_blocks_bot(self):
        original=self.ex.orders.side_effect;calls=[0]
        def orders(pair,side):
            if side=='BUY':
                calls[0]+=1
                if calls[0]==3:return [self.order('manual','10','0','open','10')]
            return original(pair,side)
        self.ex.orders.side_effect=orders;self.poll()
        self.ex.average_long.assert_not_called();self.assertTrue(self.row()['data']['averaging_used'])
    def test_manual_closing_order_blocks_addition(self):
        self.orders['close']={**self.order('close','2','1.1','open','2'),'side':'sell'}
        self.poll();self.ex.average_long.assert_not_called()
    def test_native_profit_order_does_not_block_addition(self):
        self.orders['tp']={**self.order('tp','0','1.06','untriggered'),'side':'sell','stage':'tpsl_exit'}
        self.place()
    def test_position_changed_after_confirmation_blocks_submission(self):
        original=self.ex.positions.return_value;calls=[0]
        def positions(pair):
            calls[0]+=1
            return [dict(self.p,active_pos='7')] if calls[0]==3 else original
        self.ex.positions.side_effect=positions;self.poll();self.ex.average_long.assert_not_called()
    def test_cancel_market_timeout_is_bounded_and_keeps_slot(self):
        a=self.place();a['submitted_at']=self.now-20;self.db.update(self.ident,'OPEN',average_order=a)
        self.ex.cancel.side_effect=ExchangeError('timeout')
        for n in range(3):
            with patch('scanner.long_averaging.time.time',return_value=self.now+n*31):
                with self.assertRaises(ExchangeError):self.poll()
        with patch('scanner.long_averaging.time.time',return_value=self.now+100):self.poll();self.poll()
        self.assertEqual(self.ex.cancel.call_count,3);self.ex.average_long.assert_called_once()
        self.assertEqual(self.row()['status'],'UNCERTAIN');self.assertEqual(len(self.db.occupied()),1)
    def test_base_closure_cancels_pending_addition_before_releasing_slot(self):
        self.place();self.ex.positions.return_value=[];self.poll()
        self.ex.cancel.assert_called_once();self.assertTrue(self.db.occupied())
        self.orders['addition'].update(status='cancelled',remaining_quantity='0',cancelled_quantity='18')
        self.poll();self.assertEqual(self.row()['status'],'PNL_PENDING');self.ex.average_long.assert_called_once()
    def test_reversal_or_changed_margin_does_not_get_an_addition(self):
        self.p['active_pos']='-6';self.poll();self.ex.average_long.assert_not_called()
        self.p['active_pos']='6';self.p['margin_type']='crossed';self.poll();self.ex.average_long.assert_not_called()
    def test_disabled_bot_reconciles_manual_addition_but_cannot_add(self):
        self.c=replace(self.c,enabled=False);self.reset();self.manual();self.p.update(active_pos='16',avg_price='.625')
        self.poll();self.ex.average_long.assert_not_called();self.assertEqual(self.p['take_profit_trigger'],'0.6625')
    def test_position_lag_recovers_without_duplicate_order(self):
        self.place();self.fill_addition();actual=dict(self.p);self.p.update(active_pos='6',avg_price='1');self.poll()
        self.assertEqual(self.row()['status'],'CONFLICT');self.p.update(actual);self.poll()
        self.assertEqual(self.row()['status'],'OPEN');self.ex.average_long.assert_called_once()


class WireTests(unittest.TestCase):
    def test_average_payload_is_one_times_isolated_without_new_tp_or_sl(self):
        ex=Exchange(Mock(),Mock());ex.request=Mock(return_value=[{'id':'a'}])
        self.assertEqual(ex.average_long(PAIR,D(18)),'a')
        o=ex.request.call_args.args[1]['order']
        self.assertEqual((o['side'],o['order_type'],o['leverage'],o['position_margin_type']),('buy','market_order',1,'isolated'))
        self.assertEqual(o['total_quantity'],18.0);self.assertIsNone(o['price'])
        self.assertFalse(any('profit' in k or 'loss' in k for k in o));self.assertNotIn('time_in_force',o)
    def test_gold_is_rejected_before_request(self):
        ex=Exchange(Mock(),Mock());ex.request=Mock()
        with self.assertRaises(ValueError):ex.average_long('B-XAU_USDT',D(1))
        ex.request.assert_not_called()
