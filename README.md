# Telegram XAUUSDT trader

Docker-ready Python bot for channel `-1001496382172`, using a Telegram **user session**, local RapidOCR (Paddle models on ONNX Runtime), SQLite WAL persistence, and CoinDCX USDT futures. `.env` is the configuration file. It is already configured for **LIVE trading**, with **5 USDT margin per trade at 5× leverage** (about 25 USDT notional). Quantity rounds down; fees and market slippage are additional. Screenshot lot size is ignored. No trade is placed by installation or the Telegram login command.

## Deploy on your VPS

Docker and Docker Compose must already be installed. This project does not install Docker on your machine.

1. Copy this directory to the VPS.
2. Edit `.env`: fill in `COINDCX_API_KEY`, `COINDCX_API_SECRET`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, and `TELEGRAM_PHONE`. Get Telegram application credentials at https://my.telegram.org. The user account must already have channel access. Use a CoinDCX key with futures trading access; configure its IP restrictions for your VPS if applicable.
3. Build and authorize the Telegram session interactively:

   ```sh
   docker compose build
   docker compose run --rm trader python -m app.main --login
   ```

   Enter the Telegram login code and two-step verification password if requested. This only saves a user session; it does not trade.
4. Start live monitoring:

   ```sh
   docker compose up -d
   docker compose logs -f trader
   ```

   `DRY_RUN=false` and `TRADING_ENABLED=true` are already set. No test trade is required. On first startup the bot starts after the channel's latest post, so historical images do not trigger entries.

5. Health:

   ```sh
   curl http://127.0.0.1:8080/health
   ```

Restart after configuration changes with `docker compose up -d --force-recreate`. Stop with `docker compose stop`. Stopping the container does not close an open position; its confirmed exchange-side SL/TP remains. Set `TRADING_ENABLED=false` to stop new entries while retaining management, then recreate the container. `DRY_RUN=true` optionally simulates new trades without submitting them; existing live trades retain their live management mode.

## Behavior and safeguards

- Only Telegram photos paired with a fresh NEW announcement open positions. Text, image documents, stickers, video and other media cannot open one. Only XAUUSD/XAUUSDT parses; one complete card, direction, entry arrow, labeled SL and TP, valid price relationships are required. No signal confidence threshold or OCR recognition score threshold is applied.
- Entry is the number **before the arrow**; TP always comes from the `T/P` field. Prices allow thousands spaces. OCR text is parsed directly; missing or ambiguous required fields still reject the image.
- CoinDCX instrument metadata is discovered at startup. The pair is never assumed to exist. Missing XAU futures or invalid metadata stops startup. A $5 margin trade below exchange quantity/notional minimums is skipped, never rounded up.
- Entries use the current market price even when it differs from the image entry. No entry-price-gap filter is applied. SL/TP must still be on the correct sides of current price. Photos older than 20 minutes are skipped, including during restart catch-up.
- Fresh same-side signals add another $5 margin entry at market while retaining existing exposure. CoinDCX combines these entries into one net position; the newest signal’s SL/TP applies to the whole position. An opposite-side signal closes the whole position, waits for confirmed closure, then opens $5 in the new direction at market. Daily limits and current-price SL/TP validation apply to every entry, including the entry after a reversal. SHA256 image and normalized signal hashes plus channel/message IDs prevent duplicates persistently. Screenshot ticket IDs already associated with a bot trade cannot be traded again, even when the SL or displayed profit changes. Legacy OCR records are included in this check.
- Complete-word `partial`, `booked`, `closed`, or `taken` in a later text post or photo caption closes the full associated position. Replies may reference any source image in the combined position. Non-replies apply to the whole current bot position. Closing instructions cancel queued entries, including a pending reversal. A close received before fill is persisted until position recovery.
- The reference code's HMAC-SHA256 signing and create-order structure are preserved. Orders are market orders for this strategy. SL/TP fields are submitted with entry, then verified against exchange position triggers; missing protection is attached using the documented full-position TP/SL endpoint. Failure to confirm protection causes an emergency exit request. There is an unavoidable interval between fill and protection confirmation.
- **Keep XAUUSDT exclusive to this bot in this account.** CoinDCX nets positions and reuses a position ID per pair. Untracked XAU positions or unrelated pending orders block entries. The bot verifies added order fills before increasing its recorded combined quantity. Changed quantity/direction triggers `ISOLATION_CONFLICT` and blocks automated exit rather than closing a mixed position. Same-size external replacement cannot be distinguished reliably; do not manually trade this instrument while the bot owns it.
- Entry intent is committed before sending an order. Ambiguous requests are never resubmitted. Restart recovery reconciles the stored intent with the exchange. `UNCERTAIN`, `ISOLATION_CONFLICT`, or an unconfirmed `CLOSING` state requires reviewing CoinDCX and the database; the bot blocks new entries. Do not delete the database to clear an unresolved live trade. An exit is sent once, then reconciled; a rejected/uncertain exit needs manual intervention.
- Daily limits use UTC: 10 entry attempts and a 10 USDT **conservative loss allowance** by default. Each closed combined position charges the sum of its entries’ configured margins against that allowance, even a winner, because this version does not reconstruct realized PnL, funding and fees from account transactions. This is deliberately more restrictive than realized-PnL counting; it is not an exact daily loss report. Exit price and cause remain unknown when the exchange closes a position independently; the bot records `EXCHANGE_CLOSED` rather than inventing a TP/SL classification.

## NEW announcement required

Every entry requires the whole word **new** in a non-update trading announcement. It may be mixed with other words, such as “New buy guys”, and can be in the photo caption, in a text post before the image, or in a text post after it. `NEW_SIGNAL_WINDOW_SECONDS=1200` bounds association to 20 minutes, and `MAX_SIGNAL_AGE_SECONDS=1200` permits the image to remain pending for that same window. A plain text NEW announcement never opens a position on its own.

An unmarked photo is saved as a pending candidate; it cannot trade unless a matching NEW announcement arrives while fresh. One announcement authorizes at most one accepted image. Pending context survives restart. Only the latest pending image is considered; replies must reference the corresponding image/announcement. Updates, closing keywords and disabled trading invalidate pending context. New signals are also accepted while a bot position is open; execution waits if a prior fill or exit still needs confirmation, and expires when the source image becomes older than 20 minutes.

Captions/OCR marked partial, booked, closed, done, profitable, result, update, again/made, running, or TP/SL hit are not new entries. Negative announcements such as “no new trade” and promotional NEW posts are rejected. A changed SL on a screenshot of an already-traded ticket is an update, even if someone adds NEW to it.

## Telegram alerts

Use the same alert bot and destination as your reference bot. Add these to your VPS `.env` (existing `.env` files are not updated by `git pull`):

```dotenv
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_alert_chat_id
```

Both values are required to enable alerts; leaving both blank disables notifications and logs `TELEGRAM_ALERTS_NOT_CONFIGURED`. For a personal chat, start a conversation with the bot first. For a channel, add the bot as an administrator with permission to post. The alert destination is separate from `TELEGRAM_CHANNEL_ID`, which controls signal monitoring. Notifications use Telegram's [sendMessage API](https://core.telegram.org/bots/api#sendmessage).

Alerts cover startup, submitted entries, confirmed protected positions, requested exits, confirmed closures, skipped entries, image rejection, uncertain orders, failed exits, protection failure, isolation conflicts, loop errors, and fatal application errors. An unconfirmed exit is flagged after 30 seconds. Order submission and exit requests are explicitly distinguished from confirmed fills and closures. Fill/SL/TP details are included with position-open alerts.

Delivery runs in a separate thread, uses bounded timeouts and up to three attempts, and honors short Telegram retry-after delays. Repeated matching events are suppressed for five minutes. The in-memory queue holds 100 notifications; overflow or delivery failure is logged without interrupting trade management. Alerts are best-effort and may be lost on a crash, prolonged outage or full queue; Docker logs remain the event record. The bot cannot report a VPS/network outage while it is offline. Messages beginning `[XAU BOT]` are ignored by the signal listener to prevent its own alerts triggering exits.

After pulling updated code and filling in the two values:

```sh
docker compose up -d --build --force-recreate
docker compose logs --tail=100 trader
```

A startup notification is sent when initialization succeeds. No test trade is needed.

## Persistence and operations

`data/` stores SQLite and downloaded photos; `sessions/` stores Telegram authorization. Back up both and protect them like credentials. Run only one replica. The schema initializes automatically (`PRAGMA user_version=3`) with `signals`, `trades`, `telegram_state`, `bot_events`, `signal_context`, `entry_queue`, and `additions`; variable exchange/signal fields are held in JSON columns. A partial unique index enforces one active combined position; individual additions and queued reversal signals are recorded separately. Existing databases migrate without deleting trade history. Structured logs omit secrets; Docker logs rotate at 10 MB × 3. Photo/event retention is operator-managed.

The localhost-only health port reports Telegram connectivity, recent exchange connectivity, DB loop health, mode, active trade state and message offset. Docker health status does not automatically restart an unhealthy process; monitor `review_required` and `degraded` states. Telegram messages are processed independently of exchange reconciliation errors. Failed message processing is logged and its offset advances; durable entry/exit intents prevent duplicate mutations. Exchange read calls retry with backoff; mutating requests are never blindly retried.

## Offline checks

`tests/fixtures/short_signal.jpg` and `long_signal.jpg` are the exact supplied reference images. Parser, isolation, sizing and restart checks use no exchange credentials and place no orders:

```sh
python -m unittest discover -s tests -v
```

Only MARKET entries and margin sizing are supported in this version. SQLite is used to keep deployment to one container. LIMIT orders, fixed quantity and independent per-entry exchange SL/TP are unsupported; same-side entries share the newest SL/TP on their combined position.

API implementation reference: https://docs.coindcx.com/ (futures active instruments, instrument details, orderbook, orders, positions, update leverage, create TP/SL, and exit position). Live API compatibility and real fills require your configured deployment; no authenticated requests or real trades were used to validate this project locally.

## Validation completed

All 87 offline checks passed, including OCR on both actual supplied JPGs, acceptance of complete signals with zero OCR scores, persistent duplicates/restart state, $5 sizing, keyword matching, position isolation, pending close recovery, protection failure exits, and avoiding repeated exits. The supplied images produced exactly:

| Image | Side | Entry | SL | TP |
|---|---|---:|---:|---:|
| 1st.jpg | SELL | 4336.09 | 4338.77 | 4289.31 |
| 2nd.jpg | BUY | 4346.33 | 4325.69 | 4393.02 |

A read-only check of CoinDCX public endpoints found `B-XAU_USDT`, quantity step `0.001`, price tick `0.01`, minimum quantity `0.001`, and minimum notional `6 USDT`. The current quote sized to `0.005` XAU at the configured margin/leverage. These values are discovered again at each startup and can change. Docker was neither installed nor run locally. No test trades or authenticated exchange requests were made.

## September 15 incident fix

The running bot had the order ID but fetched more than 10,000 historical account orders before using it. Recovery hit its 100-page limit, leaving the trade marked SUBMITTED and preventing the old loop from fetching Telegram messages. Recovery now stops at the exact order ID; position queries are filtered to XAUUSDT on the exchange. API error events retain the failing endpoint and HTTP status without exposing credentials.

Close keywords are checked before image extraction, including photo captions. A caption with a close keyword cannot open a new trade. Telegram polling runs before reconciliation, and a reconciliation failure cannot suppress message processing. Earlier messages and replies to unrelated signals cannot close a later trade.

Per the updated entry instruction, market orders have no price-gap filter. `MAX_ENTRY_DEVIATION_PERCENT` is obsolete and ignored if present in an older `.env` file. Actual fill can differ from the historical entry printed in a trade screenshot.

Entry preparation errors are recorded separately from uncertain order submissions.
A failed leverage update cannot reserve a nonexistent trade indefinitely. Confirmed
terminal entry orders recover even when the position closed before the first
reconciliation. An entry with no acknowledgement remains reserved for review;
unchanged uncertainty does not generate a new event on every polling cycle.

Explicit HTTP 400/401/404/422 order rejections also release the failed entry
reservation. Timeouts, rate limits and server failures remain uncertain and
are never blindly retried. Unconfirmed-exit alerts are persisted once per exit,
including across restart, without repeating the exit request.

The September 24 10:01 signal is included as an exact-image regression fixture.
Repeated OCR arrow glyphs (such as `4285.37→→4286.29`) are normalized before
parsing; price fields are unchanged. Its NEW-caption path is checked through
the message processor with trading mocked, so no test orders are placed.
