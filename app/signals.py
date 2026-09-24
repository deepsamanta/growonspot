import re
import hashlib
import json
from dataclasses import dataclass, asdict
from decimal import Decimal
from typing import Optional
from .entry_rules import is_update_text, ticket_from_text

NUMBER = r'\d+(?:[ \u00a0\u202f]\d{3})*(?:\.\d+)?'

@dataclass(frozen=True)
class Signal:
    symbol: str
    side: str
    entry: Decimal
    sl: Decimal
    tp: Decimal
    ticket_id: Optional[str] = None

    def data(self):
        return {k: str(v) if isinstance(v, Decimal) else v for k, v in asdict(self).items()}

    def digest(self):
        # Keep the legacy no-ticket hash format. Distinct source tickets may have
        # identical prices; ticket history separately blocks updates to old trades.
        payload = {k: v for k, v in self.data().items() if k != 'ticket_id'}
        payload['confidence'] = 100
        if self.ticket_id:
            payload['ticket_id'] = self.ticket_id
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

def number(value):
    return Decimal(re.sub(r'\s', '', value))

def geometry(side, entry, sl, tp):
    return all(x.is_finite() and x > 0 for x in (entry, sl, tp)) and (
        sl < entry < tp if side == 'BUY' else tp < entry < sl)

def parse(text):
    if is_update_text(text):
        raise ValueError('SIGNAL_REJECTED_UPDATE_IMAGE')
    text = text.upper().replace('\u00a0', ' ').replace('\u202f', ' ')
    # OCR can emit the same printed arrow twice (e.g. 4285.37→→4286.29).
    # Collapse only repeated arrow glyphs; never alter prices or infer fields.
    text = re.sub(r'([→➜⟶])(?:\s*\1)+', r'\1', text)
    symbols = re.findall(r'\bXAU[ /-]*USDT?\b', text)
    sides = re.findall(r'\b(BUY|SELL)\b', text)
    # One complete trade card only; ambiguous/multiple cards are rejected.
    if len(symbols) != 1 or len(sides) != 1:
        raise ValueError('SIGNAL_REJECTED_INCOMPLETE_OR_AMBIGUOUS')
    def field(pattern):
        values = re.findall(pattern, text, re.MULTILINE)
        if len(values) != 1:
            raise ValueError('SIGNAL_REJECTED_INCOMPLETE_OR_AMBIGUOUS')
        return number(values[0])
    sl = field(r'\bS\s*/?\s*L\s*:\s*(' + NUMBER + r')')
    tp = field(r'\bT\s*/?\s*P\s*:\s*(' + NUMBER + r')')
    entry = field(r'^\s*(' + NUMBER + r')\s*(?:→|➜|⟶|->|-->)\s*' + NUMBER + r'(?![\d.])')
    if not geometry(sides[0], entry, sl, tp):
        raise ValueError('SIGNAL_REJECTED_PRICE_RELATIONSHIP')
    return Signal('XAUUSDT', sides[0], entry, sl, tp, ticket_from_text(text))

def should_close_from_text(text, keywords=('partial', 'booked', 'closed', 'taken')):
    return any(re.search(r'\b' + re.escape(k) + r'\b', text, re.I) for k in keywords)

_ocr_engine = None

def extract_image(path):
    """Local Paddle-based ONNX OCR; spatial rows join separated SL/TP labels and prices."""
    global _ocr_engine
    from PIL import Image, ImageOps
    import numpy as np
    from rapidocr_onnxruntime import RapidOCR
    if _ocr_engine is None:
        _ocr_engine = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=2, text_score=0.0)
    with Image.open(path) as original:
        if original.width * original.height > 25_000_000:
            raise ValueError('IMAGE_TOO_LARGE')
        img = ImageOps.exif_transpose(original).convert('RGB')
        if img.width < 1000:
            img = img.resize((img.width * 2, img.height * 2))
        result, _ = _ocr_engine(np.array(img))
    if not result:
        raise ValueError('SIGNAL_REJECTED_INCOMPLETE')
    rows = []
    for box, text, _score in sorted(result, key=lambda item: min(p[1] for p in item[0])):
        cy = sum(p[1] for p in box) / 4
        height = max(p[1] for p in box) - min(p[1] for p in box)
        row = next((r for r in rows if abs(r['y'] - cy) < min(r['height'], height) * 0.5), None)
        if row is None:
            row = {'y': cy, 'height': height, 'words': []}
            rows.append(row)
        row['words'].append((min(p[0] for p in box), text))
    lines = []
    for row in rows:
        words = sorted(row['words'])
        line = ' '.join(w[1] for w in words)
        lines.append(line)
    text = '\n'.join(lines)
    return parse(text), text
