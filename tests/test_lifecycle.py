import tempfile
import unittest
from types import SimpleNamespace
from decimal import Decimal as D
from app.database import Database
from app.trading import Trader
from app.signals import parse

class Exchange:
    pair = 'B-XAU_USDT'
    def __init__(self):
        self.pos = []
        self.exit_calls = []
        self.protected = False
    def positions(self): return self.pos
    def orders(self, side, status=None):
        return [{'id': 'order', 'status': 'filled', 'total_quantity': '0.005',
                 'remaining_quantity': 0, 'cancelled_quantity': 0}] if status else []
    def find_order(self, side, order_id):
        return self.orders(side, 'filled')[0]
    def protect(self, p, sl, tp):
        p.update(stop_loss_trigger=str(sl), take_profit_trigger=str(tp))
        self.protected = True
    def exit(self, ident): self.exit_calls.append(ident)

class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(self.tmp.name + '/db')
        sid = self.db.signal(-1, 7, 'i', 's', {})
        self.db.reserve(sid, {'message_id': 7, 'dry': False, 'quantity': '0.005', 'side': 'BUY',
                            'sl': '4325.69', 'tp': '4393.02', 'margin': '5',
                            'exchange_order_id': 'order', 'submitted_at': 1})
        self.ex = Exchange()
        self.ex.pos = [{'id': 'position', 'active_pos': '0.005', 'avg_price': '4346.33'}]
        self.trader = Trader(None, self.db, self.ex)
    def tearDown(self):
        self.db.conn.close()
        self.tmp.cleanup()
    def test_recovery_attaches_protection_and_exit_is_once(self):
        self.trader.reconcile()
        self.assertTrue(self.ex.protected)
        self.assertEqual(self.db.active()['status'], 'OPEN')
        self.trader.close('TELEGRAM_PARTIAL')
        self.trader.close('TELEGRAM_PARTIAL')
        self.assertEqual(self.ex.exit_calls, ['position'])
        self.ex.pos = []
        self.trader.reconcile()
        self.assertIsNone(self.db.active())
    def test_protection_failure_exits(self):
        self.ex.protect = lambda *args: None
        self.trader.reconcile()
        self.assertEqual(self.ex.exit_calls, ['position'])
        self.assertEqual(self.db.active()['status'], 'CLOSING')
    def test_pending_close_survives_recovery(self):
        self.trader.close('TELEGRAM_BOOKED')
        self.trader.reconcile()
        self.assertEqual(self.ex.exit_calls, ['position'])
    def test_uncertain_entry_remains_reserved(self):
        self.ex.pos = []
        self.ex.find_order = lambda *args: None
        self.trader.reconcile()
        self.assertIsNotNone(self.db.active())
    def test_rejected_acknowledged_entry_releases_reservation(self):
        self.ex.pos = []
        self.ex.find_order = lambda *args: {'id': 'order', 'status': 'rejected',
            'total_quantity': '.005', 'remaining_quantity': '.005', 'cancelled_quantity': 0}
        self.trader.reconcile()
        self.assertIsNone(self.db.active())
        self.assertEqual(self.db.conn.execute('SELECT status FROM trades').fetchone()[0], 'REJECTED')
    def test_filled_then_closed_before_recovery_releases_reservation(self):
        self.ex.pos = []
        self.trader.reconcile()
        self.assertIsNone(self.db.active())
        self.assertEqual(self.db.conn.execute('SELECT status FROM trades').fetchone()[0], 'CLOSED')
    def test_unacknowledged_entry_stays_uncertain_without_repeating_events(self):
        self.ex.pos = []
        self.db.update(self.db.active(), 'SUBMITTING', exchange_order_id=None)
        self.trader.reconcile()
        before=self.db.conn.execute('SELECT count(*) FROM bot_events').fetchone()[0]
        for _ in range(5): self.trader.reconcile()
        self.assertEqual(self.db.active()['status'], 'UNCERTAIN')
        self.assertEqual(before,self.db.conn.execute('SELECT count(*) FROM bot_events').fetchone()[0])
    def test_unconfirmed_exit_alerts_once_across_restart_without_resubmission(self):
        self.trader.reconcile()
        self.trader.close('TELEGRAM_PARTIAL')
        self.db.update(self.db.active(),'CLOSING',close_requested_at=1)
        for _ in range(5): self.trader.reconcile()
        self.db.conn.close()
        self.db=Database(self.tmp.name+'/db')
        self.trader=Trader(None,self.db,self.ex)
        self.trader.reconcile()
        count=self.db.conn.execute("SELECT count(*) FROM bot_events WHERE event_type='EXIT_UNCONFIRMED'").fetchone()[0]
        self.assertEqual(count,1)
        self.assertEqual(self.ex.exit_calls,['position'])
    def test_flat_with_working_entry_is_not_cleared(self):
        self.ex.pos=[]
        self.ex.find_order=lambda *args: {'id':'order','status':'open'}
        self.trader.reconcile()
        self.assertIsNotNone(self.db.active())
    def test_flat_recovery_rechecks_exchange_before_closing_record(self):
        position=self.ex.pos
        calls=iter([[],position])
        self.ex.positions=lambda: next(calls)
        self.trader.reconcile()
        self.assertIsNotNone(self.db.active())
