"""Exercise the unchanged execution engine through Binance REST-shaped responses."""
import tempfile
import time
import unittest
from decimal import Decimal as D
from unittest.mock import Mock
from binance_scanner.config import BinanceConfig
from binance_scanner.exchange import Exchange
from binance_scanner.client import BinanceError
from scanner.engine import Engine
from scanner.market import Candle,Quote,DAY
from scanner.state import ScannerState
from scanner.strategy import Candidate
from test_binance_adapter import PAIR,order,position
from test_scanner_strategy import INFO


class SimulatedBinance:
    def __init__(self):
        self.orders={};self.algos={};self.fills=[];self.qty=D(0);self.avg=D(0)
        self.mode='cross';self.leverage=3;self.calls=[];self.price=D('.31');self.next_id=1
    def execute(self,oid,quantity=None,price=None):
        o=self.orders[oid];q=D(quantity or o['origQty'])-D(o['executedQty'])
        price=D(price or o['price'] or self.price)
        old=self.qty;delta=q if o['side']=='BUY' else -q
        if o.get('reduceOnly'):
            assert old*delta<=0 and abs(delta)<=abs(old)
        pnl=D(0)
        if old==0 or old*delta>0:self.avg=(abs(old)*self.avg+q*price)/(abs(old)+q)
        else:pnl=q*(price-self.avg)*(1 if old>0 else -1)
        self.qty+=delta
        if self.qty==0:self.avg=D(0)
        executed=D(o['executedQty'])+q
        o.update(executedQty=str(executed),status='FILLED' if executed==D(o['origQty']) else 'PARTIALLY_FILLED',avgPrice=str(price))
        self.fills.append(dict(id=len(self.fills)+1,orderId=oid,symbol='TESTUSDT',positionSide='BOTH',side=o['side'],
            qty=str(q),price=str(price),time=int(time.time()*1000),commission='.001',commissionAsset='USDT',realizedPnl=str(pnl)))
    def manual_add(self,side,qty,price):
        oid=self.next_id;self.next_id+=1
        self.orders[oid]={**order(oid,side,str(qty)),'price':str(price),'clientOrderId':'manual'}
        self.execute(oid);return oid
    def call(self,method,path,params=None,**kwargs):
        p=dict(params or {});self.calls.append((method,path,p))
        if path.endswith('/positionSide/dual'):return {'dualSidePosition':False}
        if path.endswith('/multiAssetsMargin'):return {'multiAssetsMargin':False}
        if path.endswith('/feeBurn'):return {'feeBurn':False}
        if path.endswith('/balance'):return [{'asset':'USDT','balance':'50'}]
        if path.endswith('/positionRisk'):
            return [{**position(str(self.qty)),'entryPrice':str(self.avg),'marginType':self.mode,'leverage':str(self.leverage)}]
        if path.endswith('/openOrders'):return [dict(o) for o in self.orders.values() if o['status'] in ('NEW','PARTIALLY_FILLED')]
        if path.endswith('/openAlgoOrders'):return [dict(o) for o in self.algos.values() if o['algoStatus']=='NEW']
        if path.endswith('/income'):return []
        if path.endswith('/userTrades'):
            return [dict(f) for f in self.fills if int(p.get('startTime',0))<=f['time']<=int(p.get('endTime',2**63)) and f['id']>=int(p.get('fromId',0))][:1000]
        if path.endswith('/marginType'):
            self.mode='isolated' if p['marginType']=='ISOLATED' else 'cross';return {'code':200}
        if path.endswith('/leverage'):self.leverage=int(p['leverage']);return {'leverage':self.leverage}
        if path.endswith('/order'):
            if method=='POST':
                oid=self.next_id;self.next_id+=1
                self.orders[oid]={**order(oid,p['side'],p['quantity']),'price':p.get('price',str(self.price)),
                    'type':p['type'],'clientOrderId':p['newClientOrderId'],'reduceOnly':p.get('reduceOnly')=='true'}
                if p['type']=='MARKET':self.execute(oid)
                return dict(self.orders[oid])
            oid=int(p.get('orderId') or next(o['orderId'] for o in self.orders.values() if o['clientOrderId']==p['origClientOrderId']))
            if method=='DELETE':self.orders[oid]['status']='CANCELED'
            return dict(self.orders[oid])
        if path.endswith('/algoOrder'):
            if method=='POST':
                oid=self.next_id;self.next_id+=1
                self.algos[oid]=dict(symbol='TESTUSDT',algoId=oid,side=p['side'],quantity='0',algoStatus='NEW',
                    orderType=p['type'],triggerPrice=p['triggerPrice'],positionSide='BOTH',closePosition=True,clientAlgoId=p['clientAlgoId'])
                return dict(self.algos[oid])
            oid=int(p.get('algoId') or next(o['algoId'] for o in self.algos.values() if o['clientAlgoId']==p['clientAlgoId']))
            if method=='DELETE':self.algos[oid]['algoStatus']='CANCELED'
            return dict(self.algos[oid])
        raise AssertionError((method,path,p))


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.db=ScannerState(self.temp.name+'/state.db');self.api=SimulatedBinance()
        self.c=BinanceConfig(enabled=True,key='key',secret='secret');self.market=Mock();self.now=time.time()
        self.info={**INFO,'min_notional':'5','close_min_notional':'0'}
        self.market.metadata.return_value=self.info;self.market.eligible_metadata.return_value=True
        self.market.price.side_effect=lambda pair,side:self.api.price
        self.market.quotes.side_effect=lambda:{PAIR:Quote(PAIR,self.api.price,D(36),time.time(),D('.30'),self.api.price)}
        end=int(self.now//14400)*14400
        self.market.four_hour.return_value=[Candle(t,D('.31'),D('.311'),D('.30'),D('.31'),D(1)) for t in range(end-6*14400,end,14400)]
        self.ex=Exchange(self.c,self.market,self.db,self.api);self.emit=Mock();self.engine=Engine(self.c,self.db,self.ex,self.market,self.emit)
    def tearDown(self):self.db.conn.close();self.temp.cleanup()
    def row(self):return self.db.rows()[0]
    def poll(self,n=1):
        for _ in range(n):self.engine.reconcile(self.row())
    def enter(self,side):
        ref=D('.30') if side=='BUY' else D('.312')
        self.engine.enter(Candidate(PAIR,side,time.time(),ref,int(self.now-200*DAY),D(36)))
        self.assertEqual(self.row()['status'],'SUBMITTED')
        oid=int(self.row()['data']['order_id'])
        if side=='SELL':self.api.execute(oid)
        self.poll();self.assertEqual(self.row()['status'],'OPEN')
        return self.row()
    def test_long_entry_minimum_seven_percent_limit_then_fifteen_runner(self):
        row=self.enter('BUY');state=row['data']['long_tp']
        self.assertEqual(state['tp1_quantity'],'1');self.assertEqual(state['tp1'],'0.332');self.assertEqual(state['tp2'],'0.357')
        self.assertEqual((self.api.mode,self.api.leverage),('isolated',1))
        exit_id=int(row['data']['long_tp_orders'][-1]['order_id']);self.assertTrue(self.api.orders[exit_id]['reduceOnly'])
        self.api.execute(exit_id);self.poll()
        self.assertEqual(self.row()['data']['long_tp']['phase'],'runner')
        self.assertEqual(next(iter(self.api.algos.values()))['triggerPrice'],'0.357')
        count=len(self.api.orders);self.poll(2);self.assertEqual(len(self.api.orders),count)
    def test_short_crossed_limit_then_partial_then_twenty_runner(self):
        row=self.enter('SELL');data=row['data'];state=data['short_tp']
        self.assertEqual(data['limit_price'],'0.319');self.assertEqual((self.api.mode,self.api.leverage),('cross',3))
        self.assertEqual(state['tp1_quantity'],'21');self.assertEqual(state['tp1'],'0.296');self.assertEqual(state['tp2'],'0.255')
        self.api.execute(int(data['tp1_orders'][-1]['order_id']));self.poll()
        row=self.row();self.assertEqual(row['data']['short_tp']['phase'],'runner');self.assertTrue(row['data']['average_margin_mode'])
        self.assertFalse(row['data']['averaging_used']);self.assertEqual(next(iter(self.api.algos.values()))['triggerPrice'],'0.255')
    def test_manual_long_addition_cancels_old_exit_and_consumes_allowance(self):
        row=self.enter('BUY');old=int(row['data']['long_tp_orders'][-1]['order_id'])
        self.api.manual_add('BUY',D(6),D('.20'));self.poll(3)
        row=self.row();self.assertEqual(row['status'],'OPEN');self.assertTrue(row['data']['averaging_used'])
        self.assertEqual(self.api.orders[old]['status'],'CANCELED')
        self.assertNotEqual(row['data']['long_tp']['tp1'],'0.332')
        self.assertEqual(len([o for o in self.api.orders.values() if o['status']=='NEW' and o['side']=='SELL']),1)
    def test_restart_does_not_duplicate_resting_limit(self):
        self.enter('SELL');before=len(self.api.orders)
        self.ex=Exchange(self.c,self.market,self.db,self.api);self.engine=Engine(self.c,self.db,self.ex,self.market,self.emit)
        self.poll(3);self.assertEqual(len(self.api.orders),before)
    def test_flat_manual_close_cancels_bot_exit_and_confirms_pnl(self):
        row=self.enter('BUY');old=int(row['data']['long_tp_orders'][-1]['order_id'])
        self.api.manual_add('SELL',abs(self.api.qty),D('.35'));self.poll(3)
        self.assertEqual(self.api.orders[old]['status'],'CANCELED');self.assertEqual(self.row()['status'],'CLOSED')
        self.assertTrue(D(self.row()['realized_pnl'])>0)
    def test_short_entry_expiry_cancels_only_unfilled_order(self):
        self.engine.enter(Candidate(PAIR,'SELL',time.time(),D('.312'),int(self.now-200*DAY),D(36)))
        row=self.row();self.db.update(row['id'],'SUBMITTED',expires_at=time.time()-1)
        self.poll(2);self.assertEqual(self.row()['status'],'REJECTED');self.assertEqual(self.api.qty,0)
