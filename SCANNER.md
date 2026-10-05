# Independent CoinDCX market scanner

This service adds a USDT futures scanner without changing the Telegram gold bot. It uses its own process, Docker Compose project, SQLite database and health port. It excludes XAU, never adopts pre-existing positions, and reuses the existing `.env` CoinDCX and Telegram credentials. There is no Google Sheets dependency.

| Entry | Conditions | Margin and leverage | Margin mode | Take profit |
|---|---|---|---|---|
| Short | More than 35% gain in 24 hours; price within 10% below the nearest confirmed weekly resistance; resting sell limit 2% above that resistance | $3 margin, 3×, sized at the limit price | Crossed | 7% price decline from actual fill |
| Long | Executable market price at or below 110% of the available-history all-time low, with completed 4h consolidation or bullish reversal | $6 margin, 1× | Isolated | 6% price increase from actual fill |

No stop-loss is submitted by this scanner. Gold retains its existing settings. Five non-gold positions maximum across the account: at most three shorts and two longs. This includes external positions (including DOGE), pending exchange orders, and durable scanner submissions whose outcome is uncertain. Gold is excluded. Positions and reservations for the same pair are counted once. The scanner checks account capacity before discovery/candle analysis and again before entry submission. If full, over either side limit, or the account read fails, no new entries are submitted. One scanner position per coin; externally owned positions are counted but never modified. Existing positions above the new limits remain open and block further entries until capacity becomes available. A coin with a realized profit transaction is blocked until the next midnight in Asia/Kolkata, with this lock stored across restarts. Unknown exit PnL blocks that coin until reconciliation succeeds.

Both strategies require at least 100 days of available CoinDCX futures history. The scanner fetches up to 1,000 completed daily candles for weekly resistance, groups them into complete Monday–Sunday UTC weeks, and uses swing highs with two strictly lower completed weeks on each side. It selects the nearest resistance strictly above price. Incomplete weeks and gaps cannot confirm resistance. When price is between 90% and 100% of that resistance (excluding the resistance itself), an eligible short uses a resting `limit_order` at **resistance × 1.02**, rounded upward to a valid exchange tick. For example, resistance at 100 places the short limit at 102. The 10% eligibility band is still measured from the original resistance. Current last/mark and bid are checked before submission so the order is intended to rest above market. Exchange price bounds are also checked; an invalid level is skipped, never replaced by a market entry.

An unfilled short limit reserves a short slot and remains at its original price for up to four hours. CoinDCX accepts it as `good_till_cancel`; the running bot enforces the deadline, persists it across restarts, and cancels overdue orders on recovery. The slot remains reserved until the exchange confirms cancellation. A fresh scan may reassess and submit another eligible order after confirmed cancellation. A partial fill triggers cancellation of the remaining entry quantity, then the bot manages the filled amount. Cancellation retries are bounded, with uncertainty retaining the slot and generating an alert. If external activity pushes account capacity over the limits, pending scanner limits are cancelled.

Short TP is attached to the position after the fill is confirmed, based on the actual average fill price. If the 7% target is already executable, the bot requests a profit exit once. It does not submit a premature TP trigger while the entry is still unfilled. The scanner normally checks order status about every five seconds, subject to API response time. It must be running to enforce cancellation deadlines and attach TP to new fills.

A filled scanner short has **one averaging allowance per position**, stored in SQLite across restarts. When the executable bid is strictly more than 30% above its first actual fill, the bot can place one further sell limit at the nearest confirmed weekly resistance × 1.02. The same 10% proximity band, completed daily/weekly history rules and four-hour expiry apply. This addition uses the **exact quantity filled by the first entry**, the same 3× leverage and crossed mode. It is not resized to a $3 margin budget: at a higher price, equal quantity needs more margin. The 35% 24-hour pump requirement is only for the initial entry. Combined position size must fit the exchange leverage tier. The addition counts as the existing coin's position, so it is allowed at five occupied pairs if all total/side limits are still satisfied; an account above those limits blocks it.

The bot reads same-side pending orders, filled orders and the position before adding. A manual pending addition, verified manual fill, or unexplained quantity increase consumes that position's averaging allowance. It cancels only its own outstanding averaging order when manual activity is detected, never the manual order. Filled manual entries must link to the same position in CoinDCX's transaction ledger and explain the quantity and average entry before TP is updated. The first entry remains separately recorded; combined short TP is 7% below the actual weighted-average entry. A manual addition of 17 units at 0.68 after 17 units at 0.5264 gives an average of 0.6032 and a raw TP of 0.560976, rounded down to the instrument's tick. Partial closing, reversal, changes of position ID or unexplained balances require review instead of being adopted automatically.

Once claimed, the allowance remains consumed even if the averaging attempt is rejected, expires, or has an ambiguous acknowledgement. Observing a manual pending addition also consumes it even if that manual order is later cancelled. A fully closed position and a subsequent distinct initial entry get a fresh allowance. Partial fills cancel the averaging remainder and use the actual combined average. The bot cancels an outstanding addition when the base position closes and waits for cancellation confirmation before releasing its reservation. A profit exit requested by the bot likewise waits for its averaging cancellation. Exchange-native TP and a pending addition are separate orders: a near-simultaneous TP/manual fill/addition can race between API reads. Unexpected quantities after such a race are frozen for review and alerted; no replacement averaging order is sent. Monitoring and cancellation require the scanner to be running and the API to respond.

Long eligibility uses the complete available CoinDCX futures history, paginated back to before the exchange existed and cached in SQLite. This is the futures contract's available-history low, not a global spot-market ATL. Missing daily candles block eligibility. The current 24-hour low is included so a fresh low is not ignored. The cache advances as new daily candles complete.

ATL proximity alone never opens a long. Six completed four-hour candles must either consolidate inside a 4% high/low range with no fresh low in the final two bars, or the latest bar must be bullish, close above the previous three highs, and hold a higher low than the previous bar. Current price must still hold the confirmation lows. Four-hour bars are assembled from complete sets of documented one-hour API candles; missing, stale and forming bars cannot confirm entry. Confirmation is rechecked before submitting. Existing scanner longs are reconciled to the new 6% TP; there is no forced portfolio rebalance or automatic loss exit.

Quantities normally round down. Short quantity and estimated $3 margin are computed from the limit price 2% above resistance, not the lower current market price. If a $6 long is below the exchange's minimum, it rounds up only to the smallest valid quantity, provided its estimated margin is no more than $6.50. Larger exchange minimums are skipped. Minimum notional is checked using the lower of the executable, last and fresh mark prices; the margin cap uses the higher price. Sizing is refreshed after preparation, immediately before submission. This avoids sizing solely from an ask price that is above the exchange mark/reference price. Short sizing stays within $3 estimated margin. Market fills can differ from the sizing quote; fees and funding are separate. TP prices round outward to a valid tick and are verified against the filled position.

Order intent is committed before submission. A timeout does not trigger another entry order. Uncertain orders retain a slot and generate an alert for review. Definite exchange rejections retain a sanitized error message and apply a durable 15-minute per-coin cooldown instead of resubmitting on every scan. Market prices can still move before exchange validation, so no implementation can promise zero future API rejections. Position quantity, direction, margin mode and leverage are checked before subsequent position mutations. Manual same-side additions to tracked shorts are reconciled as described below; unexplained position changes remain blocked for review. Do not delete the state database while positions remain open.

## Deploy on the VPS

Keep the gold service running. These commands only operate on the separate scanner project. If the VPS checkout contains unrelated local changes, preserve them before updating Git; do not reset or overwrite the gold code.

```sh
cd /home/ubuntu/growonspot
# Update the checkout once any existing local changes have been preserved.
git pull --ff-only
# Create only on first setup; retain existing scanner settings on later deploys.
test -f .env.scanner || cp .env.scanner.example .env.scanner
chmod 600 .env.scanner
mkdir -p scanner-data
docker compose -p growonspot-scanner -f docker-compose.scanner.yml build scanner
# Authenticated reads only: no orders and no Telegram test messages.
docker compose -p growonspot-scanner -f docker-compose.scanner.yml run --rm scanner python -m scanner --check
# Enables live scanning with SCANNER_ENABLED=true from .env.scanner.
docker compose -p growonspot-scanner -f docker-compose.scanner.yml up -d scanner
curl -fsS http://127.0.0.1:8081/health
docker compose -p growonspot-scanner -f docker-compose.scanner.yml logs --tail=100 -f scanner
```

Discovery is configured for every five minutes (`SCANNER_SCAN_SECONDS=300`). A resting limit can execute at the exchange between scans. `SCANNER_SHORT_DISTANCE=0.10` sets the maximum distance below resistance, and `SCANNER_SHORT_LIMIT_SECONDS=14400` sets the four-hour cancellation deadline.

The first full scan downloads and caches history; it can take several minutes. Subsequent scans reuse completed daily history. Health reports discovery count, scan progress, completion timestamp and occupied scanner slots plus total account longs/shorts. A capacity pause is a healthy idle state, not a process failure. An uncertain exchange outcome makes health return `review_required`; a read failure returns `degraded`. Docker health status does not itself restart a container. The restart policy recovers exited processes, while SQLite and the process lock prevent duplicate active instances sharing the same state.

Set `SCANNER_ENABLED=false` in `.env.scanner` and recreate **only this service** to pause entries while retaining reconciliation and TP management. Stopping the service stops monitoring and alerts; exchange TP orders already accepted remain at the exchange. Resting entry limits also remain at the exchange until cancelled, and can fill while the bot is stopped. To stop trading entirely, cancel pending scanner entry orders as well; do not assume that stopping Docker cancels orders.

Telegram alerts use `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`: manual averaging detection, averaging submission/fills/cancellation/errors, resting short limits, cancellation requests/confirmation, confirmed fills, order errors/uncertainty, TP attachment issues and confirmed exits. Failed alert delivery is logged separately. `.env`, `.env.scanner` and `scanner-data/` are excluded from Git and the Docker build context.

## Verify locally without orders

```sh
.venv/bin/python -m unittest discover -s tests -q
.venv/bin/python -m scanner --inspect
```

The automated suite uses mocked exchange writes. `--inspect` uses public data only; `--check` also validates signed read endpoints. Neither submits trades. API integration follows the [CoinDCX official API reference](https://docs.coindcx.com/).
