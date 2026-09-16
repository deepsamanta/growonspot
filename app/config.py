import os
from dataclasses import dataclass
from decimal import Decimal
from dotenv import load_dotenv

load_dotenv()

def boolean(name, default):
    value = os.getenv(name, default).lower()
    if value not in ('true', 'false'):
        raise ValueError(f'{name} must be true or false')
    return value == 'true'

@dataclass(frozen=True)
class Config:
    key: str = os.getenv('COINDCX_API_KEY', '')
    secret: str = os.getenv('COINDCX_API_SECRET', '')
    api_id: int = int(os.getenv('TELEGRAM_API_ID') or 0)
    api_hash: str = os.getenv('TELEGRAM_API_HASH', '')
    bot_token: str = os.getenv('TELEGRAM_BOT_TOKEN', '')
    alert_chat_id: str = os.getenv('TELEGRAM_CHAT_ID', '')
    phone: str = os.getenv('TELEGRAM_PHONE', '')
    session: str = os.getenv('TELEGRAM_SESSION_NAME', 'sessions/xau')
    channel: int = int(os.getenv('TELEGRAM_CHANNEL_ID', '-1001496382172'))
    margin: Decimal = Decimal(os.getenv('TRADE_MARGIN_USDT', '5'))
    leverage: int = int(os.getenv('LEVERAGE', '5'))
    dry: bool = boolean('DRY_RUN', 'false')
    enabled: bool = boolean('TRADING_ENABLED', 'true')
    new_signal_window: int = int(os.getenv('NEW_SIGNAL_WINDOW_SECONDS', '1200'))
    max_age: int = int(os.getenv('MAX_SIGNAL_AGE_SECONDS', '1200'))
    keywords: tuple = tuple(x.strip() for x in os.getenv('CLOSE_KEYWORDS', 'partial,booked,closed,taken').split(',') if x.strip())
    daily_trades: int = int(os.getenv('MAX_DAILY_TRADES', '10'))
    daily_loss: Decimal = Decimal(os.getenv('MAX_DAILY_LOSS_USDT') or '10')
    poll: int = int(os.getenv('POLL_SECONDS', '3'))
    database: str = os.getenv('DATABASE_PATH', 'data/bot.sqlite3')

    def validate(self, login=False):
        if bool(self.bot_token.strip()) != bool(self.alert_chat_id.strip()):
            raise ValueError('Set both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID for alerts')
        if not self.api_id or not self.api_hash:
            raise ValueError('Configure TELEGRAM_API_ID and TELEGRAM_API_HASH in .env')
        if not login and (not self.key or not self.secret):
            raise ValueError('Configure CoinDCX API credentials in .env')
        if self.channel != -1001496382172:
            raise ValueError('Only the specified Telegram channel is permitted')
        if not self.margin.is_finite() or self.margin <= 0 or self.leverage < 1:
            raise ValueError('Invalid margin/leverage')
        if self.daily_trades < 1 or not self.daily_loss.is_finite() or self.daily_loss <= 0 or self.poll < 1 or self.max_age < 1:
            raise ValueError('Invalid limits')
        if not 1 <= self.new_signal_window <= self.max_age:
            raise ValueError('NEW_SIGNAL_WINDOW_SECONDS must be between 1 and MAX_SIGNAL_AGE_SECONDS')
        if os.getenv('FIXED_QUANTITY') or os.getenv('ENTRY_MODE', 'MARKET') != 'MARKET':
            raise ValueError('Version 1 uses margin sizing and MARKET entries only')
