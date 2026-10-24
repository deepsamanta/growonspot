import time
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal as D
from unittest.mock import Mock,patch
from scanner.ath_short import RULE,day_start,prior_ath,surge_bar,detect
from scanner.market import Candle,Quote,MarketData,MarketDataError,DAY
from scanner.strategy import Candidate,History
from scanner.state import ScannerState
from scanner.engine import Engine
from scanner.exchange import ExchangeError
import test_scanner_limit_orders as fixtures
from test_scanner_strategy import PAIR


def bar(t,high='1.35',open='1'):
    return Candle(t,D(open),D(high),D('.9'),D('1.1'),D(1))


class AthStrategyTests(unittest.TestCase):
    def test_wick_counts_in_current_and_previous_but_not_older_candle(self):
        now=1800000000;start=now//900*900
        self.assertIsNotNone(surge_bar([bar(start)],now))
        self.assertIsNotNone(surge_bar([bar(start-900)],now))
        self.assertIsNone(surge_bar([bar(start-1800)],now))
        self.assertIsNone(surge_bar([bar(start,high='1.349')],now))

    def test_fifteen_minute_build_requires_all_elapsed_minutes(self):
        start=1800000000//900*900;now=start+150
        market=MarketData(clock=lambda:now)
        market.candles=Mock(return_value=[bar(t) for t in range(start-900,start+180,60)])
        result=market.fifteen_minute(PAIR,now)
        self.assertEqual([r.timestamp for r in result],[start-900,start])
        self.assertEqual(result[-1].volume,D(3))
        market.candles.return_value=[bar(t) for t in range(start-900,start+180,60) if t!=start+60]
        self.assertEqual([r.timestamp for r in market.fifteen_minute(PAIR,now)],[start-900])

    def test_open_candles_are_opt_in(self):
        now=1800000010;start=now//60*60
        market=MarketData(clock=lambda:now)
        market.get=Mock(return_value={'s':'ok','data':[{'time':start*1000,'open':1,'high':1.4,'low':1,'close':1.2,'volume':1}]})
        self.assertEqual(market.candles(PAIR,start,start+60,'1',60),[])
        self.assertEqual(len(market.candles(PAIR,start,start+60,'1',60,include_open=True)),1)

    def test_ath_full_history_and_ist_boundary_excludes_today(self):
        now=1800000000;cutoff=day_start(now);end=cutoff//DAY*DAY
        with tempfile.TemporaryDirectory() as tmp:
            db=ScannerState(tmp+'/db');market=Mock();history=History(market,db)
            daily=[bar(t,high='2') for t in range(end-100*DAY,end,DAY)]
            # This UTC candle overlaps today IST and must NOT enter the daily ATH.
            daily.append(bar(end,high='100'))
            old=[bar(end-101*DAY,high='3')]
            tail=[bar(t,high='4') for t in range(end,cutoff,300)]
            market.candles.side_effect=[old,tail]
            self.assertEqual(prior_ath(history,PAIR,now,daily),(D(4),end-101*DAY,cutoff))
            self.assertEqual(prior_ath(history,PAIR,now+1,daily)[0],D(4))
            self.assertEqual(market.candles.call_count,2)
            self.assertEqual(market.candles.call_args.args,(PAIR,end,cutoff,'5',300))
            db.conn.close()

    def test_missing_boundary_bars_fail_closed(self):
        now=1800000000;end=day_start(now)//DAY*DAY
        with tempfile.TemporaryDirectory() as tmp:
            db=ScannerState(tmp+'/db');market=Mock();history=History(market,db)
            daily=[bar(t) for t in range(end-100*DAY,end,DAY)]
            market.candles.side_effect=[[],[]]
            with self.assertRaises(MarketDataError):prior_ath(history,PAIR,now,daily)
            db.conn.close()

    def test_detection_needs_current_price_above_ath_not_daily_gain(self):
        now=time.time();market=Mock();history=Mock()
        market.fifteen_minute.return_value=[bar(int(now//900)*900)]
        daily=[bar(int(now//DAY)*DAY-i*DAY) for i in range(110,0,-1)]
        quote=Quote(PAIR,D('1.1'),D('-5'),now,D('.9'),D('1.1'),D('1.4'))
        with patch('scanner.ath_short.prior_ath',return_value=(D(1),now-110*DAY,day_start(now))):
            signal=detect(PAIR,quote,market,history,now,daily)
            self.assertEqual(signal.confirmation,RULE)
            self.assertIsNone(detect(PAIR,replace(quote,price=D('.99')),market,history,now,daily))
            self.assertIsNone(detect(PAIR,quote,market,history,now,daily[-99:]))
            self.assertIsNone(detect(PAIR,replace(quote,timestamp=now-61),market,history,now,daily))


class AthExecutionTests(unittest.TestCase):
    def setUp(self):
        fixtures.ShortLimitTests.setUp(self)
        self.config=replace(self.config,ath_short_enabled=True)
        self.engine=Engine(self.config,self.db,self.ex,self.market,self.emit)
        self.market.quotes.return_value={PAIR:Quote(PAIR,D('1.1'),D('-5'),self.now,D('.9'),D('1.1'),D('1.4'))}
        self.ex.price.return_value=D('1.1')
        self.stamp=int(self.now//900)*900
        self.market.fifteen_minute.return_value=[bar(self.stamp)]
        self.candidate=Candidate(PAIR,'SELL',self.now,D(1),int(self.now-200*DAY),D(-5),RULE,self.stamp,day_start(self.now))
    def tearDown(self):fixtures.ShortLimitTests.tearDown(self)

    def test_market_short_with_existing_budget_and_durable_candle_key(self):
        self.engine.enter(self.candidate)
        row=self.db.rows()[0]
        self.assertEqual(row['data']['order_type'],'market_order')
        self.assertEqual(row['signal_key'],f'{PAIR}:ATH:{self.stamp}')
        self.assertEqual(row['data']['confirmation'],RULE)
        args=self.ex.create.call_args
        self.assertEqual(args.args[1],'SELL');self.assertEqual(args.args[3:5],(3,'crossed'))
        self.assertNotIn('limit_price',args.kwargs)
        self.assertLessEqual(D(row['data']['estimated_margin']),D(3))
        self.engine.enter(self.candidate);self.ex.create.assert_called_once()

    def test_price_falling_below_ath_during_preflight_blocks_submission(self):
        self.ex.price.side_effect=[D('1.1'),D('.99')]
        self.engine.enter(self.candidate)
        self.ex.create.assert_not_called()
        self.assertEqual(self.db.rows()[0]['status'],'REJECTED')

    def test_capacity_pause_and_feature_switch_block_entry(self):
        for config in (replace(self.config,enabled=False),replace(self.config,ath_short_enabled=False)):
            Engine(config,self.db,self.ex,self.market,self.emit).enter(self.candidate)
        for n in range(3):self.db.reserve(str(n),f'B-S{n}_USDT',{'side':'SELL'},5)
        self.engine.enter(self.candidate);self.ex.create.assert_not_called()

    def test_old_candle_or_midnight_reference_cannot_trade(self):
        self.engine.enter(replace(self.candidate,candle_at=self.stamp-1800))
        self.engine.enter(replace(self.candidate,reference_end=self.candidate.reference_end-DAY))
        self.ex.create.assert_not_called()

    def test_profit_day_lock_and_ambiguous_post_are_preserved(self):
        self.db.lock_profit(PAIR,self.now)
        self.engine.enter(self.candidate);self.ex.create.assert_not_called()
        self.db.conn.execute('delete from scanner_profit_days');self.db.conn.commit()
        self.ex.create.side_effect=ExchangeError('timeout','orders/create')
        self.engine.enter(self.candidate);self.engine.enter(self.candidate)
        self.ex.create.assert_called_once()
        self.assertEqual(self.db.rows()[0]['status'],'UNCERTAIN')

    def test_same_candle_is_not_retried_after_restart(self):
        self.engine.enter(self.candidate)
        row=self.db.rows()[0];self.db.update(row['id'],'CLOSED')
        Engine(self.config,self.db,self.ex,self.market,self.emit).enter(self.candidate)
        self.ex.create.assert_called_once()

    def test_market_fill_enters_existing_split_profit_management(self):
        self.config=replace(self.config,short_split_tp=True)
        self.engine=Engine(self.config,self.db,self.ex,self.market,self.emit)
        self.engine.enter(self.candidate)
        row=self.db.rows()[0];qty=row['data']['quantity']
        self.ex.find_order.return_value={'id':'entry1','pair':PAIR,'side':'sell','stage':'default',
            'order_type':'market_order','status':'filled','total_quantity':qty,'remaining_quantity':'0',
            'cancelled_quantity':'0','avg_price':'1.1'}
        p={'id':'p1','pair':PAIR,'active_pos':str(-D(qty)),'avg_price':'1.1','margin_type':'crossed',
           'leverage':3,'take_profit_trigger':0,'stop_loss_trigger':0}
        self.ex.positions.return_value=[p];self.ex.all_positions.return_value=[p]
        self.ex.partial_short_limit.return_value='partial'
        self.engine.reconcile(row)
        self.ex.partial_short_limit.assert_called_once()
        fresh=self.db.get(row['id'])
        self.assertEqual(fresh['status'],'OPEN')
        self.assertEqual(D(fresh['data']['short_tp']['tp2']),D('0.88'))


class AthWatcherTests(unittest.TestCase):
    def run_watch(self,capacity,error=None):
        import threading
        from scanner.ath_short import watch
        from scanner.config import ScannerConfig
        stop=threading.Event();stop.wait=Mock(side_effect=lambda delay:stop.set())
        market=Mock();reader=Mock();reader.get.return_value=capacity
        if error:reader.get.side_effect=error
        quote=Quote(PAIR,D('1.1'),D(-5),time.time(),D('.9'),D('1.1'),D('1.4'))
        market.active_instruments.return_value=[PAIR];market.quotes.return_value={PAIR:quote}
        signal=Candidate(PAIR,'SELL',quote.timestamp,D(1),int(quote.timestamp-200*DAY),D(-5),RULE,int(quote.timestamp//900)*900,day_start(quote.timestamp))
        queue=Mock();health={};emit=Mock()
        with tempfile.TemporaryDirectory() as tmp,patch('scanner.market.MarketData',return_value=market),patch('scanner.capacity.BalanceCapacity',return_value=reader),patch('scanner.ath_short.detect',return_value=signal):
            watch(ScannerConfig(database=tmp+'/db',enabled=True,ath_short_enabled=True),queue,health,emit,stop)
        return market,queue,health

    def test_full_capacity_skips_discovery_and_never_queues(self):
        from scanner.capacity import Capacity
        market,queue,health=self.run_watch(Capacity(frozenset('abc'),frozenset(),frozenset('abc')))
        market.active_instruments.assert_not_called();queue.put_nowait.assert_not_called()
        self.assertEqual(health['ath_paused_reason'],'SHORT_CAPACITY_FULL')

    def test_account_error_prevents_discovery(self):
        market,queue,health=self.run_watch(None,ValueError('unavailable'))
        market.active_instruments.assert_not_called();queue.put_nowait.assert_not_called()
        self.assertEqual(health['ath_error'],'ValueError')

    def test_candidate_is_queued_for_serialized_engine_only(self):
        from scanner.capacity import Capacity
        market,queue,health=self.run_watch(Capacity(frozenset(),frozenset(),frozenset()))
        queue.put_nowait.assert_called_once()
        self.assertIsNone(health['ath_error'])
