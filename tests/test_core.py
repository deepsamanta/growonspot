import tempfile
import unittest
from decimal import Decimal as D
from types import SimpleNamespace
from app.signals import parse, should_close_from_text
from app.database import Database
from app.exchange import CoinDCX
from app.trading import Trader

SHORT = 'XAUUSD, sell 0.50\n4 336.09 → 4 289.31\nS / L: 4 338.77\nT / P: 4 289.31'
LONG = 'XAUUSD, buy 0.50\n4 346.33 → 4 359.45\nS/L: 4 325.69\nT/P: 4 393.02'

class CoreTests(unittest.TestCase):
    def test_reference_values(self):
        short, long = parse(SHORT), parse(LONG)
        self.assertEqual((short.side, short.entry, short.sl, short.tp), ('SELL', D('4336.09'), D('4338.77'), D('4289.31')))
        self.assertEqual((long.side, long.entry, long.sl, long.tp), ('BUY', D('4346.33'), D('4325.69'), D('4393.02')))

    def test_reject_incomplete_wrong_symbol_and_multiple_cards(self):
        for text in (SHORT.replace('XAUUSD', 'BTCUSDT'), SHORT.replace('S / L:', 'missing:'), SHORT + '\n' + LONG, SHORT.replace('4 338.77', '4 000.00')):
            with self.assertRaises(ValueError): parse(text)

    def test_keywords(self):
        for text in ('PARTIAL', 'Profit Booked', 'closed now', 'TP taken'):
            self.assertTrue(should_close_from_text(text))
        for text in ('partially', 'unclosed', 'undertaken', 'booking'):
            self.assertFalse(should_close_from_text(text))

    def test_durable_dedup_and_single_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(tmp + '/test.db')
            one = db.signal(-1, 1, 'image', 'signal', {})
            self.assertIsNotNone(one)
            self.assertIsNone(db.signal(-1, 2, 'image', 'signal2', {}))
            self.assertIsNone(db.signal(-1, 3, 'image2', 'signal', {}))
            db.reserve(one, {'message_id': 1})
            db.advance(-1, 100)
            db.advance(-1, 99)
            db.conn.close()
            db = Database(tmp + '/test.db')
            self.assertEqual(db.offset(-1), 100)
            self.assertEqual(db.active()['status'], 'SUBMITTING')
            with self.assertRaises(Exception): db.reserve(2, {})
            db.conn.close()

    def test_margin_rounds_down_and_never_bumps_minimum(self):
        ex = CoinDCX(SimpleNamespace(margin=D(5), leverage=5))
        ex.instrument = {'dynamic_position_leverage_details': {'5': 10000}, 'quantity_increment': '0.001', 'min_quantity': '0.001', 'min_notional': 5, 'max_quantity': 100, 'max_market_order_quantity': 100}
        q = ex.size(D(4300))
        self.assertEqual(q, D('0.005'))
        self.assertLessEqual(q * 4300 / 5, 5)
        ex.instrument['min_quantity'] = '0.01'
        with self.assertRaises(ValueError): ex.size(D(4300))

    def test_unrelated_reply_and_position_mixing_do_not_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(tmp + '/test.db')
            sid = db.signal(-1, 7, 'i', 's', {})
            db.reserve(sid, {'message_id': 7, 'dry': False, 'position_id': 'p', 'quantity': '1', 'side': 'BUY'})
            class Exchange:
                def positions(self): return [{'id': 'p', 'active_pos': '2'}]
                def exit(self, _): raise AssertionError('Must never close mixed position')
            trader = Trader(None, db, Exchange())
            trader.close('TELEGRAM_CLOSED', reply_to=8)
            self.assertEqual(db.active()['status'], 'SUBMITTING')
            trader.close('TELEGRAM_CLOSED', reply_to=7)
            self.assertEqual(db.active()['status'], 'ISOLATION_CONFLICT')
            db.conn.close()

if __name__ == '__main__': unittest.main()
