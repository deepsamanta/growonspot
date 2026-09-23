import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from app.database import Database
from app.exchange import APIError, CoinDCX, EntryNotSubmitted
from app.messages import MessageProcessor, run_cycle
from app.signals import parse
from app.trading import Trader


class ExchangeLookupTests(unittest.TestCase):
    def test_leverage_failure_is_distinct_from_uncertain_order_submission(self):
        ex=CoinDCX(SimpleNamespace(leverage=5))
        ex.request=Mock(side_effect=APIError('HTTP 400','positions/update_leverage',400))
        with self.assertRaises(EntryNotSubmitted): ex.create(Mock(),D('.005'),D(4200),D(4400))
        ex.request.assert_called_once()
    def test_order_timeout_remains_uncertain(self):
        ex=CoinDCX(SimpleNamespace(leverage=5))
        ex.request=Mock(side_effect=[{},APIError('ReadTimeout','orders/create')])
        with self.assertRaises(APIError) as caught: ex.create(SimpleNamespace(side='BUY'),D('.005'),D(4200),D(4400))
        self.assertNotIsInstance(caught.exception,EntryNotSubmitted)
    def test_find_known_order_stops_on_full_first_page(self):
        ex = CoinDCX(SimpleNamespace())
        ex.pair = 'B-XAU_USDT'
        page = [{'id': str(i), 'pair': 'B-OTHER_USDT', 'side': 'sell'} for i in range(99)]
        expected = {'id': 'our-order', 'pair': ex.pair, 'side': 'sell', 'status': 'filled'}
        ex.request = Mock(side_effect=[page + [expected], AssertionError('Must not fetch unrelated history')])
        self.assertEqual(ex.find_order('SELL', 'our-order'), expected)
        self.assertEqual(ex.request.call_count, 1)

    def test_positions_use_server_side_pair_filter(self):
        ex = CoinDCX(SimpleNamespace())
        ex.pair = 'B-XAU_USDT'
        ex.request = Mock(return_value=[{'id': 'p', 'pair': ex.pair}])
        self.assertEqual(len(ex.positions()), 1)
        self.assertEqual(ex.request.call_args.args[1]['pairs'], ex.pair)

    def test_repeated_pages_fail_with_useful_context(self):
        ex = CoinDCX(SimpleNamespace())
        ex.request = Mock(return_value=[{'id': str(i)} for i in range(100)])
        with self.assertRaises(APIError) as caught:
            ex.rows('orders')
        self.assertIn('repeated', str(caught.exception))
        self.assertEqual(caught.exception.details()['endpoint'], 'orders')
        self.assertEqual(ex.request.call_count, 2)

    def test_http_error_retains_endpoint_without_credentials(self):
        ex = CoinDCX(SimpleNamespace(key='private-key', secret='private-secret'))
        ex.http.post = Mock(return_value=SimpleNamespace(ok=False, status_code=401))
        with self.assertRaises(APIError) as caught:
            ex.request('positions', {}, read=True)
        self.assertEqual(caught.exception.http_status, 401)
        self.assertEqual(caught.exception.endpoint, 'positions')
        self.assertNotIn('private', str(caught.exception))
        ex.http.post.assert_called_once()


class MessageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.temp.name) / 'db'))
        self.c = SimpleNamespace(channel=-1001496382172, keywords=('partial','booked','closed','taken'),
                                 enabled=True, new_signal_window=120, max_age=120, database=str(Path(self.temp.name) / 'db'))
        self.trader = Mock()
        self.trader.close.side_effect = lambda *args: None
        async def worker(fn, *args):
            return fn(*args)
        self.processor = MessageProcessor(self.c, self.db, self.trader, worker)

    def tearDown(self):
        self.db.conn.close()
        self.temp.cleanup()

    def active(self, message_id):
        sid = self.db.signal(self.c.channel, message_id, 'hash', 'signal', {})
        self.db.reserve(sid, {'message_id':message_id})

    def caption(self, ident=13400, reply_to=None):
        return SimpleNamespace(id=ident, raw_text='Partial book', photo=True, media=object(),
            reply_to_msg_id=reply_to, date=datetime.now(timezone.utc), download_media=AsyncMock())

    async def test_actual_partial_book_caption_closes_prior_signal_without_ocr(self):
        self.active(13398)
        message = self.caption()
        with patch('app.messages.extract_image', side_effect=AssertionError('Must not OCR exit caption')):
            await self.processor.process(message)
        self.trader.close.assert_called_once_with('TELEGRAM_PARTIAL', None)
        self.trader.enter.assert_not_called()
        message.download_media.assert_not_called()

    async def test_partial_photo_without_active_trade_never_opens(self):
        await self.processor.process(self.caption())
        self.trader.enter.assert_not_called()
        self.trader.close.assert_not_called()

    async def test_older_partial_cannot_close_a_later_trade(self):
        self.active(13402)
        await self.processor.process(self.caption(13400))
        self.trader.close.assert_not_called()

    async def test_unrelated_reply_does_not_close(self):
        self.active(13398)
        await self.processor.process(self.caption(reply_to=13397))
        self.trader.close.assert_not_called()

    async def test_reconcile_failure_cannot_prevent_caption_processing_or_offset(self):
        self.active(13398)
        message = self.caption()
        self.trader.reconcile.side_effect = APIError('Pagination exceeded', 'orders')
        class Client:
            async def iter_messages(self, *args, **kwargs):
                yield message
        offset, healthy = await run_cycle(Client(), object(), 13399, self.processor)
        self.trader.close.assert_called_once()
        self.assertEqual(offset, 13400)
        self.assertEqual(self.db.offset(self.c.channel), 13400)
        self.assertFalse(healthy)


class MarketEntryTests(unittest.TestCase):
    def test_image_market_gap_does_not_skip_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            db=Database(directory+'/db')
            sid=db.signal(-1,13402,'i','s',{})
            c=SimpleNamespace(enabled=True, daily_trades=10, daily_loss=D(10), leverage=5, margin=D(5), dry=False)
            ex=Mock(pair='B-XAU_USDT')
            ex.positions.return_value=[]
            ex.orders.return_value=[]
            ex.price.return_value=D('4260')
            ex.size.return_value=D('0.005')
            ex.tick.side_effect=lambda x:x
            ex.create.return_value='new-order'
            signal=parse('XAUUSD sell 0.50\n4292.13 → 4273.51\nS/L: 4307.92\nT/P: 4257.24')
            Trader(c,db,ex).enter(sid,13402,signal)
            ex.create.assert_called_once()
            self.assertEqual(db.active()['data']['market_price'],'4260')
            self.assertEqual(db.active()['data']['entry'],'4292.13')
            db.conn.close()
