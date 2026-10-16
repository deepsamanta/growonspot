import argparse
import json
from .config import BinanceConfig
from .client import Client
from .market import MarketData


def main():
    parser=argparse.ArgumentParser(description='Independent Binance USD-M crypto scanner')
    group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--inspect',action='store_true',help='Public Binance market data only')
    group.add_argument('--check',action='store_true',help='Read-only Binance account and configuration check')
    group.add_argument('--run',action='store_true',help='Run live strategy when BINANCE_ENABLED=true')
    args=parser.parse_args();c=BinanceConfig();c.validate()
    if args.run:
        from .runner import run
        run(c);return
    client=Client(c.key,c.secret);market=MarketData(client)
    instruments=market.active_instruments();quotes=market.quotes()
    result=dict(exchange='BINANCE',active_usdt_crypto_perpetuals=len(instruments),
                fresh_quotes=len(set(instruments)&set(quotes)),trading_enabled=c.enabled)
    if args.check:
        if not c.key:raise SystemExit('Set BINANCE_API_KEY and BINANCE_API_SECRET in .env.binance first')
        from scanner.state import ScannerState
        from scanner.capacity import account_capacity
        from .exchange import Exchange
        db=ScannerState(c.database,c.timezone)
        try:
            ex=Exchange(c,market,db);ex.account_mode()
            burn=client.call('GET','/fapi/v1/feeBurn')
            if burn.get('feeBurn') is not False:raise SystemExit('Disable Futures BNB fee payment for exact USDT PnL accounting')
            result.update(authenticated_read='ok',wallet_usdt=str(ex.wallet_balance()),
                          capacity=account_capacity(ex.all_positions(),db.occupied()).report())
        finally:db.conn.close()
    print(json.dumps(result,indent=2))


if __name__=='__main__':main()
