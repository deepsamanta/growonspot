import hashlib
import hmac
import json
import tempfile
import time
import unittest
from types import SimpleNamespace
from dataclasses import replace
from decimal import Decimal as D
from unittest.mock import Mock,patch
from urllib.parse import parse_qs
import requests
from binance_scanner.client import Client,BinanceError
from binance_scanner.config import BinanceConfig
from binance_scanner.exchange import Exchange,normalize
from binance_scanner.market import MarketData,pair_name,symbol_name
from binance_scanner.history import FillHistory
from binance_scanner.stream import AccountStream
from scanner.state import ScannerState
from scanner.capacity import account_capacity
from scanner.long_tp import minimum_partial
from scanner.short_tp import partial_quantity
from scanner.market import DAY,MarketDataError

PAIR='B-TEST_USDT'


def order(oid=1,side='SELL',qty='10',filled='0',status='NEW',**extra):
    return dict(symbol='TESTUSDT',orderId=oid,side=side,origQty=qty,executedQty=filled,status=status,
                type='LIMIT',price='1.02',avgPrice='1.02' if D(filled) else '0',positionSide='BOTH',
                reduceOnly=False,closePosition=False,time=1000,clientOrderId='gosb_test',**extra)


def algo(oid=5,status='NEW',**extra):
    return dict(symbol='TESTUSDT',algoId=oid,side='BUY',quantity='0',algoStatus=status,orderType='TAKE_PROFIT_MARKET',
                triggerPrice='.8',positionSide='BOTH',closePosition=True,clientAlgoId='gosb_native',**extra)


def position(qty='-10',**extra):
    return dict(symbol='TESTUSDT',positionSide='BOTH',positionAmt=qty,entryPrice='1',marginType='cross',leverage='3',**extra)


def response(data,status=200,headers=None):
    r=Mock(ok=status<400,status_code=status,headers=headers or {});r.json.return_value=data;return r


def symbol(base='TEST',kind='COIN',status='TRADING'):
    return dict(symbol=base+'USDT',baseAsset=base,quoteAsset='USDT',marginAsset='USDT',contractType='PERPETUAL',
        status=status,underlyingType=kind,onboardDate=0,orderTypes=['LIMIT','MARKET','STOP_MARKET'],
        pricePrecision=8,quantityPrecision=8,filters=[dict(filterType='LOT_SIZE',minQty='1',maxQty='100000',stepSize='1'),
        dict(filterType='MARKET_LOT_SIZE',minQty='1',maxQty='10000',stepSize='1'),
        dict(filterType='PRICE_FILTER',minPrice='.001',maxPrice='100000',tickSize='.001'),
        dict(filterType='MIN_NOTIONAL',notional='5'),dict(filterType='PERCENT_PRICE',multiplierUp='1.15',multiplierDown='.85')])


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.http=Mock();self.c=Client('key','secret',self.http,clock=lambda:1000);self.c.synced=1000
    def test_signed_exact_query_with_decimal_strings(self):
        self.http.request.return_value=response({'orderId':1})
        self.c.call('POST','/fapi/v1/order',{'symbol':'TESTUSDT','quantity':'0.01000000'})
        query=self.http.request.call_args.kwargs['params'];unsigned,sig=query.rsplit('&signature=',1)
        self.assertEqual(sig,hmac.new(b'secret',unsigned.encode(),hashlib.sha256).hexdigest())
        self.assertEqual(parse_qs(unsigned)['quantity'],['0.01000000'])
        self.assertEqual(self.http.request.call_args.kwargs['headers'],{'X-MBX-APIKEY':'key'})
    def test_public_calls_never_send_credentials(self):
        self.http.request.return_value=response({})
        self.c.call('GET','/fapi/v1/time',signed=False)
        self.assertEqual(self.http.request.call_args.kwargs['headers'],{})
        self.assertNotIn('signature',self.http.request.call_args.kwargs['params'])
    def test_write_timeout_is_never_retried_or_leaked(self):
        self.http.request.side_effect=requests.Timeout('key secret signed-URL')
        with self.assertRaises(BinanceError) as context:self.c.call('POST','/fapi/v1/order',{})
        self.assertEqual(str(context.exception),'Timeout');self.assertEqual(self.http.request.call_count,1)
    def test_unknown_server_error_is_not_a_definite_rejection(self):
        for status,code in ((503,None),(400,-1007),(400,-1006)):
            self.http.request.return_value=response({'code':code},status)
            with self.assertRaises(BinanceError) as c:self.c.call('POST','/fapi/v1/order',{})
            self.assertFalse(c.exception.rejected)
    def test_filter_rejection_is_definite(self):
        self.http.request.return_value=response({'code':-4164,'msg':'private detail'},400)
        with self.assertRaises(BinanceError) as c:self.c.call('POST','/fapi/v1/order',{})
        self.assertTrue(c.exception.rejected);self.assertNotIn('private detail',str(c.exception))
    def test_rate_limit_cooldown_prevents_hammering(self):
        self.http.request.return_value=response({},429,{'Retry-After':'60'})
        for _ in range(2):
            with self.assertRaises(BinanceError):self.c.call('GET','/fapi/v1/balance',{})
        self.assertEqual(self.http.request.call_count,1)
    def test_read_timestamp_error_resyncs_only_reads(self):
        self.http.request.side_effect=[response({'code':-1021},400),response({'serverTime':1000100}),response([])]
        self.assertEqual(self.c.call('GET','/fapi/v1/openOrders'),[])
        self.assertEqual(self.c.offset,100)
    def test_clock_sync_precedes_first_signed_call(self):
        self.c.synced=None;self.http.request.side_effect=[response({'serverTime':1001000}),response([])]
        self.c.call('GET','/fapi/v3/balance')
        self.assertEqual(self.c.offset,1000)


class MarketTests(unittest.TestCase):
    def setUp(self):
        self.client=Mock();self.m=MarketData(self.client,clock=lambda:2000000000)
        self.m.symbols={PAIR:symbol()};self.m.info_at=2000000000
        self.client.call.return_value={'symbol':'TESTUSDT','brackets':[dict(initialLeverage=20,notionalCap=100000)]}
    def test_crypto_perpetual_discovery_excludes_gold_tradfi_and_closed(self):
        self.m.symbols={};self.client.call.return_value={'symbols':[symbol(),symbol('XAU','COMMODITY'),symbol('STOCK','EQUITY'),symbol('OLD',status='SETTLING')]}
        self.assertEqual(self.m.active_instruments(),[PAIR])
    def test_exact_filter_steps_not_display_precision_and_close_exemption(self):
        info=self.m.metadata(PAIR)
        self.assertEqual(info['quantity_increment'],'1');self.assertEqual(info['price_increment'],'.001')
        self.assertEqual(info['min_notional'],'5');self.assertEqual(info['close_min_notional'],'0')
        self.assertEqual(minimum_partial(info,D(6),D('1.07')),D(1))
        self.assertEqual(partial_quantity(info,D(2),D('.93')),D(1))
    def test_coindcx_minimums_unchanged_without_override(self):
        info=self.m.metadata(PAIR);info.pop('close_min_notional')
        self.assertEqual(minimum_partial(info,D(6),D('1.07')),D(5))
    def test_symbol_conversion_is_reversible(self):
        for s in ('1000PEPEUSDT','ETHUSDT'):
            self.assertEqual(symbol_name(pair_name(s)),s)
        with self.assertRaises(ValueError):pair_name('BTCUSD')
    def test_stale_book_is_rejected(self):
        self.client.call.return_value={'symbol':'TESTUSDT','time':1,'askPrice':'1','bidPrice':'.9'}
        with self.assertRaises(MarketDataError):self.m.price(PAIR,'BUY')
    def test_four_hour_uses_completed_native_candles(self):
        self.m.candles=Mock(return_value=[]);self.m.four_hour(PAIR,2000000000)
        a=self.m.candles.call_args.args;self.assertEqual(a[-2:],('4h',14400));self.assertEqual(a[2]%14400,0)
    def test_candle_windows_are_bounded_and_include_all_completed_rows(self):
        now=2000000000//DAY*DAY;self.m.clock=lambda:now
        def candles(method,path,params,**kwargs):
            return [[t,'1','2','.5','1','1',t+DAY*1000-1] for t in range(params['startTime'],params['endTime']+1,DAY*1000)]
        self.client.call.side_effect=candles
        rows=self.m.candles(PAIR,now-1200*DAY,now)
        self.assertEqual(len(rows),1200);self.assertEqual(self.client.call.call_count,2)
    def test_candle_api_failure_never_becomes_empty_history(self):
        self.client.call.return_value={'code':-1}
        with self.assertRaises(MarketDataError):self.m.candles(PAIR,1000,2000)
    def test_minimum_notional_exempt_only_for_reduce_only_close(self):
        self.client.call.return_value={'symbol':'TESTUSDT','time':2000000000000,'markPrice':'1'}
        self.m.validate_order(PAIR,'SELL',D(1),D('1.07'),True)
        with self.assertRaises(MarketDataError):self.m.validate_order(PAIR,'BUY',D(1),reduce=False)
    def test_price_bounds_use_mark_instead_of_last_trade(self):
        self.client.call.return_value={'symbol':'TESTUSDT','time':2000000000000,'markPrice':'1'}
        with self.assertRaises(MarketDataError):self.m.validate_order(PAIR,'BUY',D(6),D('1.151'))
        with self.assertRaises(MarketDataError):self.m.validate_order(PAIR,'SELL',D(6),D('.849'))
        self.m.validate_order(PAIR,'BUY',D(1),D('.50'),True)
    def test_invalid_step_is_rejected_before_submission(self):
        with self.assertRaises(MarketDataError):self.m.validate_order(PAIR,'BUY',D('6.5'))


class ExchangeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=ScannerState(self.tmp.name+'/state.db')
        self.client=Mock();self.market=Mock();self.c=BinanceConfig(enabled=True,key='k',secret='s')
        self.ex=Exchange(self.c,self.market,self.db,self.client);self.ex.account_checked=time.time();self.ex.fee_checked=time.time()
    def tearDown(self):self.db.conn.close();self.tmp.cleanup()
    def test_cancelled_partial_fill_keeps_executed_quantity(self):
        o=normalize(order(qty='10',filled='3',status='CANCELED'))
        self.assertEqual((o['status'],o['remaining_quantity'],o['cancelled_quantity']),('partially_cancelled','0','7'))
    def test_hedge_mode_cannot_be_silently_adopted(self):
        raw=order();raw['positionSide']='SHORT'
        with self.assertRaises(BinanceError):normalize(raw)
    def test_account_mode_is_checked_without_changing_it(self):
        self.ex.account_checked=0;self.client.call.side_effect=[{'dualSidePosition':True},{'multiAssetsMargin':False}]
        with self.assertRaises(BinanceError):self.ex.account_mode()
        self.assertTrue(all(c.args[0]=='GET' for c in self.client.call.call_args_list))
    def test_resting_long_and_short_exits_are_reduce_only_gtc(self):
        self.client.call.side_effect=lambda method,path,p:order(side=p['side'],qty=p['quantity'])
        self.ex.partial_short_limit(PAIR,D(1),3,D('.93'))
        self.ex.partial_long_limit(PAIR,D(1),D('1.07'))
        for c in self.client.call.call_args_list:
            p=c.args[2];self.assertEqual(p['reduceOnly'],'true');self.assertEqual(p['timeInForce'],'GTC')
            self.assertNotIn('stopPrice',p);self.assertNotIn('take_profit_price',p)
    def test_long_entry_is_market_without_time_in_force(self):
        self.client.call.return_value={**order(side='BUY',qty='6',filled='6',status='FILLED'),'type':'MARKET'}
        self.ex.create(PAIR,'BUY',D(6),1,'isolated',D('1.07'))
        p=self.client.call.call_args.args[2]
        self.assertEqual(p['type'],'MARKET');self.assertNotIn('timeInForce',p);self.assertNotIn('reduceOnly',p)
    def test_lost_ack_queries_client_id_and_never_reposts(self):
        self.client.call.side_effect=[BinanceError('Timeout'),order(side='SELL',qty='9')]
        oid=self.ex.create(PAIR,'SELL',D(9),3,'crossed',D('.93'),D('1.02'))
        self.assertEqual(oid,'1');self.assertEqual([c.args[0] for c in self.client.call.call_args_list],['POST','GET'])
        self.assertEqual(self.client.call.call_args.args[2]['origClientOrderId'],self.client.call.call_args_list[0].args[2]['newClientOrderId'])
    def test_unresolved_ack_remains_uncertain(self):
        self.client.call.side_effect=[BinanceError('Timeout'),BinanceError('Not found',status=400,code=-2013)]
        with self.assertRaises(BinanceError) as c:self.ex.create(PAIR,'SELL',D(9),3,'crossed',D('.93'),D('1.02'))
        self.assertFalse(c.exception.rejected)
    def test_durable_intent_exists_before_post(self):
        def call(method,path,p):
            self.assertEqual(self.db.cache('binance:intent:'+p['newClientOrderId'])['state'],'submitting')
            return order(qty=p['quantity'])
        self.client.call.side_effect=call;self.ex.create(PAIR,'SELL',D(9),3,'crossed',D('.93'),D('1.02'))
    def test_final_tp_uses_algo_close_all_without_quantity(self):
        ident,_=self.db.reserve('s',PAIR,{'side':'SELL'},5);self.db.update(ident,'OPEN')
        p={'pair':PAIR,'active_pos':'-10'};self.ex.orders=Mock(return_value=[])
        self.client.call.return_value=algo()
        self.ex.take_profit(PAIR,p,D('.8'))
        args=self.client.call.call_args.args
        self.assertEqual(args[:2],('POST','/fapi/v1/algoOrder'))
        self.assertEqual(args[2]['closePosition'],'true');self.assertEqual(args[2]['triggerPrice'],'0.8')
        self.assertNotIn('quantity',args[2]);self.assertNotIn('reduceOnly',args[2])
    def test_uncertain_native_tp_is_not_recreated_after_restart(self):
        ident,_=self.db.reserve('s',PAIR,{'side':'SELL'},5);self.db.update(ident,'OPEN')
        self.ex.orders=Mock(return_value=[]);self.client.call.side_effect=BinanceError('Timeout')
        for _ in range(2):
            with self.assertRaises(BinanceError):self.ex.take_profit(PAIR,{'pair':PAIR,'active_pos':'-10'},D('.8'))
        self.assertEqual(sum(c.args[0]=='POST' for c in self.client.call.call_args_list),1)
    def test_manual_closing_order_blocks_native_tp(self):
        self.ex.orders=Mock(return_value=[normalize(order(side='BUY'))])
        with self.assertRaises(BinanceError):self.ex.take_profit(PAIR,{'pair':PAIR,'active_pos':'-10'},D('.8'))
        self.client.call.assert_not_called()
    def test_terminal_orders_are_available_after_exchange_history_expiry(self):
        self.client.call.return_value=order(filled='10',status='FILLED')
        first=self.ex.find_order(PAIR,'SELL','1');self.client.call.reset_mock()
        self.assertEqual(self.ex.find_order(PAIR,'SELL','1'),first);self.client.call.assert_not_called()
    def test_capacity_includes_manual_entry_but_not_reduce_only_exit(self):
        self.client.call.side_effect=[[position()],[{**order(side='BUY',qty='10'),'reduceOnly':True}],[]]
        cap=account_capacity(self.ex.all_positions(),[])
        self.assertEqual((len(cap.shorts),len(cap.longs),len(cap.pairs)),(1,0,1))
    def test_pending_entry_occupies_a_slot_before_fill(self):
        self.client.call.side_effect=[[position('0')],[order(qty='10')],[]]
        cap=account_capacity(self.ex.all_positions(),[])
        self.assertEqual((len(cap.shorts),len(cap.pairs)),(1,1))
    def test_disabled_blocks_entries_but_allows_exit_management(self):
        self.ex.c=replace(self.c,enabled=False)
        with self.assertRaises(BinanceError):self.ex.average_long(PAIR,D(6))
        self.client.call.return_value=order(side='BUY',qty='1')
        self.ex.partial_short_limit(PAIR,D(1),3,D('.93'))
    def test_gold_cannot_be_sent_to_binance(self):
        with self.assertRaises(ValueError):self.ex.partial_short_limit('B-XAU_USDT',D(1),3,D(100))
        self.client.call.assert_not_called()
    def test_local_validation_failure_is_definite_without_post(self):
        self.market.validate_order.side_effect=MarketDataError('Invalid quantity')
        with self.assertRaises(BinanceError) as error:self.ex.average_long(PAIR,D(6))
        self.assertTrue(error.exception.rejected);self.client.call.assert_not_called()


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=ScannerState(self.tmp.name+'/state.db');self.client=Mock()
        self.now=2000000000;self.h=FillHistory(self.client,self.db,clock=lambda:self.now)
    def tearDown(self):self.db.conn.close();self.tmp.cleanup()
    def fill(self,ident,stamp=None):
        return dict(id=ident,symbol='TESTUSDT',positionSide='BOTH',orderId=1,time=int((stamp or self.now-1)*1000),
                    side='SELL',qty='1',price='1',realizedPnl='0',commission='.001',commissionAsset='USDT')
    def test_more_than_thousand_same_timestamp_fills_are_not_lost(self):
        self.client.call.side_effect=[[self.fill(i) for i in range(1000)],[self.fill(1000)]]
        result=self.h.fills(PAIR,self.now-100)
        self.assertEqual(len(result),1001)
        p=self.client.call.call_args.args[2];self.assertEqual(p['fromId'],1000)
        self.assertNotIn('startTime',p);self.assertNotIn('endTime',p)
    def test_overlap_and_restart_do_not_double_count(self):
        self.client.call.return_value=[self.fill(1)]
        self.assertEqual(len(self.h.fills(PAIR,self.now-100)),1)
        other=FillHistory(self.client,self.db,clock=lambda:self.now)
        self.assertEqual(len(other.fills(PAIR,self.now-100)),1)
    def test_failed_read_never_advances_coverage(self):
        self.client.call.side_effect=BinanceError('timeout')
        with self.assertRaises(BinanceError):self.h.fills(PAIR,self.now-100)
        self.assertIsNone(self.db.conn.execute('SELECT * FROM binance_fill_coverage').fetchone())
    def test_query_windows_never_exceed_seven_days(self):
        self.client.call.return_value=[];self.h.fills(PAIR,self.now-20*DAY)
        self.assertEqual(self.client.call.call_count,3)
        for c in self.client.call.call_args_list:
            p=c.args[2];self.assertLess(p['endTime']-p['startTime'],7*DAY*1000)
    def test_shorter_query_does_not_skip_restart_gap_for_older_position(self):
        self.client.call.return_value=[];self.h.fills(PAIR,self.now-10*DAY)
        old=self.now;self.now+=5*DAY;self.client.call.reset_mock();self.h.fills(PAIR,self.now-DAY)
        self.assertEqual(self.client.call.call_args.args[2]['startTime'],old*1000-60000)
    def test_missing_history_older_than_retention_blocks_instead_of_guessing(self):
        with self.assertRaisesRegex(BinanceError,'retention'):self.h.fills(PAIR,self.now-91*DAY)
        self.client.call.assert_not_called()
    def test_nonprogressing_ids_are_rejected(self):
        self.client.call.side_effect=[[self.fill(i) for i in range(1000)],[self.fill(999)]]
        with self.assertRaisesRegex(BinanceError,'Non-progressing'):self.h.fills(PAIR,self.now-100)
    def test_cached_old_history_survives_long_position(self):
        self.client.call.return_value=[self.fill(1)];start=self.now-100
        self.h.fills(PAIR,start)
        for _ in range(4):
            self.now+=30*DAY;self.client.call.return_value=[];self.h.fills(PAIR,start)
        self.assertEqual(len(self.h.fills(PAIR,start)),1)


class StreamTests(unittest.TestCase):
    def test_order_and_position_events_wake_existing_reconciliation(self):
        s=AccountStream('k','s',Mock())
        s.receive_binance({'e':'ORDER_TRADE_UPDATE','o':{'s':'TESTUSDT','i':1,'X':'FILLED','q':'2','z':'2','S':'SELL'}})
        self.assertTrue(s.wake.is_set());self.assertTrue(s.snapshot()['private_event_seen'])
        s.wake.clear();s.receive_binance({'e':'ACCOUNT_UPDATE','a':{'P':[{'s':'TESTUSDT','pa':'3','ep':'1','ps':'BOTH','mt':'isolated'}]}})
        self.assertTrue(s.wake.is_set())
    def test_expired_key_reconnects_instead_of_silent_stall(self):
        s=AccountStream('k','s',Mock())
        with self.assertRaises(ConnectionError):s.receive_binance({'e':'listenKeyExpired'})
    def test_gold_events_do_not_wake_scanner(self):
        s=AccountStream('k','s',Mock());s.receive_binance({'e':'ORDER_TRADE_UPDATE','o':{'s':'XAUUSDT','i':1,'X':'FILLED'}})
        self.assertFalse(s.wake.is_set())
    def test_current_private_url_and_no_shared_key_deletion(self):
        client=Mock();client.call.return_value={'listenKey':'abc123'};socket=Mock();connect=Mock(return_value=socket)
        s=AccountStream('k','s',Mock(),client=client,connect=connect)
        def receive():s.stop.set();return '{}'
        socket.recv.side_effect=receive
        with patch.dict('sys.modules',{'websocket':SimpleNamespace(WebSocketTimeoutException=TimeoutError)}):s.run()
        self.assertEqual(connect.call_args.args[0],'wss://fstream.binance.com/private/ws/abc123')
        self.assertFalse(any(c.args[0]=='DELETE' for c in client.call.call_args_list))


class ConfigTests(unittest.TestCase):
    def test_no_keys_can_run_disabled_but_not_live(self):
        c=BinanceConfig(key='',secret='',enabled=False);c.validate()
        with self.assertRaises(ValueError):replace(c,enabled=True).validate()
    def test_binance_cannot_share_existing_scanner_database(self):
        with self.assertRaises(ValueError):BinanceConfig(database='scanner-data/scanner.sqlite3').validate()
