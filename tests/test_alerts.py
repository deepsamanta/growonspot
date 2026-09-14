import contextlib
import io
import tempfile
import unittest
from unittest.mock import Mock, patch
from app.alerts import TelegramAlerts, PREFIX
from app.database import Database

class AlertTests(unittest.TestCase):
    def setUp(self):
        self.alerts = TelegramAlerts('secret-token', '-100123', start_worker=False)
    def tearDown(self):
        self.alerts.close()
    def response(self, status, payload):
        result = Mock(status_code=status)
        result.json.return_value = payload
        return result
    def test_event_sends_to_configured_chat_with_no_secret_fields(self):
        self.alerts.emit('POSITION_OPEN', trade_id=1, side='BUY', sl='4325.69', secret='never-send')
        text = self.alerts.pending.get_nowait()
        self.assertTrue(text.startswith(PREFIX))
        self.assertNotIn('never-send', text)
        self.alerts.http.post = Mock(return_value=self.response(200, {'ok': True}))
        self.assertTrue(self.alerts._deliver(text))
        args, kwargs = self.alerts.http.post.call_args
        self.assertTrue(args[0].endswith('/sendMessage'))
        self.assertEqual(kwargs['json']['chat_id'], '-100123')
        self.assertEqual(kwargs['json']['text'], text)
    def test_repeated_error_throttled_but_different_trade_delivered(self):
        self.alerts.emit('ORDER_UNCERTAIN', trade_id=1)
        self.alerts.emit('ORDER_UNCERTAIN', trade_id=1)
        self.alerts.emit('ORDER_UNCERTAIN', trade_id=2)
        self.assertEqual(self.alerts.pending.qsize(), 2)
    def test_rate_limit_retries_with_retry_after(self):
        self.alerts.http.post = Mock(side_effect=[self.response(429, {
            'ok': False, 'error_code': 429, 'parameters': {'retry_after': 7}}),
            self.response(200, {'ok': True})])
        with patch.object(self.alerts.stopping, 'wait', return_value=False) as wait:
            self.assertTrue(self.alerts._deliver('alert'))
            wait.assert_called_once_with(7)
        self.assertEqual(self.alerts.http.post.call_count, 2)
    def test_forbidden_delivery_logs_without_token_or_response_body(self):
        self.alerts.http.post = Mock(return_value=self.response(403, {
            'ok': False, 'error_code': 403, 'description': 'secret-token'}))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertFalse(self.alerts._deliver('alert'))
        self.assertIn('TELEGRAM_ALERT_FAILED', output.getvalue())
        self.assertNotIn('secret-token', output.getvalue())
        self.alerts.http.post.assert_called_once()
    def test_emit_does_not_make_http_calls(self):
        self.alerts.http.post = Mock(side_effect=AssertionError('Trading thread must not send HTTP'))
        self.alerts.emit('EXIT_REQUEST_FAILED', trade_id=1)
        self.alerts.http.post.assert_not_called()
        self.assertEqual(self.alerts.pending.qsize(), 1)
    def test_disabled_without_credentials(self):
        alerts = TelegramAlerts('', '')
        alerts.emit('BOT_STARTED')
        self.assertFalse(alerts.enabled)
        self.assertTrue(alerts.pending.empty())
        alerts.close()
    def test_database_event_survives_notification_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            broken = Mock()
            broken.emit.side_effect = RuntimeError('offline')
            db = Database(directory + '/db', broken)
            with contextlib.redirect_stdout(io.StringIO()):
                db.event('POSITION_CLOSED', trade_id=1)
            self.assertEqual(db.conn.execute('SELECT count(*) FROM bot_events').fetchone()[0], 1)
            db.conn.close()
