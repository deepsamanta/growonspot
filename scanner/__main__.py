import argparse
import json
from .market import MarketData


def main():
    parser=argparse.ArgumentParser(description='Independent CoinDCX futures scanner')
    group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--inspect',action='store_true',help='Public discovery only; never places orders')
    group.add_argument('--check',action='store_true',help='Read-only credentials and data check')
    group.add_argument('--run',action='store_true',help='Run the configured strategy')
    args=parser.parse_args()
    if args.run:
        from .config import ScannerConfig
        from .runner import run
        run(ScannerConfig());return
    market=MarketData();instruments=market.active_instruments();quotes=market.quotes()
    result={'active_usdt_instruments':len(instruments),'fresh_quotes':len(set(instruments)&set(quotes)),
            'gold_excluded':True,'above_35_percent':[p for p in instruments if p!='B-XAU_USDT' and p in quotes and quotes[p].change_24h>35]}
    if args.check:
        from .config import ScannerConfig
        from .exchange import Exchange
        config=ScannerConfig();config.validate();ex=Exchange(config,market)
        ex.positions('B-ETH_USDT')
        ex.orders('B-ETH_USDT','BUY')
        import time
        transactions=ex.transactions(time.time()-86400)
        result['authenticated_read']='ok'
        result['transaction_rows_last_24h']=len(transactions)
        result['alerts_configured']=bool(config.bot_token and config.chat_id)
    print(json.dumps(result,indent=2))


if __name__=='__main__':main()
