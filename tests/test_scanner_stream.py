import hashlib
import hmac
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scanner.stream import AccountStream, client_factory, rows


class StreamTests(unittest.TestCase):
    def setUp(self):
        self.emit = Mock()
        self.stream = AccountStream('test-key', 'test-secret', self.emit)

    def order(self, **changes):
        return dict(id='entry-1', pair='B-ETH_USDT', status='filled',
                    total_quantity='1', remaining_quantity='0', **changes)

    def test_documented_envelopes_and_malformed_data(self):
        order = self.order()
        for data in ([order], {'data': [order]}, {'data': json.dumps([order])},
                     json.dumps({'data': [order]}), order):
            self.assertEqual(rows(data), [order])
        for data in (None, 'not json', 'x' * 262145, [None, 2], 5):
            self.assertEqual(rows(data), [])

    def test_filled_order_wakes_monitor_without_trading_or_logging_payload(self):
        self.stream.receive('df-order-update', {'data': [self.order()]})
        self.assertTrue(self.stream.wake.is_set())
        self.assertTrue(self.stream.snapshot()['private_event_seen'])
        self.emit.assert_not_called()

    def test_duplicate_events_do_not_create_wakeup_storm(self):
        self.stream.receive('df-order-update', [self.order()])
        self.stream.wake.clear()
        for _ in range(100):
            self.stream.receive('df-order-update', [self.order(updated_at=123)])
        self.assertFalse(self.stream.wake.is_set())
        self.assertEqual(self.stream.snapshot()['wakeups'], 1)
        self.stream.receive('df-order-update', [{**self.order(), 'remaining_quantity': '0.2'}])
        self.assertTrue(self.stream.wake.is_set())

    def test_manual_addition_wakes_but_mark_price_ticks_do_not(self):
        position = dict(id='p1', pair='B-ETH_USDT', active_pos='2', avg_price='100')
        self.stream.receive('df-position-update', [position])
        self.stream.wake.clear()
        self.stream.receive('df-position-update', [{**position, 'mark_price': 110, 'updated_at': 99}])
        self.assertFalse(self.stream.wake.is_set())
        self.stream.receive('df-position-update', [{**position, 'active_pos': '3', 'avg_price': '95'}])
        self.assertTrue(self.stream.wake.is_set())

    def test_gold_other_margin_and_incomplete_notifications_are_ignored(self):
        for row in ({**self.order(), 'pair': 'B-XAU_USDT'},
                    {**self.order(), 'margin_currency_short_name': 'INR'},
                    {**self.order(), 'pair': None}, {'id': 'missing-state', 'pair': 'B-ETH_USDT'}):
            self.stream.receive('df-order-update', [row])
        self.assertFalse(self.stream.wake.is_set())
        self.assertFalse(self.stream.snapshot()['private_event_seen'])

    def test_usdt_balance_change_requests_rest_capacity_refresh(self):
        self.stream.receive('balance-update', [{'currency_short_name': 'BTC', 'balance': '10'}])
        self.assertFalse(self.stream.wake.is_set())
        self.stream.receive('balance-update', [{'currency_short_name': 'USDT', 'balance': '60'}])
        self.assertTrue(self.stream.wake.is_set())

    def test_subscription_is_signed_and_not_claimed_authenticated_until_data_arrives(self):
        client = Mock()
        self.stream.subscribe(client)
        body = client.emit.call_args.args[1]
        self.assertEqual(body['authSignature'], hmac.new(b'test-secret', b'{"channel":"coindcx"}', hashlib.sha256).hexdigest())
        self.assertEqual(body['apiKey'], 'test-key')
        self.assertEqual(body['channelName'], 'coindcx')
        self.assertTrue(self.stream.snapshot()['subscription_requested'])
        self.assertFalse(self.stream.snapshot()['private_event_seen'])
        self.assertNotIn('test-secret', str(self.emit.call_args_list))
        self.assertNotIn('test-key', str(self.emit.call_args_list))

    def test_disconnect_retains_rest_fallback_and_reconnect_forces_refresh(self):
        self.stream.subscribe(Mock())
        self.stream.disconnected()
        self.assertEqual(self.stream.snapshot()['mode'], 'rest_polling')
        self.stream.wake.clear()
        self.stream.subscribe(Mock())
        self.assertTrue(self.stream.wake.is_set())
        self.assertEqual(self.stream.snapshot()['reconnects'], 1)

    def test_event_bursts_are_coalesced_with_one_second_minimum_wait(self):
        self.stream.wake = Mock()
        self.stream.wake.wait.return_value = True
        self.stream.stop = Mock()
        with patch('scanner.stream.time.monotonic', side_effect=[100, 100.1]):
            self.stream.wait(5)
        self.assertAlmostEqual(self.stream.stop.wait.call_args.args[0], .9)
        self.stream.wake.clear.assert_called_once()

    def test_quiet_or_disconnected_stream_still_polls_on_schedule(self):
        self.stream.wake = Mock()
        self.stream.wake.wait.return_value = False
        self.stream.stop = Mock()
        self.stream.wait(5)
        self.stream.wake.wait.assert_called_once_with(5)
        self.stream.stop.wait.assert_not_called()

    def test_disabled_stream_starts_no_connection(self):
        stream = AccountStream('', '', self.emit, enabled=False, factory=Mock())
        stream.start()
        stream.factory.assert_not_called()
        self.assertIsNone(stream.thread)
        self.assertEqual(stream.snapshot()['mode'], 'rest_polling')
        stream.close()

    def test_client_bounds_websocket_timeout_and_disables_sdk_retry_loop(self):
        socketio = SimpleNamespace(Client=Mock())
        websocket = SimpleNamespace(setdefaulttimeout=Mock())
        with patch.dict('sys.modules', socketio=socketio, websocket=websocket):
            client_factory()
        websocket.setdefaulttimeout.assert_called_once_with(60)
        self.assertFalse(socketio.Client.call_args.kwargs['reconnection'])

    def test_dedup_memory_is_bounded(self):
        for i in range(2100):
            self.stream.receive('df-order-update', [{**self.order(), 'id': str(i)}])
        self.assertLessEqual(len(self.stream.seen), 2048)

    def test_supervisor_reconnects_and_rejoins_after_exhausted_connection(self):
        stream = self.stream
        clients = []
        class Client:
            connected = False
            def __init__(self):
                self.handlers = {}; self.emitted = []
            def on(self, event, fn): self.handlers[event] = fn
            def emit(self, event, body): self.emitted.append((event, body))
            def connect(self, endpoint, transports):
                self.handlers['connect']()
                if len(clients) == 2:
                    stream.stop.set()
            def disconnect(self): self.handlers['disconnect']()
        def factory():
            client = Client(); clients.append(client); return client
        stream.factory = factory
        real_wait = stream.stop.wait
        with patch.object(stream.stop, 'wait', side_effect=lambda delay: real_wait(0)):
            stream.run()
        self.assertEqual(len(clients), 2)
        self.assertTrue(all(c.emitted[0][0] == 'join' for c in clients))
        self.assertEqual(stream.snapshot()['reconnects'], 1)
        self.assertTrue(any(call.args[0] == 'WEBSOCKET_UNAVAILABLE' for call in self.emit.call_args_list))

    def test_repeated_failures_back_off_without_exhausting_recovery_or_logging_secrets(self):
        stream = self.stream
        stream.factory = Mock(side_effect=RuntimeError('test-secret'))
        delays = []
        def wait(delay):
            delays.append(delay)
            if len(delays) == 6:stream.stop.set()
            return stream.stop.is_set()
        with patch.object(stream.stop, 'wait', side_effect=wait):
            stream.run()
        self.assertEqual(delays, [5, 10, 20, 40, 60, 60])
        self.assertEqual(stream.snapshot()['mode'], 'rest_polling')
        self.assertEqual(stream.snapshot()['last_error'], 'RuntimeError')
        self.assertNotIn('test-secret', str(self.emit.call_args_list))


if __name__ == '__main__':
    unittest.main()
