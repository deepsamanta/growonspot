import contextlib
import io
import json
import unittest
from unittest.mock import patch
from scanner.alerts import Alerts


class DiagnosticAlertTests(unittest.TestCase):
    def test_blocked_signal_has_ist_log_and_telegram_with_dedup(self):
        with patch('scanner.alerts.threading.Thread'):
            alerts=Alerts('unused','unused')
        output=io.StringIO()
        with patch('scanner.alerts.time.time',return_value=0),contextlib.redirect_stdout(output):
            for _ in range(2):
                alerts.emit('SHORT_CAPACITY_DIAGNOSTIC',pair='B-WET_USDT',
                            reason='SIGNAL_MATCHED_CAPACITY_BLOCKED',observed_at=0)
        row=json.loads(output.getvalue())
        self.assertEqual(row['time_ist'],'1970-01-01 05:30:00 IST')
        self.assertEqual(row['observed_at_ist'],row['time_ist'])
        self.assertEqual(alerts.pending.qsize(),1)
        text=alerts.pending.get_nowait()
        self.assertIn('blocked: no available trade slot',text)
        self.assertIn('observed_at_ist: 1970-01-01 05:30:00 IST',text)

    def test_nonqualifying_diagnostic_stays_log_only(self):
        with patch('scanner.alerts.threading.Thread'):
            alerts=Alerts('unused','unused')
        with contextlib.redirect_stdout(io.StringIO()):
            alerts.emit('SHORT_CAPACITY_DIAGNOSTIC',reason='OUTSIDE_RESISTANCE_RANGE')
        self.assertTrue(alerts.pending.empty())
