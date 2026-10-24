import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from dotenv import load_dotenv
from scanner.config import ScannerConfig

load_dotenv('.env.binance', override=True)


@dataclass(frozen=True)
class BinanceConfig(ScannerConfig):
    ath_short_enabled: bool = False  # CoinDCX-only entry extension.
    key: str = os.getenv('BINANCE_API_KEY', '')
    secret: str = os.getenv('BINANCE_API_SECRET', '')
    bot_token: str = os.getenv('BINANCE_TELEGRAM_BOT_TOKEN') or os.getenv('TELEGRAM_BOT_TOKEN', '')
    chat_id: str = os.getenv('BINANCE_TELEGRAM_CHAT_ID') or os.getenv('TELEGRAM_CHAT_ID', '')
    enabled: bool = os.getenv('BINANCE_ENABLED', 'false').lower() == 'true'
    database: str = os.getenv('BINANCE_DATABASE', 'binance-data/scanner.sqlite3')
    timezone: str = os.getenv('BINANCE_DAY_TIMEZONE', 'Asia/Kolkata')
    long_margin_cap: Decimal = Decimal(os.getenv('BINANCE_LONG_MARGIN_CAP', '6.50'))
    scan_interval: int = int(os.getenv('BINANCE_SCAN_SECONDS', '300'))
    short_distance: Decimal = Decimal('.10')
    short_limit_seconds: int = 14400
    health_port: int = int(os.getenv('BINANCE_HEALTH_PORT', '8082'))
    websocket_enabled: bool = os.getenv('BINANCE_WEBSOCKET_ENABLED', 'true').lower() == 'true'

    def validate(self):
        for name in ('BINANCE_ENABLED', 'BINANCE_WEBSOCKET_ENABLED'):
            if os.getenv(name, 'false' if name == 'BINANCE_ENABLED' else 'true').lower() not in ('true', 'false'):
                raise ValueError(name+' must be true or false')
        if bool(self.key) != bool(self.secret):
            raise ValueError('Both Binance API key and secret must be supplied')
        if self.enabled and (not self.key or not self.bot_token or not self.chat_id):
            raise ValueError('Live Binance trading requires Binance API and Telegram credentials')
        if not self.long_margin_cap.is_finite() or not Decimal('6') <= self.long_margin_cap <= Decimal('6.50'):
            raise ValueError('Long margin cap must be from 6 to 6.50 USDT')
        if self.scan_interval < 60 or not 1024 <= self.health_port <= 65535:
            raise ValueError('Invalid Binance scan interval or health port')
        if Path(self.database).resolve() in (Path('scanner-data/scanner.sqlite3').resolve(), Path('data/bot.sqlite3').resolve()):
            raise ValueError('Binance must use its own database')
