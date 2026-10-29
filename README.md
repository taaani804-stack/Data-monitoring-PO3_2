# ICT Power Of Three → MEXC USDT-M Futures → Telegram (SHORT-only, 1H / 4H / 1D)

A **signal-only** scanner. It ports the signal logic of the TradingView indicator *"ICT Power Of Three | Flux
Charts"* (Pine v5, © fluxchart, MPL-2.0) to Python. **v2:** the coins to scan are the **top 100 cryptocurrencies by
GLOBAL futures/perpetual 24h volume** (CoinMarketCap, aggregated over many exchanges), reduced to those with a
**verified MEXC USDT-margined crypto perpetual**. They are scanned on **1H, 4H and 1D** (each from its own
candles, MEXC data) and **SELL / SHORT alerts only** go to Telegram. Runs on GitHub Actions; no computer needed.

> It never places orders. It reads public MEXC Futures market data (no MEXC key), public-tier CoinMarketCap
> derivatives data (free API key) and calls the Telegram Bot API. Not financial advice; no signal is guaranteed.

## Global futures-volume ranking (v2)
**Provider: CoinMarketCap** (free "Basic" plan). Endpoints (documented as available on the free plan; see
`REVIEW_REPORT.md` for what was and was not verified):
* `GET /v5/exchange/derivatives/list` – derivatives exchanges + their 24h derivative volume (1 call per refresh)
* `GET /v5/exchange/derivatives/market-pairs/list/latest` – all derivative markets of ONE exchange (batch; one call
  per exchange per 1000 markets). The per-coin endpoint is deliberately not used (would need one call per coin).

**Volume metric.** For each cryptocurrency: the sum of CMC's USD-normalised `quotes[USD].volume_24h` over all its
perpetual + futures markets on the top-N (default 40) derivatives exchanges. Never mixed in: spot volume, open
interest, market cap, exchange-reported raw volumes. Each market is counted once (`market_id` de-duplication);
markets flagged `outlier_detected` or with `exclusions` are not counted; only `currency_type == "cryptocurrency"`
bases with a CMC `crypto_id` are ranked; stablecoin bases are dropped. **Coverage is partial** (top-N exchanges);
the covered share of CMC's total derivative volume is logged and cached (`coverage.fetched_volume_share_pct`).

**Selection (steps A–G).** (A–C) fetch → aggregate → rank. (D) take the top 100. (E) keep those with a MEXC
contract; in the default `strict` mode CMC itself must list that contract on MEXC as a *perpetual*, *USDT*-quoted,
*cryptocurrency* market (so `TSLA_USDT` can never pass merely because it ends in USDT). (F) MEXC-side filters:
`quoteCoin = settleCoin = USDT`, `futureType = 1` (perpetual), `state = 0`, `type = 1` (not suspended),
`typeLabel` not TradFi/stock, not hidden, no stock/forex/commodity keywords, not stablecoin/leveraged, plus a
ticker denylist as a *second* layer. (G) liquidity: MEXC 24h turnover ≥ `MIN_MEXC_TURNOVER_USDT` (1,000,000),
spread ≤ `MAX_SPREAD_PCT` (0.5 %, only if `bid1`/`ask1` exist), tick-size metadata present, one contract per asset.
If fewer than 100 qualify, fewer are scanned (never padded). `GLOBAL_FILL_TO_N=true` instead walks down the ranking
until 100 MEXC contracts are found. Anything unknown or ambiguous is excluded and logged.

**Cache and schedule.** Ranking cache `state/global_ranking.json` (persisted on the `scanner-state` branch).
* Workflow **Global Volume Ranking Refresh** runs daily (23:40 UTC). Hourly scans only *read* the cache.
* The hourly scanner rebuilds it itself only if the cache is ≥ `GLOBAL_RANKING_REFRESH_HOURS` (24) old.
* If a refresh fails: cache ≤ `GLOBAL_RANKING_MAX_AGE_HOURS` (72) old is used and **labelled STALE** in logs and
  alerts; otherwise policy `GLOBAL_FALLBACK_POLICY`: `halt` (default: scan nothing, run fails visibly) or
  `mexc_only` (MEXC-volume ranking; alerts say "not global" and show rank/volume as *unavailable*).
* Cost: ≈ 1 + 40 × ≤4 credits per refresh (cap `GLOBAL_MAX_CREDITS_PER_REFRESH`=400), ≈ 5k of the 15k monthly free credits.

**Alert fields** (all real or "unavailable"): cryptocurrency, MEXC contract, global rank, global 24h futures volume,
source, ranking timestamp, timeframe, signal/closed-candle times, entry, SL, TP1 (TP2/TP3 are `n/a`: the original PO3
defines one TP; set `TP_EXTRA_RR=1.5,2.5` to add clearly-labelled R-multiple extensions), R:R, setup evidence, data quality.

## What changed in this version
| Requirement | Implementation |
|---|---|
| USDT-M crypto futures only | `mexc_data.select_top_contracts`: `quoteCoin=settleCoin=USDT`, perpetual (`futureType=1`), `state=0`, not hidden, `type=1`; excludes stablecoin/leveraged bases and **non-crypto** (stocks/commodities/indices/FX, see below). Symbols come from the live `contract/detail` call only — nothing hard-coded. |
| Top 100 by volume | Ranked by `amount24` (24h futures turnover in USDT) from `contract/ticker`; refreshed at the start of **every** scan; works with <100 contracts; logs discovered / eligible / selected. |
| SHORT only | PO3 state machine unchanged (bullish entries still run, as in Pine, because an open trade blocks the next cycle) but only `SELL` entries are alerted; `format_signal_message` and `validate_short` refuse anything else. |
| 1H / 4H / 1D | Own MEXC intervals `Min60` / `Hour4` / `Day1` (never resampled). Only **closed** candles. |
| Lookback | New entry must be inside the last **7** closed candles (1H) or **2** (4H, 1D). The state machine still runs over ~500 candles of history for ATR(50)/accumulation context. |
| Scheduling | One hourly cron + due-detection from the exchange's candle timestamps and persisted progress (details below). |
| Duplicates | ID `PO3|MEXC|PAIR|TF|SELL|signal-candle-open-ms`, persisted; recorded only after Telegram confirms. |
| Message | New SHORT format (below); prices rounded to the contract tick size (`priceUnit`/`priceScale`). |

## Files
| File | Purpose |
|---|---|
| `po3_strategy.py` | Pine → Python port (logic unchanged; only extra context fields + new signal-id format) |
| `mexc_data.py` | MEXC Futures client, contract filtering, top-100 ranking, closed-candle filter |
| `short_signals.py` | SHORT-only gate: validation, tick rounding, reason text |
| `scanner.py` | CLI + one-timeframe scan (lookback window, duplicates, Telegram) |
| `orchestrator.py` | Due-detection and sequential 1D → 4H → 1H cycle with ~10 min gaps |
| `telegram_bot.py` | Credentials from env, SHORT message format, safe sending (secrets never logged) |
| `state_store.py` | Atomic, lock-protected, merge-on-save state (sent signals + scan progress) |
| `config.py`, `models.py` | Settings (env-overridable), data classes |
| `scripts/state_branch.sh` | Persists state on the `scanner-state` branch (git plumbing; safe mid-run) |
| `.github/workflows/scanner.yml` | Hourly schedule + manual dispatch |
| `.github/workflows/tests.yml` | Test suite on push / PR |

## Schedule (final)
* **Cron: `5 * * * *`** — every hour at :05 UTC (a single workflow `PO3 SHORT Scanner`).
* Each run asks MEXC for the latest *closed* candle of the highest-volume contract on 1D, 4H, 1H and compares
  it with the last candle whose scan **completed** (saved in the state file). A timeframe whose latest closed
  candle is newer is **due**. Therefore 4H/1D scans happen only after a new 4H/1D candle really closed, in
  **the exchange's own candle timezone** (nothing about UTC vs UTC+8 is assumed), and a late-starting cron
  run (GitHub delays) just finds the same due scans a bit later.
* Due scans run **sequentially in priority order 1D → 4H → 1H**, waiting 600 s (`SCAN_GAP_SECONDS`) between them:
  * all due (00:05 UTC, if the exchange's daily candle closes at 00:00 UTC): 1D, +10 min 4H, +10 min 1H
  * 4H boundary (e.g. 04:05, 08:05 …): 4H, +10 min 1H
  * any other hour: 1H only
* A timeframe is marked done only if ≥80% of the pairs were checked **and** every alert was delivered;
  otherwise it stays due and is retried next hour (duplicates are impossible via the registry).
* Non-overlap: workflow `concurrency` group (no cancel; one run at a time) + sequential scans in one process.
* State is pushed to the `scanner-state` branch after every timeframe scan (and once more at the end).
* Worst-case run length ≈ 2×10 min waiting + 3 scans; `timeout-minutes: 50`.

## Telegram message
```
━━━━━━━━━━━━━━━━━━━━
🔴 ICT PO3 — SHORT SIGNAL · 4H
━━━━━━━━━━━━━━━━━━━━
Pair: BTCUSDT
Market: USDT-Margined Perpetual Futures (MEXC)
Direction: 🔻 SELL / SHORT
Timeframe: ⏱ 4H
Signal Time (UTC): …      Candle Close (UTC): …
Entry / Stop Loss (SL) / Take Profit: TP1 …
Strategy: ICT Power of Three · Setup: Confirmed bearish PO3
Reason: <built from the real accumulation box and sweep values>
Risk/Reward: 1:0.86
```
**Take profit:** the original Pine strategy has exactly **one** TP (`RR × risk`, default 0.86) and one SL
(`5×ATR(50)`). Both are preserved, so the alert shows **TP1 only** — TP2/TP3 are *not* invented. Note the
original default has reward < risk (1:0.86); that is the indicator's default, not a choice made here.

## Setup

### 1. Create the Telegram bot and find your chat ID
1. In Telegram open **@BotFather** → `/newbot` → copy the **token** (treat it like a password; never paste it in chats, issues or code).
2. Send any message to your new bot (or add it to your group/channel and post once).
3. Open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser and read `message.chat.id`
   (groups/channels are negative numbers, e.g. `-100123...`). For a channel the bot must be an admin.

### 2. Put the project in GitHub
Web UI: create a new repository (see the *Public vs private* note below), then **Add file → Upload files**,
drag in the project contents (keep the folder structure; make sure `.github/workflows/` and `scripts/` are included)
and **Commit changes** to `main`.

Or with git:
```bash
cd po3-mexc-scanner
git init -b main
git add .
git commit -m "Add PO3 MEXC scanner"
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

### 3. Add the repository secrets
Get a free key at <https://pro.coinmarketcap.com> (Basic plan). Repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | token from BotFather |
| `TELEGRAM_CHAT_ID` | chat id from step 1 |
| `COINMARKETCAP_API_KEY` | your CoinMarketCap API key (only needed for the global ranking) |

Without the CoinMarketCap key the default policy halts (nothing is scanned). To run without it set
`GLOBAL_RANKING_ENABLED=false` (MEXC-volume ranking, labelled as not global).

### 4. Allow the workflow to push the state branch
Repo → **Settings → Actions → General → Workflow permissions → "Read and write permissions"** → Save.
(The workflow also declares `permissions: contents: write`, but an org/repo default of read-only can override it.)

### 5. Enable and test
1. **Actions** tab → enable workflows if prompted.
2. **PO3 SHORT Scanner → Run workflow** with **telegram_test = true** → test message arrives in Telegram.
3. **Global Volume Ranking Refresh → Run workflow** (force = true). Log must show `Global ranking refreshed: N assets`.
   Then run **PO3 SHORT Scanner** with **dry_run = true**, **timeframes = all** → alerts (if any) are *printed in the log*, nothing sent, no state stored. Check the log for the `Contracts:` line and the `excluded (…)` lines.
4. Run with defaults (`timeframes = auto`) or wait for the hourly schedule.

### Verify SHORT-only and no repeats
* Telegram only ever shows `🔴 ICT PO3 — SHORT SIGNAL`; the log line `SCAN SUMMARY` shows `bullish_ignored=N` (seen, never sent).
* Run the workflow twice with `timeframes = all`: the second run logs `duplicates_suppressed=N` and sends nothing new.
  Auto runs log `… not due (latest closed candle … already scanned)`.
* State: branch `scanner-state` → `sent_signals.json` (`sent` ids end with `|SELL|<candle-ms>`, `scans` = last scanned candle per timeframe).

### Local usage
```bash
pip install -r requirements-dev.txt
python -m pytest -q
python scanner.py --refresh-ranking --force               # needs COINMARKETCAP_API_KEY; builds the cache
python scanner.py --list-contracts                        # final contracts + EVERY exclusion with its reason
python scanner.py --dry-run --timeframes all --gap-seconds 0   # alerts printed, nothing sent or stored
python scanner.py --dry-run --symbols BTC_USDT,ETH_USDT --timeframes all --gap-seconds 0  # skips the ranking
python scanner.py --inspect BTC_USDT --timeframes 4h      # every PO3 entry (both directions) in the fetched window
export TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...
python scanner.py --telegram-test
python scanner.py                                         # auto: scans whatever is due
```

## How the port maps to the Pine script
Defaults are identical to the Pine inputs: Algorithm Mode *Small Manipulation* (accumulation length 40, max accumulation
size 5×ATR, min manipulation 0.6×ATR), Breakout *Wick*, TP/SL *Dynamic*, Risk *Normal* (SL = 5×ATR), RR 0.86, ATR length 50,
accumulation expansion 0.5×ATR, Fixed-mode 0.3 % TP / 0.4 % SL. All Pine inputs are available on `PO3Config`.

State machine (kept in Pine order, including **same-bar cascading** — accumulation, breakout, manipulation and entry can all occur in one candle, because the Pine blocks are sequential `if`s):
1. *Waiting For Accumulation*: `highest(40) − lowest(40) ≤ ATR × 5` → box = high/low ± 0.5×ATR.
2. *Accumulation End*: wick (or close) outside the box → *Waiting For Manipulation*.
3. *Manipulation*: price beyond box ± 0.6×ATR. **Sweep of highs → SELL; sweep of lows → BUY.**
4. Entry at that candle's **close**; SL/TP set by ATR (Dynamic) or % (Fixed).
5. Exit simulation on later candles (TP first, then SL; if both hit in one candle **SL wins**, as in Pine). A new PO3 cycle only starts after the previous trade exited — exactly like Pine — which is why exits are simulated even though only entries are alerted.

## Pine-to-Python differences (read this)
**The port has been unit-tested against hand-computed values, but it has NOT been compared with live TradingView output.** Do not assume exact equivalence until you check it (procedure below). Known differences:

1. **History window.** TradingView runs the script on up to ~4800 bars of chart history (`maxDistanceToLastBar`); the scanner fetches the last **500** candles per symbol/timeframe. The state machine is path-dependent (it only resets after a trade exits), so the cycle in progress on the first fetched candle can differ from TradingView's, and a signal can appear/disappear until both sides resynchronise. Also, a 500-candle window slides each run.
2. **ATR seeding.** Pine's `ta.atr` is seeded over the whole chart history; here over the first 50 fetched candles. The difference decays (≈ 0.98ⁿ) and is negligible after a few hundred candles, but not zero.
3. **Data source.** Candles come from MEXC Futures public REST API. TradingView's MEXC feed should match, but candle boundaries/volume quirks or a different exchange selected on your chart will change results.
4. **Alert filter (added, not in Pine).** Pine alerts fire live on bar close. The scanner scans history each run, so it only alerts SELL entries inside the recent-candle window (1H: 7, 4H/1D: 2) that have not already hit TP/SL. This is a delivery filter; it does not change PO3 rules.
5. **Not ported:** zones/boxes/labels, the backtest dashboard, and the Pine TP-hit / SL-hit alerts (alerts are SHORT entries only). The exit logic is still simulated internally.
6. **Fixed-mode and floating point.** Same formulas as Pine, evaluated in IEEE double precision; results at exactly-equal boundaries could theoretically differ by 1 ulp.

### Verify against TradingView
```bash
python scanner.py --inspect BTC_USDT --timeframes 4h
```
prints every signal PO3 finds in the fetched window (candle time UTC, entry, SL, TP, outcome). Load the same symbol on
TradingView (MEXC feed, same timeframe, indicator defaults, chart timezone UTC) and compare the Buy/Sell labels
for recent candles. Differences near the *start* of the window are expected (difference 1); differences in the most recent days are worth investigating.

## Closed candles, lookback and look-ahead
A candle is used only if `open + interval + 5 s ≤ MEXC server time` (runner clock as fallback). The forming candle
never reaches the indicator, so nothing repaints. The strategy sees the whole fetched history; only the *search
for a newly confirmed entry* is limited to the last 7 (1H) / 2 (4H, 1D) closed candles. Entries whose TP/SL was
already reached on later closed candles are skipped. If a run is delayed so long that the signal candle falls out of
the window (4H/1D: 2 candles), that signal is skipped rather than sent late.

## Non-crypto filtering (please verify once)
MEXC lists US-stock and other TradFi perpetuals (e.g. TSLA, NVDA) next to crypto, also USDT-settled. The public
`contract/detail` payload has no documented "asset class" field, so exclusion is layered: contract `type != 1`,
`isHidden`, keywords (stock/equity/forex/commodity/ETF) in `displayNameEn`/`baseCoinName`/`conceptPlate`, and a
denylist of well-known equity/commodity/index tickers. Tokenised gold (XAUT, PAXG) is a crypto token and stays
eligible. **Every exclusion is logged with its reason**; add anything that slips through with the env var
`EXTRA_EXCLUDED_BASES=ABC,XYZ` (or a repo/workflow env entry).

## Configuration (environment variables, all optional)
`TOP_N_SYMBOLS` (100), `CANDLE_LIMIT` (500, max 1000), `CANDLE_CLOSE_BUFFER_SECONDS` (5),
`LOOKBACK_CANDLES_1H/4H/1D` (7/2/2), `SCAN_GAP_SECONDS` (600), `STATE_RETENTION_DAYS` (14), `STATE_PATH`,
`EXTRA_EXCLUDED_BASES`, `STATE_SYNC_COMMAND` (set by the workflow).

## Differences added by this version
See the list in the strategy section above (history window, ATR seeding, data source). Added by this version:
the lookback window is counted in candles (7/2/2) instead of minutes; duplicates use the v2 id (old v1 ids are still honoured).

## Known limitations / not verified
* **Not tested against live CoinMarketCap, MEXC, Telegram or GitHub Actions from the build environment** (network restricted). HTTP
  behaviour is tested against a local fake server that mimics the documented response shapes; the first real
  `dry_run` is the true integration test.
* MEXC's `Hour4`/`Day1` interval names and `priceUnit`/`priceScale`/`type` fields follow MEXC's public docs; confirm in the first dry-run log.
* Which UTC hour MEXC's **daily** candle closes is not hard-coded; the scheduler follows the candle timestamps, so the 1D scan fires whenever the exchange closes the day.
* Equivalence with TradingView is unverified.
* GitHub cron is best-effort (can be delayed/dropped); public repos pause schedules after 60 days of inactivity. The 10-minute gaps keep a job running ≈ 20+ minutes on 1D hours (free for public repos; counts against minutes on private ones).
* No TP/SL-hit notifications, no position tracking, no order execution — by design.

## v2 limitations
* CoinMarketCap free-plan access is *documented*, not live-verified here. Plan limits/field semantics (`outlier_detected`, `exclusions`) can change; the first `--refresh-ranking` run is the real check.
* Global volume is a **partial** aggregate (top-N exchanges, top markets each) – not the entire global market.
* `strict` verification needs CMC to list MEXC's derivatives; if it does not, nothing passes (use `CRYPTO_VERIFICATION=symbol`, weaker).
* CMC could label a *tokenised stock* as a cryptocurrency; MEXC `typeLabel`/keyword/denylist filters are the backstop. Inspect `--list-contracts`.
* MEXC's docs now show `/contract/detail/country`; the client tries `/contract/detail` first and falls back to it.
