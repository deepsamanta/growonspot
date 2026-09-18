"""Ordered Telegram processing; exit captions take precedence over image entries."""
import hashlib
import time
from functools import partial
from pathlib import Path
from .alerts import PREFIX
from .exchange import APIError
from .signals import extract_image, should_close_from_text
from .entry_rules import is_new_announcement, is_update_text


def error_details(error):
    return error.details() if isinstance(error, APIError) else {'error_type': type(error).__name__}


class MessageProcessor:
    def __init__(self, config, db, trader, worker):
        self.c, self.db, self.trader, self.worker = config, db, trader, worker

    async def event(self, kind, **data):
        await self.worker(partial(self.db.event, kind, **data))

    async def clear_context(self, message_id):
        context = await self.worker(self.db.context, self.c.channel)
        await self.worker(self.db.save_context, self.c.channel,
                          {'consumed_through': max(message_id, context.get('consumed_through', 0))})

    async def process(self, message):
        text = message.raw_text or ''
        if text.startswith(PREFIX):
            return
        # An exit/update invalidates pending images and earlier NEW announcements.
        if should_close_from_text(text, self.c.keywords):
            await self.clear_context(message.id)
            trade = await self.worker(self.db.active)
            if trade and message.id > trade['data']['message_id']:
                message_ids = await self.worker(self.db.message_ids, trade)
                if message.reply_to_msg_id is not None and message.reply_to_msg_id not in message_ids:
                    await self.event('CLOSE_KEYWORD_IGNORED_UNRELATED_REPLY', message_id=message.id, trade_id=trade['id'])
                    return
                keyword = next(k for k in self.c.keywords if should_close_from_text(text, (k,)))
                await self.event('CLOSE_KEYWORD_DETECTED', message_id=message.id, trade_id=trade['id'],
                                 reason='TELEGRAM_' + keyword.upper())
                await self.worker(self.trader.close, 'TELEGRAM_' + keyword.upper(), message.reply_to_msg_id)
            else:
                if message.reply_to_msg_id is None:
                    await self.worker(self.db.cancel_pending)
                await self.event('CLOSE_KEYWORD_IGNORED_NO_MATCHING_TRADE', message_id=message.id)
            return
        if is_update_text(text):
            await self.clear_context(message.id)
            await self.event('UPDATE_IGNORED_NO_ENTRY', message_id=message.id)
            return
        new = is_new_announcement(text)
        if not message.photo and not new:
            return
        if not self.c.enabled:
            await self.clear_context(message.id)
            await self.event('TRADING_DISABLED', message_id=message.id)
            return
        now = time.time()
        sent_at = message.date.timestamp()
        if now - sent_at > self.c.max_age or sent_at > now + 30:
            await self.clear_context(message.id)
            await self.event('STALE_IMAGE_OR_ANNOUNCEMENT_SKIPPED', message_id=message.id)
            return
        context = await self.worker(self.db.context, self.c.channel)
        if message.id <= context.get('consumed_through', 0):
            return
        window = self.c.new_signal_window
        for key in ('marker', 'image'):
            if key in context and now - context[key]['sent_at'] > window:
                del context[key]
        if message.photo:
            path = Path(self.c.database).parent / 'images' / f'{message.id}.jpg'
            path.parent.mkdir(parents=True, exist_ok=True)
            await message.download_media(file=str(path))
            context['image'] = {'message_id': message.id, 'sent_at': sent_at,
                                'reply_to': message.reply_to_msg_id, 'path': str(path)}
        if new:
            context['marker'] = {'message_id': message.id, 'sent_at': sent_at,
                                 'reply_to': message.reply_to_msg_id}
        await self.worker(self.db.save_context, self.c.channel, context)
        if 'image' not in context or 'marker' not in context:
            await self.event('NEW_ANNOUNCEMENT_PENDING_IMAGE' if new else 'IMAGE_WAITING_FOR_NEW', message_id=message.id)
            return
        candidate, marker = context['image'], context['marker']
        same_message = marker['message_id'] == candidate['message_id']
        related = same_message or (
            (marker['reply_to'] is None or marker['reply_to'] == candidate['message_id']) and
            (candidate['reply_to'] is None or candidate['reply_to'] == marker['message_id']))
        if not related or abs(candidate['sent_at'] - marker['sent_at']) > window:
            await self.event('NEW_ANNOUNCEMENT_NOT_ASSOCIATED', message_id=message.id)
            return
        # Consume authorization durably BEFORE OCR/entry. One NEW cannot authorize
        # repeated result screenshots, including after restart or partial closure.
        await self.clear_context(max(marker['message_id'], candidate['message_id']))
        await self.enter_candidate(candidate, marker)

    async def retain_later_marker(self, candidate, marker):
        # A standalone NEW may precede the next image rather than describe an old
        # pending result. Keep it available if that older candidate was invalid.
        if marker['message_id'] > candidate['message_id']:
            context = await self.worker(self.db.context, self.c.channel)
            context['marker'] = marker
            await self.worker(self.db.save_context, self.c.channel, context)

    async def enter_candidate(self, candidate, marker):
        path = Path(candidate['path'])
        message_id = candidate['message_id']
        try:
            started = time.monotonic()
            signal, raw = await self.worker(extract_image, path)
            await self.event('OCR_COMPLETED', message_id=message_id, seconds=round(time.monotonic()-started, 2))
            if await self.worker(self.db.has_traded_ticket, self.c.channel, signal.ticket_id):
                await self.event('SIGNAL_DUPLICATE_TICKET', message_id=message_id, ticket_id=signal.ticket_id)
                await self.retain_later_marker(candidate, marker)
                return
            data = {**signal.data(), 'raw_ocr_text': raw, 'image_path': str(path),
                    'source_sent_at': candidate['sent_at'],
                    'reply_to_message_id': candidate['reply_to'], 'new_announcement_message_id': marker['message_id']}
            image_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            signal_id = await self.worker(self.db.signal, self.c.channel, message_id, image_hash, signal.digest(), data)
            if signal_id is None:
                await self.event('SIGNAL_DUPLICATE', message_id=message_id)
                await self.retain_later_marker(candidate, marker)
            elif time.time() - candidate['sent_at'] <= self.c.max_age:
                await self.worker(self.trader.enter, signal_id, message_id, signal)
            else:
                await self.event('STALE_IMAGE_SKIPPED', message_id=message_id)
        except ValueError as error:
            await self.event('SIGNAL_REJECTED', message_id=message_id, reason=str(error))
            await self.retain_later_marker(candidate, marker)


async def run_cycle(client, channel, offset, processor):
    """Reconciliation errors must never prevent Telegram offset/close processing."""
    healthy = True
    try:
        async for message in client.iter_messages(channel, min_id=offset, reverse=True, limit=100):
            try:
                await processor.process(message)
            except Exception as error:
                healthy = False
                await processor.event('MESSAGE_PROCESSING_ERROR', message_id=message.id, **error_details(error))
            # Entry and exit intents are durable before mutations. Do not replay an order
            # because notification, exchange reconciliation, or OCR failed afterwards.
            await processor.worker(processor.db.advance, processor.c.channel, message.id)
            offset = message.id
    except Exception as error:
        healthy = False
        await processor.event('TELEGRAM_POLL_ERROR', **error_details(error))
    try:
        await processor.worker(processor.trader.reconcile)
    except Exception as error:
        healthy = False
        await processor.event('RECONCILIATION_ERROR', **error_details(error))
    return offset, healthy
