# Independent CoinDCX market scanner

This service adds a USDT futures scanner without changing the Telegram gold bot. It uses its own process, Docker Compose project, SQLite database and health port. It excludes XAU, never adopts pre-existing positions, and reuses the existing `.env` CoinDCX and Telegram credentials. There is no Google Sheets dependency.

| Entry | Conditions | Margin and leverage | Margin mode | Take profit |
|---|---|---|---|---|
| Short | More than 35% gain in 24 hours; executable market price between 99% and 100% of the nearest confirmed weekly resistance | $3 margin, 3× | Crossed | 7% price decline from actual fill |
| Long | Executable market price at or below 110% of the available-history all-time low | $6 margin, 1× | Isolated | 10% price increase from actual fill |

No stop-loss is submitted by this scanner. Gold retains its existing settings. Five scanner positions maximum, including pending submissions and orders whose outcome is uncertain. One scanner position per coin; other services' positions are left alone. A coin with a realized profit transaction is blocked until the next midnight in Asia/Kolkata, with this lock stored across restarts. Unknown exit PnL blocks that coin until reconciliation succeeds.

Both strategies require at least 100 days of available CoinDCX futures history. The scanner fetches up to 1,000 completed daily candles for weekly resistance, groups them into complete Monday–Sunday UTC weeks, and uses swing highs with two strictly lower completed weeks on each side. It selects the nearest resistance at or above price. Incomplete weeks and gaps cannot confirm resistance. A rising coin outside the entry band is monitored, without placing a waiting limit order.

Long eligibility uses the complete available CoinDCX futures history, paginated back to before the exchange existed and cached in SQLite. This is the futures contract's available-history low, not a global spot-market ATL. Missing daily candles block eligibility. The current 24-hour low is included so a fresh low is not ignored. The cache advances as new daily candles complete.

Quantities normally round down. If a $6 long is below the exchange's minimum, it rounds up only to the smallest valid quantity, provided its estimated margin is no more than $6.50. Larger exchange minimums are skipped. Short sizing stays within $3 estimated margin. Market fills can differ from the sizing quote; fees and funding are separate. TP prices round outward to a valid tick and are verified against the filled position.

Order intent is committed before submission. A timeout does not trigger another entry order. Uncertain orders retain a slot and generate an alert for review. Position quantity, direction, margin mode and leverage are checked before subsequent position mutations. Do not manually trade a scanner-owned coin while it is active. Do not delete the state database while positions remain open.

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

The first full scan downloads and caches history; it can take several minutes. Subsequent scans reuse completed daily history. Health reports discovery count, scan progress, completion timestamp and occupied scanner slots. An uncertain exchange outcome makes health return `review_required`; a read failure returns `degraded`. Docker health status does not itself restart a container. The restart policy recovers exited processes, while SQLite and the process lock prevent duplicate active instances sharing the same state.

Set `SCANNER_ENABLED=false` in `.env.scanner` and recreate **only this service** to pause entries while retaining reconciliation and TP management. Stopping the service stops monitoring and alerts; exchange TP orders already accepted remain at the exchange.

Telegram alerts use `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`: confirmed fills, order errors/uncertainty, TP attachment issues and confirmed exits. Failed alert delivery is logged separately. `.env`, `.env.scanner` and `scanner-data/` are excluded from Git and the Docker build context.

## Verify locally without orders

```sh
.venv/bin/python -m unittest discover -s tests -q
.venv/bin/python -m scanner --inspect
```

The automated suite uses mocked exchange writes. `--inspect` uses public data only; `--check` also validates signed read endpoints. Neither submits trades. API integration follows the [CoinDCX official API reference](https://docs.coindcx.com/).
