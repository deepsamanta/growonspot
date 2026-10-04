import tempfile
import time
import unittest
from decimal import Decimal as D
from unittest.mock import Mock
from scanner.config import ScannerConfig
from scanner.engine import Engine
from scanner.exchange import ExchangeError
from scanner.market import DAY, Quote, Candle
from scanner.state import ScannerState
from scanner.strategy import Candidate
from test_scanner_strategy import INFO, PAIR

class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=ScannerState(self.tmp.name+'/state.db')
        self.ex=Mock();self.market=Mock();self.emit=Mock();self.now=time.time()
        self.ex.price.return_value=D('.31');self.ex.positions.return_value=[];self.ex.orders.return_value=[]
        self.ex.transactions.return_value=[];self.ex.create.return_value='order1'
        self.ex.all_positions.return_value=[]
        end=int(self.now//14400)*14400
        self.market.four_hour.return_value=[Candle(t,D('.31'),D('.311'),D('.30'),D('.31'),D(1)) for t in range(end-6*14400,end,14400)]
        self.market.quotes.return_value={PAIR:Quote(PAIR,D('.31'),D(1),self.now,D('.30'))}
        self.market.metadata.return_value={**INFO,'max_leverage_long':3,'max_leverage_short':3}
        self.market.eligible_metadata.return_value=True
        self.c=ScannerConfig(enabled=True)
        self.e=Engine(self.c,self.db,self.ex,self.market,self.emit)
        self.signal=Candidate(PAIR,'BUY',self.now,D('.30'),int(self.now-200*DAY),D(1))
    def tearDown(self):self.db.conn.close();self.tmp.cleanup()
    def enter(self):self.e.enter(self.signal);return self.db.rows()[0]
    def filled(self,tp=0):
        trade=self.enter();d=trade['data']
        self.ex.find_order.return_value={'id':'order1','pair':PAIR,'status':'filled','total_quantity':d['quantity'],
            'remaining_quantity':0,'cancelled_quantity':0,'avg_price':'.31'}
        position={'id':'position1','pair':PAIR,'active_pos':d['quantity'],'avg_price':'.31',
            'margin_type':'isolated','leverage':1,'take_profit_trigger':tp,'stop_loss_trigger':None}
        self.ex.positions.return_value=[position]
        return trade,position
    def test_market_long_uses_approved_rounding_and_isolated(self):
        row=self.enter();self.assertEqual(row['status'],'SUBMITTED')
        self.ex.prepare.assert_called_once_with(PAIR,1,'isolated')
        self.ex.create.assert_called_once_with(PAIR,'BUY',D(20),1,'isolated',D('.329'))
        self.assertEqual(row['data']['estimated_margin'],'6.20')
    def test_limit_short_crossed_3x_sized_at_resistance(self):
        self.market.quotes.return_value={PAIR:Quote(PAIR,D('.31'),D(36),self.now)}
        self.signal=Candidate(PAIR,'SELL',self.now,D('.312'),int(self.now-200*DAY),D(36))
        self.enter();self.ex.prepare.assert_called_once_with(PAIR,3,'crossed')
        self.ex.create.assert_called_once_with(PAIR,'SELL',D(28),3,'crossed',D('.290'),limit_price=D('.312'))
    def test_null_legacy_leverage_fields_use_dynamic_tiers(self):
        self.market.metadata.return_value={**INFO,'max_leverage_long':None,'max_leverage_short':None}
        self.assertEqual(self.enter()['status'],'SUBMITTED')
        self.ex.create.assert_called_once()
    def test_null_legacy_limits_do_not_bypass_dynamic_tiers(self):
        self.market.metadata.return_value={**INFO,'max_leverage_long':None,'max_leverage_short':None,
            'dynamic_position_leverage_details':{'1':'100000'}}
        self.market.quotes.return_value={PAIR:Quote(PAIR,D('.31'),D(36),self.now)}
        self.signal=Candidate(PAIR,'SELL',self.now,D('.312'),int(self.now-200*DAY),D(36))
        with self.assertRaisesRegex(ValueError,'LEVERAGE_NOT_SUPPORTED'):self.e.enter(self.signal)
        self.ex.create.assert_not_called()
    def test_durable_reservation_precedes_exchange_submission(self):
        def create(*args):
            self.assertEqual(self.db.rows()[0]['status'],'SUBMITTING');return 'order1'
        self.ex.create.side_effect=create;self.enter()
    def test_ambiguous_submission_never_retries_after_restart(self):
        self.ex.create.side_effect=ExchangeError('timeout','orders/create')
        row=self.enter();self.assertEqual(row['status'],'UNCERTAIN')
        self.e=Engine(self.c,self.db,self.ex,self.market,self.emit)
        for _ in range(3):self.e.reconcile(self.db.get(row['id']));self.e.enter(self.signal)
        self.ex.create.assert_called_once();self.assertEqual(len(self.db.occupied()),1)
    def test_definite_rejection_releases_slot(self):
        self.ex.create.side_effect=ExchangeError('bad order','orders/create',400)
        self.assertEqual(self.enter()['status'],'REJECTED');self.assertFalse(self.db.occupied())
    def test_existing_external_position_untouched(self):
        self.ex.positions.return_value=[{'active_pos':'2'}]
        self.e.enter(self.signal);self.ex.prepare.assert_not_called();self.ex.create.assert_not_called()
    def test_five_pending_slots_prevent_sixth(self):
        for i in range(5):self.db.reserve(str(i),f'B-C{i}_USDT',{},5)
        self.e.enter(self.signal);self.ex.create.assert_not_called()
    def test_revalidates_price_after_scan(self):
        self.ex.price.return_value=D('.34');self.e.enter(self.signal);self.ex.create.assert_not_called()
    def test_daily_profit_blocks_entry(self):
        self.db.lock_profit(PAIR,self.now);self.e.enter(self.signal);self.ex.create.assert_not_called()
    def test_two_external_longs_block_before_coin_checks(self):
        self.ex.all_positions.return_value=[{'pair':'B-DOGE_USDT','active_pos':1},{'pair':'B-ADA_USDT','active_pos':1}]
        self.e.enter(self.signal)
        self.market.quotes.assert_not_called();self.market.metadata.assert_not_called();self.market.four_hour.assert_not_called()
        self.ex.create.assert_not_called()
    def test_no_long_without_confirmation(self):
        self.market.four_hour.return_value=[]
        self.e.enter(self.signal);self.ex.create.assert_not_called()
    def test_capacity_rechecked_immediately_before_submit(self):
        full=[{'pair':f'B-C{i}_USDT','active_pos':1} for i in range(2)]
        self.ex.all_positions.side_effect=[[],[],full]
        row=self.enter();self.assertEqual(row['status'],'REJECTED');self.ex.create.assert_not_called()
    def test_rejected_order_has_durable_cooldown(self):
        self.ex.create.side_effect=ExchangeError('Minimum notional invalid','orders/create',400)
        self.enter()
        self.e=Engine(self.c,self.db,self.ex,self.market,self.emit)
        self.signal=Candidate(PAIR,'BUY',self.now+301,D('.30'),int(self.now-200*DAY),D(1))
        self.e.enter(self.signal);self.ex.create.assert_called_once()
    def test_confirmed_fill_has_tp_and_one_alert(self):
        row,p=self.filled('.329')
        self.e.reconcile(row);self.e.reconcile(self.db.get(row['id']))
        self.assertEqual(self.db.get(row['id'])['status'],'OPEN')
        fills=[c for c in self.emit.call_args_list if c.args[0]=='ENTRY_FILLED']
        self.assertEqual(len(fills),1);self.ex.take_profit.assert_not_called()
    def test_tp_recomputed_from_actual_fill(self):
        row,p=self.filled('.329');self.ex.find_order.return_value['avg_price']='.32'
        def attach(pair,position,tp):position['take_profit_trigger']=str(tp)
        self.ex.take_profit.side_effect=attach
        self.e.reconcile(row)
        self.ex.take_profit.assert_called_once_with(PAIR,p,D('.340'))
        self.assertEqual(self.db.get(row['id'])['status'],'OPEN')
    def test_existing_long_migrates_from_ten_to_six_percent(self):
        row,p=self.filled('.341');self.db.update(row['id'],'OPEN',tp_pct='.10')
        def attach(pair,position,tp):position['take_profit_trigger']=str(tp)
        self.ex.take_profit.side_effect=attach
        self.e.reconcile(self.db.get(row['id']))
        self.ex.take_profit.assert_called_once_with(PAIR,p,D('.329'))
        self.assertEqual(self.db.get(row['id'])['data']['tp_pct'],'0.06')
    def test_tp_already_reached_exits_only_once(self):
        row,p=self.filled();self.ex.price.return_value=D('.35')
        self.e.reconcile(row);self.e.reconcile(self.db.get(row['id']))
        self.ex.exit.assert_called_once();self.assertEqual(self.db.get(row['id'])['status'],'CLOSING')
    def test_external_quantity_change_freezes_mutations(self):
        row,p=self.filled('.329');self.e.reconcile(row);p['active_pos']='30'
        self.e.reconcile(self.db.get(row['id']))
        self.assertEqual(self.db.get(row['id'])['status'],'CONFLICT')
        self.ex.exit.assert_not_called();self.ex.take_profit.assert_not_called()
    def test_no_loss_exit_or_stop_loss(self):
        row,p=self.filled('.329');self.ex.price.return_value=D('.01');self.e.reconcile(row)
        self.ex.exit.assert_not_called();self.ex.take_profit.assert_not_called()
    def test_ledger_confirms_profit_and_locks_today(self):
        row,p=self.filled('.329');self.e.reconcile(row);self.ex.positions.return_value=[]
        common={'pair':PAIR,'position_id':'position1','parent_type':'Derivatives::Futures::Order','created_at':int(self.now*1000)}
        self.ex.transactions.return_value=[{**common,'parent_id':'order1','stage':'default','amount':0,'fee_amount':'.01'},
            {**common,'parent_id':'tp1','stage':'tpsl_exit','amount':'.6','fee_amount':'.01'}]
        self.e.reconcile(self.db.get(row['id']))
        self.assertEqual(self.db.get(row['id'])['status'],'CLOSED')
        self.assertEqual(self.db.get(row['id'])['realized_pnl'],'0.58')
        self.assertEqual(self.db.reserve('other',PAIR,{},5)[1],'PROFIT_LOCKED_TODAY')
    def test_missing_ledger_blocks_coin_until_known(self):
        row,p=self.filled('.329');self.e.reconcile(row);self.ex.positions.return_value=[]
        self.e.reconcile(self.db.get(row['id']))
        self.assertEqual(self.db.get(row['id'])['status'],'PNL_PENDING')
        self.assertEqual(self.db.reserve('other',PAIR,{},5)[1],'DUPLICATE_SIGNAL_OR_ACTIVE_COIN')
