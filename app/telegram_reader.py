"""Recover exhausted Telegram connections without replaying trading work."""
import asyncio
import time
from telethon.errors import FloodWaitError


class TelegramLoginRequired(ConnectionError):
    pass


class TelegramReader:
    def __init__(self, client, event, clock=time.monotonic, connect_timeout=20, read_timeout=30):
        self.client, self.event, self.clock = client, event, clock
        self.connect_timeout, self.read_timeout = connect_timeout, read_timeout
        self.failures = 0
        self.retry_at = 0
        self.recovering = False
        self.cleanup = None
        self.last_success = None

    @property
    def retry_in(self):
        return max(0, self.retry_at - self.clock())

    async def failed(self, error):
        self.failures += 1
        self.recovering = True
        delay = min(60, 5 * 2 ** min(self.failures - 1, 4))
        reason = 'TELEGRAM_REQUEST_FAILED'
        if isinstance(error, FloodWaitError):
            delay = max(delay, error.seconds)
            reason = 'TELEGRAM_RATE_LIMIT'
        elif isinstance(error, (OSError, asyncio.TimeoutError)):
            reason = 'TELEGRAM_CONNECTION_LOST' if isinstance(error, ConnectionError) else 'TELEGRAM_NETWORK_TIMEOUT_OR_ERROR'
            if isinstance(error, TelegramLoginRequired):
                reason = 'TELEGRAM_SESSION_REQUIRES_LOGIN'
            # Telethon disconnect is shielded internally. Retain its future so a
            # later connect cannot race unfinished cleanup after a timed-out read.
            if self.cleanup is None:
                self.cleanup = asyncio.ensure_future(self.client.disconnect())
        self.retry_at = self.clock() + delay
        await self.event('TELEGRAM_POLL_ERROR', error_type=type(error).__name__,
                         reason=reason, retry_in_seconds=delay)

    async def ready(self):
        if self.retry_in:
            return False
        try:
            if self.cleanup is not None:
                if not self.cleanup.done():
                    return False
                cleanup, self.cleanup = self.cleanup, None
                await cleanup
            if self.client.is_connected():
                return True
            self.recovering = True
            await self.event('TELEGRAM_RECONNECTING')
            # Retrieve a failed disconnect future before Telethon replaces it.
            disconnected = self.client.disconnected
            if disconnected.done() and not disconnected.cancelled():
                disconnected.exception()
            await asyncio.wait_for(self.client.connect(), self.connect_timeout)
            if not await asyncio.wait_for(self.client.is_user_authorized(), self.connect_timeout):
                raise TelegramLoginRequired('Telegram session requires login')
            if not self.client.is_connected():
                raise ConnectionError('Telegram connection did not recover')
            return True
        except Exception as error:
            await self.failed(error)
            return False

    async def messages(self, channel, offset):
        iterator = self.client.iter_messages(channel, min_id=offset, reverse=True, limit=100).__aiter__()
        while True:
            try:
                # Bound only Telegram reads. Never cancel OCR or an exchange
                # mutation because a whole processing cycle took too long.
                message = await asyncio.wait_for(iterator.__anext__(), self.read_timeout)
            except StopAsyncIteration:
                return
            yield message

    async def succeeded(self):
        recovered = self.recovering
        self.failures = 0
        self.retry_at = 0
        self.recovering = False
        self.last_success = time.time()
        if recovered:
            await self.event('TELEGRAM_RECONNECTED')
