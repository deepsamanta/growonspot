import asyncio
import tempfile
import unittest
from datetime import datetime,timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock
from telethon.errors import FloodWaitError
from app.database import Database
from app.messages import MessageProcessor,run_cycle
from app.telegram_reader import TelegramReader


class TelegramRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=Database(str(Path(self.tmp.name)/'db'))
        self.config=SimpleNamespace(channel=-1001496382172,keywords=('partial','booked','closed','taken'),
            enabled=True,new_signal_window=1200,max_age=1200,database=str(Path(self.tmp.name)/'db'))
        self.trader=Mock()
        async def worker(fn,*args):return fn(*args)
        self.processor=MessageProcessor(self.config,self.db,self.trader,worker)
        self.processor.event=AsyncMock();self.processor.process=AsyncMock()
        self.connected=True;self.now=0;self.rows=[];self.read_error=None
        self.client=Mock();self.client.is_connected.side_effect=lambda:self.connected
        self.client.disconnected=asyncio.get_running_loop().create_future()
        async def connect():self.connected=True
        async def disconnect():self.connected=False
        self.client.connect=AsyncMock(side_effect=connect)
        self.client.disconnect=AsyncMock(side_effect=disconnect)
        self.client.is_user_authorized=AsyncMock(return_value=True)
        async def messages(channel,**kwargs):
            for row in self.rows:
                if row.id>kwargs['min_id']:yield row
            if self.read_error:raise self.read_error
        self.client.iter_messages=Mock(side_effect=messages)
        self.reader=TelegramReader(self.client,self.processor.event,clock=lambda:self.now)
        self.offset=100;self.db.advance(self.config.channel,self.offset)

    async def asyncTearDown(self):
        if self.reader.cleanup is not None:await self.reader.cleanup
        self.db.conn.close();self.tmp.cleanup()

    async def cycle(self):
        self.offset,healthy=await run_cycle(self.client,object(),self.offset,self.processor,self.reader)
        return healthy

    def events(self,name):return [c for c in self.processor.event.call_args_list if c.args[0]==name]

    async def test_exhausted_auto_reconnect_explicitly_connects_and_resumes_saved_offset(self):
        self.connected=False
        self.client.disconnected.set_exception(ConnectionError('Automatic reconnection exhausted'))
        self.rows=[SimpleNamespace(id=101)]
        self.assertTrue(await self.cycle())
        self.client.connect.assert_awaited_once();self.client.is_user_authorized.assert_awaited_once()
        self.assertEqual(self.client.iter_messages.call_args.kwargs,{'min_id':100,'reverse':True,'limit':100})
        self.assertEqual(self.db.offset(self.config.channel),101)
        self.assertEqual(len(self.events('TELEGRAM_RECONNECTED')),1)
        self.trader.reconcile.assert_called_once()

    async def test_healthy_connection_is_not_restarted_or_announced(self):
        self.assertTrue(await self.cycle());self.assertTrue(await self.cycle())
        self.client.connect.assert_not_awaited();self.client.disconnect.assert_not_awaited()
        self.assertFalse(self.events('TELEGRAM_RECONNECTED'));self.assertIsNotNone(self.reader.last_success)

    async def test_failed_reconnection_backs_off_but_exchange_reconciles_each_cycle(self):
        self.connected=False;self.client.connect.side_effect=ConnectionError('private diagnostic text')
        for stamp in (0,1,4,5,6,14,15):
            self.now=stamp;self.assertFalse(await self.cycle());await asyncio.sleep(0)
        self.assertEqual(self.client.connect.await_count,3)
        self.assertEqual(self.trader.reconcile.call_count,7)
        self.assertEqual([c.kwargs['retry_in_seconds'] for c in self.events('TELEGRAM_POLL_ERROR')],[5,10,20])
        self.client.iter_messages.assert_not_called();self.assertEqual(self.db.offset(self.config.channel),100)
        self.assertNotIn('private diagnostic text',str(self.processor.event.call_args_list))

    async def test_retry_delay_caps_at_sixty_seconds(self):
        for n in range(9):
            await self.reader.failed(ValueError('failed'))
            self.assertLessEqual(self.reader.retry_in,60);self.now=self.reader.retry_at
        self.assertEqual(self.events('TELEGRAM_POLL_ERROR')[-1].kwargs['retry_in_seconds'],60)

    async def test_connect_timeout_does_not_block_exchange_monitoring(self):
        async def hanging():await asyncio.Event().wait()
        self.connected=False;self.client.connect.side_effect=hanging;self.reader.connect_timeout=.01
        self.assertFalse(await asyncio.wait_for(self.cycle(),.5))
        self.trader.reconcile.assert_called_once();self.assertEqual(self.offset,100)

    async def test_history_timeout_schedules_cleanup_and_keeps_offset(self):
        async def hanging(*args,**kwargs):
            await asyncio.Event().wait()
            yield
        self.client.iter_messages.side_effect=hanging;self.reader.read_timeout=.01
        self.assertFalse(await asyncio.wait_for(self.cycle(),.5));await asyncio.sleep(0)
        self.trader.reconcile.assert_called_once();self.client.disconnect.assert_awaited_once()
        self.assertEqual(self.offset,100);self.assertGreater(self.reader.retry_in,0)

    async def test_slow_message_processing_is_never_cancelled_by_read_timeout(self):
        async def process(message):await asyncio.sleep(.03)
        self.processor.process.side_effect=process;self.rows=[SimpleNamespace(id=101)]
        self.reader.read_timeout=.01
        self.assertTrue(await self.cycle());self.assertEqual(self.db.offset(self.config.channel),101)

    async def test_disconnect_mid_batch_resumes_after_last_committed_message(self):
        self.rows=[SimpleNamespace(id=101)];self.read_error=ConnectionError('disconnected')
        self.assertFalse(await self.cycle());await asyncio.sleep(0)
        self.assertEqual(self.db.offset(self.config.channel),101)
        self.read_error=None;self.rows.append(SimpleNamespace(id=102));self.now=5
        self.assertTrue(await self.cycle())
        self.assertEqual([c.args[0].id for c in self.processor.process.call_args_list],[101,102])
        self.assertEqual(self.db.offset(self.config.channel),102)
        self.assertEqual(len(self.events('TELEGRAM_RECONNECTED')),1)

    async def test_flood_wait_is_respected_without_disconnect_or_blocking_exchange(self):
        self.read_error=FloodWaitError(None,capture=120)
        self.assertFalse(await self.cycle());self.assertEqual(self.reader.retry_in,120)
        self.now=119;self.assertFalse(await self.cycle())
        self.client.iter_messages.assert_called_once();self.client.disconnect.assert_not_called()
        self.assertEqual(self.trader.reconcile.call_count,2)
        self.read_error=None;self.now=120;self.assertTrue(await self.cycle())
        self.assertEqual(self.reader.failures,0)

    async def test_transport_connection_alone_does_not_claim_recovery(self):
        self.connected=False;self.read_error=ValueError('request still fails')
        self.assertFalse(await self.cycle());self.assertFalse(self.events('TELEGRAM_RECONNECTED'))
        self.now=5;self.assertFalse(await self.cycle());self.assertEqual(self.reader.failures,2)

    async def test_unfinished_disconnect_cannot_race_a_new_connect(self):
        gate=asyncio.Event()
        async def disconnect():
            await gate.wait();self.connected=False
        self.client.disconnect.side_effect=disconnect
        await self.reader.failed(ConnectionError('lost'));await asyncio.sleep(0);self.now=5
        try:
            self.assertFalse(await self.cycle());self.client.connect.assert_not_awaited()
            self.trader.reconcile.assert_called_once()
        finally:gate.set();await self.reader.cleanup
        self.assertTrue(await self.cycle());self.client.connect.assert_awaited_once()

    async def test_unauthorized_session_does_not_process_messages(self):
        self.connected=False;self.client.is_user_authorized.return_value=False
        self.assertFalse(await self.cycle());self.client.iter_messages.assert_not_called()
        self.trader.reconcile.assert_called_once()
        await asyncio.sleep(0);self.now=5;self.assertFalse(await self.cycle())
        self.client.iter_messages.assert_not_called()
        self.assertEqual(self.events('TELEGRAM_POLL_ERROR')[-1].kwargs['reason'],'TELEGRAM_SESSION_REQUIRES_LOGIN')

    async def test_exchange_failure_does_not_replay_processed_messages(self):
        self.rows=[SimpleNamespace(id=101)];self.trader.reconcile.side_effect=RuntimeError('exchange offline')
        self.assertFalse(await self.cycle());self.assertEqual(self.db.offset(self.config.channel),101)
        self.trader.reconcile.side_effect=None;self.assertTrue(await self.cycle())
        self.processor.process.assert_awaited_once();self.assertFalse(self.events('TELEGRAM_POLL_ERROR'))

    async def test_partial_book_after_reconnection_still_closes_existing_position(self):
        self.processor.process=MessageProcessor.process.__get__(self.processor)
        sid=self.db.signal(self.config.channel,99,'image','signal',{})
        self.db.reserve(sid,{'message_id':99})
        self.connected=False
        self.rows=[SimpleNamespace(id=101,raw_text='Partial book',photo=True,reply_to_msg_id=None,
            date=datetime.now(timezone.utc),download_media=AsyncMock())]
        self.assertTrue(await self.cycle())
        self.trader.close.assert_called_once_with('TELEGRAM_PARTIAL',None)
        self.trader.enter.assert_not_called();self.rows[0].download_media.assert_not_called()
