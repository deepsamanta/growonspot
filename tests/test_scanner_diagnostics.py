import threading
import time
import unittest
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import Mock, patch
from scanner.capacity import Capacity
from scanner.diagnostics import blocked_shorts
from scanner.market import DAY, Quote


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.config=SimpleNamespace(enabled=True,short_distance=D('.10'))
        self.capacity=Capacity(frozenset('abcde'),frozenset('ab'),frozenset('cde'))
        self.market=Mock();self.history=Mock();self.emit=Mock()
        now=time.time();end=int(now//DAY)*DAY
        self.market.active_instruments.return_value=['B-WET_USDT']
        self.market.quotes.return_value={'B-WET_USDT':Quote('B-WET_USDT',D('.07523'),D('50'),now)}
        self.history.daily.return_value=[SimpleNamespace(timestamp=end-i*DAY) for i in range(110,0,-1)]

    def run_check(self):
        with patch('scanner.diagnostics.weekly_resistances',return_value=[D('.07611')]):
            blocked_shorts(self.config,self.capacity,self.market,self.history,self.emit,threading.Event())

    def test_capacity_signal_is_observation_only(self):
        self.run_check()
        fields=self.emit.call_args.kwargs
        self.assertEqual(fields['reason'],'SIGNAL_MATCHED_CAPACITY_BLOCKED')
        self.assertEqual(fields['execution_checks'],'NOT_RUN')
        self.assertEqual(fields['raw_limit_price'],'0.0776322')
        self.assertEqual(fields['shorts'],3)

    def test_stale_quote_is_not_called_qualified(self):
        self.market.quotes.return_value={'B-WET_USDT':Quote('B-WET_USDT',D('.07523'),D('50'),time.time()-120)}
        self.run_check()
        self.assertEqual(self.emit.call_args.kwargs['reason'],'STALE_QUOTE_NOT_EVALUATED')
        self.history.daily.assert_not_called()

    def test_no_diagnostics_when_paused_or_capacity_available(self):
        self.config.enabled=False;self.run_check();self.market.quotes.assert_not_called()
        self.config.enabled=True;self.capacity=Capacity(frozenset(),frozenset(),frozenset())
        self.run_check();self.market.quotes.assert_not_called()

    def test_far_from_resistance_is_not_slot_rejection(self):
        self.market.quotes.return_value={'B-WET_USDT':Quote('B-WET_USDT',D('.05'),D('50'),time.time())}
        self.run_check()
        self.assertEqual(self.emit.call_args.kwargs['reason'],'OUTSIDE_RESISTANCE_RANGE')

    def test_data_failure_is_unknown(self):
        self.history.daily.side_effect=ValueError('bad data')
        self.run_check()
        self.assertEqual(self.emit.call_args.kwargs['reason'],'DATA_ERROR_NOT_EVALUATED')
