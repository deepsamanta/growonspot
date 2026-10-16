# Binance USD-M crypto scanner

This is a separate Binance service. It reuses the CoinDCX scanner's signal evaluation,
execution state machine, averaging, capacity rules, and profit management. It does not
read the Telegram gold channel or manage gold. Binance trades, orders, fill history,
profit locks, and balance baseline live in `binance-data/scanner.sqlite3`.

## Strategy

- Active USDT-margined crypto perpetuals only; at least 100 days of Binance futures
  candle history. Commodities, stocks, indices, premarket contracts, and gold are excluded.
- Shorts: more than 35% up over 24h, nearest confirmed weekly swing-high resistance
  from up to 1,000 completed daily candles, price within 10% below it. Rest a limit
  2% above resistance for four hours. $3 margin, 3x crossed.
- Longs: within 10% of the low in complete available Binance futures candle history,
  with the same completed four-hour consolidation or bullish reversal confirmation.
  $6 margin, 1x isolated; minimum-size rounding may use up to $6.50.
- Short profit: 75% at a 7% price drop, or the minimum valid reduction when the normal
  split does not fit; the remaining quantity receives a 20% conditional full-close TP
  after the first limit fills.
- Long profit: the minimum valid quantity at a 7% price increase; the remaining
  quantity receives a 15% conditional full-close TP after the first limit fills.
- Binance reduce-only closing orders are exempt from entry minimum notional.
  Quantity/step filters still apply. Entry budgets are never increased to force a split.
- Short averaging: strictly more than 35% above the reference entry, nearest weekly
  resistance plus 2%, initially the same quantity at 3x. Manual additions consume
  the allowance. After the first partial profit, one $3-margin averaging cycle is rearmed.
- Long averaging: strictly more than 60% below the original entry, with completed
  four-hour recovery confirmation; one $6 addition (maximum $6.50), manual adds count.
  Long partial profit does not reset the allowance. Targets follow the combined average.
- Base cap: five slots, three shorts and two longs. All Binance non-gold USDT futures
  positions and pending entries count, including manual trades. Each $50 increase
  above this service's starting USDT futures wallet balance adds one slot,
  alternating short then long. Falling balance removes extra capacity without
  force-closing positions. The CoinDCX balance and positions are independent.
- Profitable exits block new entries in that coin for the remainder of the IST day.
- No stop-loss orders are added. Symbols whose entry minimum exceeds the configured
  budget are skipped. Actual Binance minimums may differ from CoinDCX.

## Setup and account requirements

Use a Binance USD-M Futures account in **One-way Mode** and **Single-Asset Mode**.
Use USDT fee payment (disable Futures BNB fee discount) for exact USDT realized-PnL
accounting. The bot checks these conditions; it does not silently change account-wide
position mode, asset mode, or fee settings. A Binance futures API key with the
required futures trading permission is separate from your CoinDCX credentials.

The deployed VPS copy is `/home/ubuntu/growonspot-binance`; the existing CoinDCX
project remains in `/home/ubuntu/growonspot`. From the Binance project directory:

```sh
cd /home/ubuntu/growonspot-binance
test -f .env.binance || cp .env.binance.example .env.binance
chmod 600 .env.binance
nano .env.binance
```

Set `BINANCE_API_KEY` and `BINANCE_API_SECRET`, and copy your existing Telegram bot
and channel values into `BINANCE_TELEGRAM_BOT_TOKEN` and `BINANCE_TELEGRAM_CHAT_ID`.
Never put credentials in Git or command-line arguments. Keep `BINANCE_ENABLED=false`
while configuring. Missing keys run a healthy service in `awaiting_credentials` state;
this does not mean authenticated account access or live trading has been verified.

```sh
docker compose -p growonspot-binance -f docker-compose.binance.yml build
docker compose -p growonspot-binance -f docker-compose.binance.yml run --rm -T --interactive=false binance python -m binance_scanner --inspect
docker compose -p growonspot-binance -f docker-compose.binance.yml run --rm -T --interactive=false binance python -m binance_scanner --check
```

Both checks are read-only and never submit test trades. `--check` needs credentials.
Set `BINANCE_ENABLED=true` only after the account check succeeds, then start:

```sh
docker compose -p growonspot-binance -f docker-compose.binance.yml up -d --no-build binance
docker compose -p growonspot-binance -f docker-compose.binance.yml logs --tail 100 -f binance
curl -fsS http://127.0.0.1:8082/health
```

These commands affect only Binance. The CoinDCX scanner remains on port 8081 and the
gold bot on 8080. The Binance compose project mounts only `binance-data` and loads
only `.env.binance`. Existing CoinDCX credentials are not passed to its container.

To stop new Binance entries while continuing to manage existing bot positions, set
`BINANCE_ENABLED=false` and recreate this service. Do not remove the database or API
credentials while positions remain. Preserve a consistent SQLite backup, including
WAL data if copying while the service is running. The database stores terminal orders
and fills beyond Binance's query-retention window; an unrecoverable history gap causes
review instead of guessed positions or duplicate averaging.

## Exchange handling

- HMAC-SHA256 signed query parameters, server-time synchronization and bounded timeouts.
  Writes are not blindly retried. A missing order acknowledgement is queried by its
  unique client ID; unresolved outcomes keep the reservation and require review.
- Ordinary orders use `/fapi/v1/order`; resting exits are GTC, `reduceOnly=true`.
  Remaining-position targets use `/fapi/v1/algoOrder`, `TAKE_PROFIT_MARKET`,
  `closePosition=true`. No competing full-position target is left alongside the
  first sized closing limit. A long entry's profit order is installed on reconciliation
  after Binance confirms its fill; Binance does not accept CoinDCX's inline TP field.
- Native TP intents are durable before submission. A lost acknowledgement cannot
  silently create another conditional close. Manual orders are preserved.
- History is read in windows no longer than seven days, with trade-ID continuation
  for full pages, persistent coverage, and overlap deduplication by trade ID.
- Current private WebSocket URL: `wss://fstream.binance.com/private/ws/<listenKey>`.
  Account events wake REST verification; they never execute trades directly. The
  listen key is renewed every 30 minutes, with reconnect/backoff and REST polling
  retained. `BINANCE_WEBSOCKET_ENABLED=false` selects polling only.
- Telegram alerts include `exchange: BINANCE` so they can share your existing channel.

## Validation

```sh
python -m unittest discover -s tests -q
```

The Binance tests exercise signatures, request failures, order normalization,
reduce-only exits, conditional TPs, partial fills, manual additions, durable restart
behavior, history pagination/retention, market filters, and WebSocket events. Public
API inspection is separate from private account verification; live order acceptance
cannot be verified without Binance credentials and an actual authorized signal.

## Official documentation used

- [USD-M trading and conditional orders](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade)
- [Market data and exchange filters](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data)
- [Account configuration, balance and leverage brackets](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/account)
- [Filter definitions](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/common-definition)
- [Reduce-only minimum-notional exception](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/error-code#-4164-min_notional)
- [Private user streams](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/user-data-streams)
- [2026 WebSocket URL migration](https://www.binance.com/en/support/announcement/detail/ebf9b0aa9eca4ff3804eef6fb09ba32a)
