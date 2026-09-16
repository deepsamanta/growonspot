import unittest
from unittest.mock import patch
from pathlib import Path
from decimal import Decimal
from app.signals import extract_image

class ImageTests(unittest.TestCase):
    def test_zero_ocr_scores_do_not_block_complete_signal(self):
        lines = ['XAUUSD, sell 0.50', '4336.09 → 4289.31',
                 'S/L: 4338.77', 'T/P: 4289.31']
        result = [([[0, y], [400, y], [400, y + 20], [0, y + 20]], text, 0.0)
                  for y, text in zip(range(0, 160, 40), lines)]
        with patch('app.signals._ocr_engine', return_value=(result, None)):
            signal, _ = extract_image(Path(__file__).parent / 'fixtures' / 'short_signal.jpg')
        self.assertEqual(signal.entry, Decimal('4336.09'))
        self.assertEqual(signal.side, 'SELL')

    def test_exact_supplied_images(self):
        for name, side, entry, sl, tp in [
            ('short_signal.jpg', 'SELL', '4336.09', '4338.77', '4289.31'),
            ('long_signal.jpg', 'BUY', '4346.33', '4325.69', '4393.02'),
            ('incident_signal.jpg', 'SELL', '4292.13', '4307.92', '4257.24'),
        ]:
            signal, _ = extract_image(Path(__file__).parent / 'fixtures' / name)
            self.assertEqual(signal.symbol, 'XAUUSDT')
            self.assertEqual(signal.side, side)
            self.assertEqual((signal.entry, signal.sl, signal.tp), tuple(map(Decimal, (entry, sl, tp))))
