"""Net-position additions and close-confirm-open reversals; no network calls."""
import tempfile
import time
import unittest
from decimal import Decimal as D
from types import SimpleNamespace
from app.database import Database
from app.signals import Signal
from app.trading import Trader
from app.exchange import EntryNotSubmitted


class NettedExchange:
    pair = 'B-XAU_USDT'
    def __init__(self):
        self.pos = []
        self.history = {}
        self.calls = []
        self.delayed_exit = False
        self.uncertain_create = False
        self.reject_add = False
        self.partial_fill = False
        self.extra_orders = []
    def positions(self): return self.pos
    def orders(self, side, status=None): return self.extra_orders
    def price(self, side): return D('4300')
    def size(self, price): return D('0.005')
    def tick(self, value): return value
    def create(self, signal, qty, sl, tp):
        self.calls.append(('create', signal.side, qty))
        if self.uncertain_create:
            raise TimeoutError('uncertain')
        filled = D(0) if self.reject_add else (D('0.003') if self.partial_fill else qty)
        sign = 1 if signal.side == 'BUY' else -1
        if not self.pos:
            self.pos = [{'id': 'p', 'active_pos': '0', 'avg_price': '4300'}]
        p = self.pos[0]
        p['active_pos'] = str(D(p['active_pos']) + sign * filled)
        ident = str(len(self.history) + 1)
        self.history[ident] = {'id': ident, 'status': 'rejected' if self.reject_add else 'filled',
            'side': signal.side.lower(), 'pair': self.pair, 'total_quantity': str(qty),
            'remaining_quantity': '0', 'cancelled_quantity': str(qty-filled), 'avg_price': '4300'}
        return ident
    def find_order(self, side, ident): return self.history.get(ident)
    def protect(self, position, sl, tp):
        position.update(stop_loss_trigger=str(sl), take_profit_trigger=str(tp))
    def exit(self, ident):
        self.calls.append(('exit', ident))
        if not self.delayed_exit:
            self.pos = []
    def cancel(self, ident): self.calls.append(('cancel', ident))


class StackingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = self.tmp.name + '/db'
        self.db = Database(self.path)
        self.c = SimpleNamespace(enabled=True, daily_trades=10, daily_loss=D(100),
            leverage=5, margin=D(5), dry=False, max_age=1200)
        self.ex = NettedExchange()
        self.trader = Trader(self.c, self.db, self.ex)
        self.ident = 0
    def tearDown(self):
        self.db.conn.close()
        self.tmp.cleanup()
    def enter(self, side='BUY', sl=None, tp=None):
        self.ident += 1
        signal = Signal('XAUUSDT', side, D(4300),
            D(sl or (4200 if side == 'BUY' else 4400)),
            D(tp or (4400 if side == 'BUY' else 4200)), str(2000000+self.ident))
        sid = self.db.signal(-1,self.ident,str(self.ident),str(self.ident),signal.data())
        self.trader.enter(sid,self.ident,signal)
        return sid
    def restart(self):
        self.db.conn.close()
        self.db = Database(self.path)
        self.trader = Trader(self.c,self.db,self.ex)
    def test_same_side_adds_without_exit_and_uses_latest_protection(self):
        self.enter()
        self.enter(sl=4250,tp=4450)
        self.enter(sl=4260,tp=4460)
        self.assertEqual(self.ex.calls,[('create','BUY',D('.005'))]*3)
        trade = self.db.active()
        self.assertEqual(trade['status'],'OPEN')
        self.assertEqual(D(trade['data']['quantity']),D('.015'))
        self.assertEqual(D(trade['data']['margin']),D(15))
        self.assertEqual(self.ex.pos[0]['stop_loss_trigger'],'4260')
        self.assertEqual(self.ex.pos[0]['take_profit_trigger'],'4460')
        self.assertEqual(self.db.daily(),(3,0))
        self.assertTrue(self.db.has_traded_ticket(-1,'2000002'))
        self.restart()
        self.trader.reconcile()
        self.assertEqual(len(self.ex.calls),3)
    def test_long_to_short_closes_all_additions_first(self):
        self.enter()
        self.enter()
        self.enter('SELL')
        self.assertEqual([x[:2] for x in self.ex.calls],[('create','BUY'),('create','BUY'),('exit','p'),('create','SELL')])
        self.assertEqual(self.db.active()['data']['side'],'SELL')
        self.assertEqual(D(self.ex.pos[0]['active_pos']),D('-.005'))
    def test_short_to_long_and_short_add(self):
        self.enter('SELL')
        self.enter('SELL')
        self.assertEqual(D(self.ex.pos[0]['active_pos']),D('-.010'))
        self.enter('BUY')
        self.assertEqual([x[:2] for x in self.ex.calls][-2:],[('exit','p'),('create','BUY')])
    def test_delayed_exit_restart_does_not_open_or_exit_twice(self):
        self.enter()
        self.ex.delayed_exit = True
        self.enter('SELL')
        self.assertEqual(self.db.active()['status'],'CLOSING')
        self.restart()
        self.trader.reconcile()
        self.assertEqual(len(self.ex.calls),2)
        self.ex.pos = []
        self.trader.reconcile()
        self.trader.reconcile()
        self.assertEqual([x[:2] for x in self.ex.calls],[('create','BUY'),('exit','p'),('create','SELL')])
    def test_partial_closes_combined_position_and_reply_to_addition_matches(self):
        self.enter()
        self.enter()
        self.trader.close('TELEGRAM_PARTIAL',reply_to=2)
        self.trader.reconcile()
        self.assertIsNone(self.db.active())
        self.assertEqual(self.ex.calls[-1],('exit','p'))
        self.assertEqual(self.db.daily(),(2,10))
    def test_partial_cancels_reversal_before_flat(self):
        self.enter()
        self.ex.delayed_exit = True
        self.enter('SELL')
        self.trader.close('TELEGRAM_PARTIAL')
        self.ex.pos = []
        self.trader.reconcile()
        self.assertIsNone(self.db.active())
        self.assertIsNone(self.db.pending())
        self.assertEqual(len(self.ex.calls),2)
    def test_uncertain_add_never_retries_or_exits_unowned_quantity(self):
        self.enter()
        self.ex.uncertain_create = True
        self.enter()
        self.trader.close('TELEGRAM_PARTIAL')
        self.restart()
        self.trader.reconcile()
        self.assertEqual(self.db.active()['status'],'UNCERTAIN')
        self.assertEqual(len(self.ex.calls),2)
    def test_add_acknowledged_then_restart_recovers_exact_fill_once(self):
        self.enter()
        original = self.trader._reconcile
        self.trader._reconcile = lambda: None
        self.enter(sl=4250,tp=4450)
        self.trader._reconcile = original
        self.restart()
        self.trader.reconcile()
        self.trader.reconcile()
        self.assertEqual(D(self.db.active()['data']['quantity']),D('.010'))
        self.assertEqual(len(self.ex.calls),2)
    def test_partial_fill_add_tracks_actual_quantity(self):
        self.enter()
        self.ex.partial_fill = True
        self.enter()
        self.assertEqual(D(self.db.active()['data']['quantity']),D('.008'))
        self.assertEqual(self.db.active()['status'],'OPEN')
    def test_rejected_add_keeps_existing_position(self):
        self.enter()
        self.ex.reject_add = True
        self.enter(sl=4250,tp=4450)
        self.assertEqual(D(self.db.active()['data']['quantity']),D('.005'))
        self.assertEqual(self.ex.pos[0]['stop_loss_trigger'],'4200')
        self.assertEqual(self.db.active()['status'],'OPEN')
    def test_external_quantity_change_blocks_add_and_reverse(self):
        self.enter()
        self.ex.pos[0]['active_pos'] = '.008'
        self.enter('SELL')
        self.assertEqual(self.db.active()['status'],'ISOLATION_CONFLICT')
        self.assertEqual(len(self.ex.calls),1)
    def test_expired_reversal_does_not_open_after_delayed_close(self):
        self.enter()
        self.ex.delayed_exit = True
        sid = self.enter('SELL')
        self.db.conn.execute('UPDATE entry_queue SET created_at=? WHERE signal_id=?',(time.time()-1201,sid))
        self.db.conn.commit()
        self.ex.pos = []
        self.trader.reconcile()
        self.assertIsNone(self.db.active())
        self.assertIsNone(self.db.pending())
        self.assertEqual(len(self.ex.calls),2)
    def test_daily_limit_blocks_add_and_reversal_before_exit(self):
        self.c.daily_trades=1
        self.enter()
        self.enter()
        self.enter('SELL')
        self.assertEqual(len(self.ex.calls),1)
    def test_replayed_signal_id_cannot_place_duplicate_add(self):
        self.enter()
        sid = self.enter()
        signal = Signal('XAUUSDT','BUY',D(4300),D(4200),D(4400))
        self.trader.enter(sid,2,signal)
        self.assertEqual(len(self.ex.calls),2)
    def test_protection_failure_after_add_exits_combined_position(self):
        self.enter()
        self.ex.protect = lambda *args: None
        self.enter(sl=4250,tp=4450)
        self.assertEqual(self.ex.calls[-1],('exit','p'))
        self.trader.reconcile()
        self.assertIsNone(self.db.active())
    def test_unrelated_order_blocks_add(self):
        self.enter()
        self.ex.extra_orders=[{'id':'external','stage':'default'}]
        self.enter()
        self.assertEqual(len(self.ex.calls),1)
    def test_preparation_failure_does_not_leave_phantom_position(self):
        def fail(*args): raise EntryNotSubmitted('HTTP 400','positions/update_leverage',400)
        self.ex.create = fail
        self.enter()
        self.assertIsNone(self.db.active())
        self.assertEqual(self.db.daily(),(0,0))
    def test_preparation_failure_on_add_keeps_original_position(self):
        self.enter()
        def fail(*args): raise EntryNotSubmitted('HTTP 400','positions/update_leverage',400)
        self.ex.create = fail
        self.enter()
        self.assertEqual(self.db.active()['status'],'OPEN')
        self.assertEqual(D(self.db.active()['data']['quantity']),D('.005'))
        self.assertEqual(self.db.daily(),(1,0))
    def test_bot_full_position_protection_does_not_block_add(self):
        self.enter()
        self.ex.extra_orders=[{'id':'sl','stage':'tpsl_exit','status':'untriggered',
            'side':'sell','order_type':'stop_market','stop_price':'4200'}]
        self.enter(sl=4250,tp=4450)
        self.assertEqual(len(self.ex.calls),2)
        self.assertEqual(self.db.active()['status'],'OPEN')
    def test_partial_during_add_closes_after_fill_recovery(self):
        self.enter()
        self.trader._reconcile = lambda: None
        self.enter()
        self.trader.close('TELEGRAM_PARTIAL')
        self.assertEqual(len(self.ex.calls),2)
        self.restart()
        self.trader.reconcile()
        self.assertEqual(self.ex.calls[-1],('exit','p'))
        self.trader.reconcile()
        self.assertIsNone(self.db.active())
    def test_expiry_uses_original_image_time(self):
        sid=self.db.signal(-1,99,'expired','expired',{'source_sent_at':time.time()-1201})
        signal=Signal('XAUUSDT','BUY',D(4300),D(4200),D(4400))
        self.trader.enter(sid,99,signal)
        self.assertEqual(self.ex.calls,[])
    def test_position_endpoint_lag_after_filled_add_recovers_without_resubmission(self):
        self.enter()
        self.trader._reconcile = lambda: None
        self.enter()
        self.ex.pos[0]['active_pos'] = '.005'
        self.restart()
        self.trader.reconcile()
        self.assertEqual(self.db.active()['status'],'ADDING')
        self.ex.pos[0]['active_pos'] = '.010'
        self.trader.reconcile()
        self.assertEqual(self.db.active()['status'],'OPEN')
        self.assertEqual(len(self.ex.calls),2)
