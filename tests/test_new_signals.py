import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from app.database import Database
from app.entry_rules import is_new_announcement, is_update_text
from app.messages import MessageProcessor
from app.signals import parse

CARD = 'XAUUSD buy 0.50\n4334.50 → 4333.11\n#2025944568\nS/L: 4298.81\nT/P: 4381.12'

class NewSignalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'db')
        self.db = Database(self.path)
        self.c = SimpleNamespace(channel=-1001496382172, enabled=True, max_age=1200,
            new_signal_window=1200, keywords=('partial','booked','closed','taken'), database=self.path)
        self.trader = Mock()
        async def worker(fn, *args): return fn(*args)
        self.worker = worker
        self.processor = MessageProcessor(self.c, self.db, self.trader, worker)
        self.ocr = patch('app.messages.extract_image', return_value=(parse(CARD), CARD))
        self.ocr_mock = self.ocr.start()
    def tearDown(self):
        self.ocr.stop()
        self.db.conn.close()
        self.tmp.cleanup()
    def message(self, ident, text='', photo=False, age=0, reply=None):
        async def download(file): Path(file).write_bytes(str(ident).encode())
        return SimpleNamespace(id=ident, raw_text=text, photo=photo,
            reply_to_msg_id=reply, date=datetime.fromtimestamp(time.time()-age,timezone.utc),
            download_media=AsyncMock(side_effect=download))
    async def test_new_in_caption(self):
        await self.processor.process(self.message(10,'New gold buy guys',True))
        self.trader.enter.assert_called_once()
    async def test_september24_actual_new_photo_reaches_market_entry(self):
        from app.signals import extract_image
        fixture=Path(__file__).parent/'fixtures'/'signal_20260924_1001.jpg'
        message=self.message(13459,'New',True)
        async def download(file): Path(file).write_bytes(fixture.read_bytes())
        message.download_media=AsyncMock(side_effect=download)
        with patch('app.messages.extract_image',side_effect=extract_image):
            await self.processor.process(message)
        self.trader.enter.assert_called_once()
        signal=self.trader.enter.call_args.args[2]
        self.assertEqual((signal.side,str(signal.entry),str(signal.sl),str(signal.tp)),
                         ('SELL','4285.37','4322.67','4239.04'))
    async def test_new_before_image(self):
        await self.processor.process(self.message(10,'New trade guys'))
        self.trader.enter.assert_not_called()
        await self.processor.process(self.message(11,photo=True))
        self.trader.enter.assert_called_once()
    async def test_new_after_image(self):
        await self.processor.process(self.message(10,photo=True))
        self.trader.enter.assert_not_called()
        await self.processor.process(self.message(11,'This is a NEW buy'))
        self.trader.enter.assert_called_once()
    async def test_new_signal_is_processed_while_existing_trade_is_open(self):
        sid = self.db.signal(self.c.channel, 1, 'first', 'first', {'raw_ocr_text': CARD})
        self.db.reserve(sid, {'message_id': 1})
        self.db.update(self.db.active(), 'OPEN')
        fresh = CARD.replace('2025944568', '2025944569')
        self.ocr_mock.return_value = (parse(fresh), fresh)
        await self.processor.process(self.message(10, 'New buy', True))
        self.trader.enter.assert_called_once()
    async def test_old_ticket_cannot_add_while_open(self):
        sid = self.db.signal(self.c.channel, 1, 'first', 'first', {'raw_ocr_text': CARD})
        self.db.reserve(sid, {'message_id': 1})
        self.db.update(self.db.active(), 'OPEN')
        await self.processor.process(self.message(10, 'New buy', True))
        self.trader.enter.assert_not_called()
    async def test_image_alone_never_opens(self):
        await self.processor.process(self.message(10,photo=True))
        self.trader.enter.assert_not_called()
        self.ocr_mock.assert_not_called()
    async def test_update_caption_cannot_use_old_new_marker(self):
        await self.processor.process(self.message(10,'New'))
        await self.processor.process(self.message(11,'Always profitable in gold.',True))
        await self.processor.process(self.message(12,photo=True))
        self.trader.enter.assert_not_called()
        self.ocr_mock.assert_not_called()
    async def test_partial_invalidates_both_pending_directions(self):
        await self.processor.process(self.message(10,'New'))
        await self.processor.process(self.message(11,'Partial book'))
        await self.processor.process(self.message(12,photo=True))
        self.trader.enter.assert_not_called()
        await self.processor.process(self.message(13,'Partial book'))
        await self.processor.process(self.message(14,'New'))
        self.trader.enter.assert_not_called()
    async def test_one_new_announcement_cannot_authorize_two_images(self):
        await self.processor.process(self.message(10,'New'))
        await self.processor.process(self.message(11,photo=True))
        await self.processor.process(self.message(12,photo=True))
        self.trader.enter.assert_called_once()
    async def test_expired_marker_is_not_used(self):
        self.db.save_context(self.c.channel, {'marker':{'message_id':1,'sent_at':time.time()-1201,'reply_to':None}})
        await self.processor.process(self.message(10,photo=True))
        self.trader.enter.assert_not_called()
    async def test_new_nineteen_minutes_before_image(self):
        await self.processor.process(self.message(10,'New trade',age=1140))
        await self.processor.process(self.message(11,photo=True))
        self.trader.enter.assert_called_once()
    async def test_new_nineteen_minutes_after_image(self):
        await self.processor.process(self.message(10,photo=True,age=1140))
        await self.processor.process(self.message(11,'New trade'))
        self.trader.enter.assert_called_once()
    async def test_image_older_than_twenty_minutes_is_not_used(self):
        await self.processor.process(self.message(10,photo=True,age=1201))
        await self.processor.process(self.message(11,'New trade'))
        self.trader.enter.assert_not_called()
    async def test_pending_image_survives_restart(self):
        await self.processor.process(self.message(10,photo=True))
        self.db.conn.close()
        self.db=Database(self.path)
        self.processor=MessageProcessor(self.c,self.db,self.trader,self.worker)
        await self.processor.process(self.message(11,'New'))
        self.trader.enter.assert_called_once()
    async def test_legacy_ticket_with_changed_sl_is_not_retraded_even_with_new(self):
        sid=self.db.signal(self.c.channel,1,'old-image','old-signal',{'raw_ocr_text':CARD})
        self.db.reserve(sid,{'message_id':1})
        self.db.close(self.db.active(),'TELEGRAM_PARTIAL',0)
        changed=CARD.replace('4298.81','4315.53')
        self.ocr_mock.return_value=(parse(changed),changed)
        await self.processor.process(self.message(10,'New',True))
        self.trader.enter.assert_not_called()
    async def test_new_before_next_image_not_lost_to_old_pending_ticket(self):
        sid=self.db.signal(self.c.channel,1,'old-image','old-signal',{'raw_ocr_text':CARD})
        self.db.reserve(sid,{'message_id':1})
        self.db.close(self.db.active(),'TELEGRAM_PARTIAL',0)
        await self.processor.process(self.message(10,photo=True))
        await self.processor.process(self.message(11,'New'))
        self.trader.enter.assert_not_called()
        fresh=CARD.replace('2025944568','2029999999').replace('4334.50','4335.50')
        self.ocr_mock.return_value=(parse(fresh),fresh)
        await self.processor.process(self.message(12,photo=True))
        self.trader.enter.assert_called_once()

    async def test_september16_new_partial_result_sequence_opens_only_once(self):
        def enter(signal_id, message_id, signal):
            self.db.reserve(signal_id, {'message_id': message_id})
        def close(*args):
            self.db.close(self.db.active(), 'TELEGRAM_PARTIAL', 0)
        self.trader.enter.side_effect=enter
        self.trader.close.side_effect=close
        await self.processor.process(self.message(13408, 'New', True))
        await self.processor.process(self.message(13409, photo=True))
        await self.processor.process(self.message(13410, 'Partial book'))
        self.ocr_mock.return_value=(parse(CARD.replace('4298.81','4315.53')), CARD.replace('4298.81','4315.53'))
        await self.processor.process(self.message(13412, 'Always profitable in gold.', True))
        await self.processor.process(self.message(13414, 'Again made 1.2 lakhs in single trade', True))
        self.trader.enter.assert_called_once()
        self.trader.close.assert_called_once_with('TELEGRAM_PARTIAL', None)
        self.assertIsNone(self.db.active())

    async def test_unrelated_reply_does_not_pair(self):
        await self.processor.process(self.message(10,photo=True))
        await self.processor.process(self.message(11,'New',reply=7))
        self.trader.enter.assert_not_called()
    async def test_update_inside_image_is_rejected(self):
        self.ocr_mock.side_effect=ValueError('SIGNAL_REJECTED_UPDATE_IMAGE')
        await self.processor.process(self.message(10,'New',True))
        self.trader.enter.assert_not_called()

class EntryTextTests(unittest.TestCase):
    def test_mixed_new_phrases_and_negations(self):
        for text in ['NEW', 'New buy guys', 'Take this new sell', 'new gold trade']:
            self.assertTrue(is_new_announcement(text),text)
        for text in ['news', 'renew', 'No new trade', 'Not a new trade', 'New update', 'New channel link', 'Partial book', 'Again made 1.2 lakhs in single trade']:
            self.assertFalse(is_new_announcement(text),text)
    def test_status_with_valid_card_rejected(self):
        for text in ['Closed', 'Partial book', 'TP hit', 'Already profitable']:
            with self.assertRaises(ValueError):parse(CARD+'\n'+text)
    def test_distinct_tickets_with_identical_prices_have_distinct_hashes(self):
        self.assertEqual(parse(CARD).ticket_id,'2025944568')
        self.assertNotEqual(parse(CARD).digest(),parse(CARD.replace('2025944568','2025944569')).digest())
