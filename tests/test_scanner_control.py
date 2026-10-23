import tempfile
import threading
import unittest
from unittest.mock import Mock
from scanner.control import RuntimeConfig, TelegramControl, parse_command, KEY
from scanner.config import ScannerConfig
from scanner.state import ScannerState
import test_scanner_engine


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=ScannerState(self.tmp.name+'/state.db')
        self.c=RuntimeConfig(ScannerConfig(enabled=True))
        self.alerts=Mock();self.control=TelegramControl(self.c,self.db,self.alerts)
    def tearDown(self):
        self.db.conn.close();self.tmp.cleanup()
    def command(self,ident,command):
        done=threading.Event();self.control.pending.put((ident,command,done))
        self.control.apply_pending();self.assertTrue(done.is_set())
    def test_stop_restart_start_durable_and_duplicates_ignored(self):
        self.command(5,'stop');self.assertFalse(self.c.enabled)
        self.assertIn('INACTIVE',self.alerts.reply.call_args.args[0])
        c=RuntimeConfig(ScannerConfig(enabled=True));other=TelegramControl(c,self.db,Mock())
        self.assertFalse(c.enabled);self.assertEqual(other.state['offset'],6)
        self.command(4,'start');self.assertFalse(self.c.enabled)
        self.command(6,'start');self.assertTrue(self.c.enabled)
        self.assertIn('ACTIVE',self.alerts.reply.call_args.args[0])
    def test_start_cannot_override_server_disable(self):
        self.c.base=ScannerConfig(enabled=False)
        self.command(5,'start');self.assertFalse(self.c.enabled)
        self.assertIn('disabled in server',self.alerts.reply.call_args.args[0])
    def test_status_does_not_resume(self):
        self.command(5,'stop');self.command(6,'status');self.assertFalse(self.c.enabled)
    def test_ignored_update_is_checkpointed(self):
        self.command(5,None);self.assertEqual(self.db.cache(KEY)['offset'],6)
        self.alerts.reply.assert_not_called()
    def test_exact_text_only_and_correct_destination(self):
        def msg(text,**fields):
            return {'channel_post':{'chat':{'id':-100,'type':'channel'},'date':100,'text':text,**fields}}
        for text,expected in [('stop','stop'),(' START ','start'),('/stop@MyBot','stop'),('please stop',None),('/start@other',None),('stop loss',None)]:
            self.assertEqual(parse_command(msg(text),-100,'mybot',90),expected)
        self.assertIsNone(parse_command(msg('stop'),-101,'mybot',90))
        self.assertIsNone(parse_command(msg('stop'),-100,'mybot',110))
        self.assertIsNone(parse_command(msg('stop',forward_origin={"type":"user"}),-100,'mybot',90))
        self.assertIsNone(parse_command({'edited_channel_post':msg('stop')['channel_post']},-100,'mybot',90))
    def test_channel_and_group_permissions(self):
        channel={'channel_post':{'chat':{'id':-100,'type':'channel'}}}
        self.assertTrue(self.control.authorized(channel,-100))
        self.assertFalse(self.control.authorized(channel,-101))
        group={'message':{'chat':{'id':-100,'type':'supergroup'},'from':{'id':4}}}
        self.control.api=Mock(return_value={'status':'member'})
        self.assertFalse(self.control.authorized(group,-100))
        self.control.api.return_value={'status':'administrator'}
        self.assertTrue(self.control.authorized(group,-100))
    def test_private_sender_must_match(self):
        msg={'message':{'chat':{'id':10,'type':'private'},'from':{'id':11}}}
        self.assertFalse(self.control.authorized(msg,10))
        msg['message']['from']['id']=10;self.assertTrue(self.control.authorized(msg,10))


class PausedExecutionTests(test_scanner_engine.EngineTests):
    # Run the inherited execution checks with the runtime config as well.
    def setUp(self):
        super().setUp()
        self.c=RuntimeConfig(self.c);self.e.c=self.c
        self.e.averager.c=self.c;self.e.long_averager.c=self.c
    def test_stop_prevents_new_entry(self):
        self.c.paused=True;self.e.enter(self.signal)
        self.ex.create.assert_not_called();self.assertEqual(self.db.rows(),[])
    def test_stop_cancels_only_pending_entry(self):
        trade=self.enter();d=trade['data'];self.c.paused=True
        order={'id':'order1','status':'open','total_quantity':d['quantity'],'remaining_quantity':d['quantity']}
        self.ex.find_order.return_value=order
        self.e.reconcile(trade)
        self.ex.cancel.assert_called_once_with(trade['pair'],order)
        self.assertEqual(self.db.get(trade['id'])['data']['cancel_reason'],'USER_PAUSED')
    def test_stop_keeps_profit_management(self):
        trade,position=self.filled();self.c.paused=True
        self.e.reconcile(trade)
        self.ex.take_profit.assert_called_once()
        self.ex.exit.assert_not_called()

    def test_stop_blocks_both_automatic_averaging_paths(self):
        self.c.paused=True
        trade={'data':{},'pair':'B-TEST_USDT'}
        self.e.averager.maybe_place(trade)
        self.e.long_averager.maybe_place(trade)
        self.ex.price.assert_not_called();self.ex.create.assert_not_called()
