"""Explicit new-trade announcements and non-entry update states."""
import re

UPDATE = re.compile(
    r'\b(?:partial|book(?:ed|ing)?|closed|taken|done|completed|finished|profitable|results?|updates?|again|made|'
    r'already|holding|running|breakeven|trailing)\b|'
    r'\b(?:tp|sl|target|stop\s*loss|take\s*profit)\s+(?:hit|reached|achieved)\b|'
    r'\b(?:no|not|nothing)\s+(?:(?:a|any|more)\s+)?new\b', re.I)


def is_update_text(text):
    return bool(UPDATE.search(text or ''))


def is_new_announcement(text):
    text = text or ''
    promotional = re.search(r'\b(?:instagram|youtube|subscribe|webinar|giveaway|course|channel|group)\b', text, re.I)
    return bool(re.search(r'\bnew\b', text, re.I)) and not is_update_text(text) and not promotional


def ticket_from_text(text):
    tickets = re.findall(r'#\s*(\d{5,})\b', text or '')
    return tickets[0] if len(set(tickets)) == 1 else None
