import os
from dataclasses import dataclass
from decimal import Decimal
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class ScannerConfig:
    key: str = os.getenv('COINDCX_API_KEY','')
    secret: str = os.getenv('COINDCX_API_SECRET','')
    bot_token: str = os.getenv('TELEGRAM_BOT_TOKEN','')
    chat_id: str = os.getenv('TELEGRAM_CHAT_ID','')
    enabled: bool = os.getenv('SCANNER_ENABLED','false').lower()=='true'
    database: str = os.getenv('SCANNER_DATABASE','scanner-data/scanner.sqlite3')
    timezone: str = os.getenv('SCANNER_DAY_TIMEZONE','Asia/Kolkata')
    max_positions: int = 5
    short_margin: Decimal = Decimal('3')
    short_leverage: int = 3
    short_tp: Decimal = Decimal('.07')
    short_distance: Decimal = Decimal(os.getenv('SCANNER_SHORT_DISTANCE','0.10'))
    short_limit_seconds: int = int(os.getenv('SCANNER_SHORT_LIMIT_SECONDS','14400'))
    long_margin: Decimal = Decimal('6')
    long_leverage: int = 1
    long_tp: Decimal = Decimal('.06')
    long_margin_cap: Decimal = Decimal(os.getenv('SCANNER_LONG_MARGIN_CAP','6.50'))
    scan_interval: int = int(os.getenv('SCANNER_SCAN_SECONDS','300'))
    poll: int = 5
    health_port: int = int(os.getenv('SCANNER_HEALTH_PORT','8081'))

    def validate(self):
        if not self.key or not self.secret:
            raise ValueError('CoinDCX API credentials are missing')
        if not self.bot_token or not self.chat_id:
            raise ValueError('Telegram alert credentials are missing')
        if not self.long_margin_cap.is_finite() or not Decimal('6')<=self.long_margin_cap<=Decimal('6.50'):
            raise ValueError('Long margin cap must be from 6 to 6.50 USDT')
        if self.scan_interval<60:
            raise ValueError('Scan interval must be at least 60 seconds')
        if not self.short_distance.is_finite() or not Decimal('0')<self.short_distance<=Decimal('.10'):
            raise ValueError('Short distance must be above 0 and at most 0.10')
        if not 60<=self.short_limit_seconds<=14400:
            raise ValueError('Short limit lifetime must be between 60 and 14400 seconds')
        if os.getenv('SCANNER_ENABLED','false').lower() not in ('true','false'):
            raise ValueError('SCANNER_ENABLED must be true or false')
