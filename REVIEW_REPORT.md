# REVIEW REPORT — v2: global futures-volume ranking + strict MEXC crypto-perpetual filtering

## 1. Test counts (actual runs)
| Stage | Result |
|---|---|
| Baseline: the v1 project as delivered, before any edit | **135 passed, 0 failed** |
| Final v2 project after independent audit (re-run from extracted ZIP) | **198 passed, 0 failed** (+63 tests beyond v1 baseline) |
| `python -m compileall`, YAML parse of all 3 workflows, `bash -n` on the state script | OK |

All tests are **local and deterministic** (mocked CoinMarketCap, local fake HTTP servers for MEXC/CMC/Telegram,
a real local bare git repository for the state script).

## 2. Which global API, and is it free?
**CoinMarketCap** was selected. From the official documentation page for the Derivatives endpoints, these are listed
under the **Free** plan: `/v5/exchange/derivatives/list`, `/v5/exchange/derivatives/market-pairs/list/latest`
(markets of ONE exchange, batch) and `/v5/cryptocurrency/derivatives/market-pairs/list/latest` (per coin). The pricing
page lists the Basic plan at 15,000 credits/month. The comparison table on the pricing page did not render in a way that
let me confirm per-plan checkmarks, so **free-plan access rests on the endpoint documentation only**.
**It was NOT confirmed with a live API key** (no key, and the sandbox cannot reach CoinMarketCap). CoinGecko was not
evaluated further because CoinMarketCap met the requirement; no CoinGecko code or key is included.
Because the free plan has no "all coins ranked by futures volume" endpoint, volume is **aggregated** from the top-N
exchanges' market lists (see README); the per-coin endpoint is not used.

## 3. What was built
* `global_volume.py` (new): CoinMarketCap client (header key, bounded retry/backoff, 429/Retry-After, credit guard,
  key never logged), aggregation without double counting, ranking, atomic JSON cache.
* `selection.py` (new): steps D–G, resolver with cache / refresh / max-age / fallback policy.
* `mexc_data.py`: filtering split into reusable `filter_mexc_contracts`; documented-field fixes (below); spread from `bid1/ask1`.
* `scanner.py`/`orchestrator.py`: `--refresh-ranking [--force]`, `--list-contracts`, resolver integration, required counts logged.
* `telegram_bot.py`/`short_signals.py`: new alert layout with global rank/volume/source/timestamp, evidence lines, data quality.
* Workflows: new daily `ranking-refresh.yml` (same concurrency group as the scanner); scanner passes `COINMARKETCAP_API_KEY`.
* `scripts/state_branch.sh`: now persists both state files and never drops a remote file it did not load.
* `.env.example`, README, tests. PO3 strategy logic is **unchanged** (only a context-field addition in v1).

## 4. Bugs / defects fixed
1. **`type` misdescribed**: v1 treated MEXC `type != 1` as "market-closed class"; the docs say 2 = *suspended*. Comment/reason fixed and `typeLabel` (1 = TradFi, 2 = stock) now excludes contracts.
2. **Missing metadata was accepted**: absent `futureType` defaulted to "perpetual" and absent `state` to "enabled". Now missing = rejected.
3. **Asset class was only keyword/denylist based**: now a positive identity proof from the global provider is required (strict mode); lists are only a second layer.
4. **Cache loader crashed** on a JSON file that is a list (found by a new test) — now ignored safely.
5. **State script could drop state**: saving from a run that lacked a file would have deleted it on the branch. Fixed and tested.
6. **Empty final contract list** now fails the run visibly instead of looking like a clean run.
7. MEXC docs currently show `/contract/detail/country`; the client now falls back to it if `/contract/detail` fails.

## 5. Requirement coverage (spec §13 list → tests)
Ranking, multi-exchange aggregation, duplicate-market prevention, open-interest/reported-volume exclusion (`test_global_volume.py`);
top-100 selection, fewer-than-100, fill-to-N, MEXC verification, USDT-perpetual filter, spot rejection, TSLA/NVDA stock rejection,
USDC / USD / coin-margined / delivery / option-like / unknown-asset rejection, liquidity & spread safeguards, empty list
(`test_selection.py`, `test_market_and_signals.py`); API failures, 429, 401/403, error codes, credit guard, stale/missing cache
(`test_global_volume.py`, `test_selection.py`); 1H/4H/1D scheduling, SHORT-only, entry/SL/TP/R:R, duplicates, persistence,
Telegram format (v1 suites, still passing); end-to-end over HTTP incl. "no order endpoints ever called"
and no secrets in output (`test_e2e_global.py`); workflows + state script (`test_workflows_and_state.py`).

## 6. NOT verified (be aware)
* **Live CoinMarketCap**: key validity, free-plan access to the three endpoints, real response shapes/field semantics
  (`outlier_detected`, `exclusions`, `currency_type`, `crypto_id`), credit cost, whether CMC lists MEXC derivatives.
  Field names follow the official doc examples; nothing here was run against the real API.
* **Live MEXC**: `Hour4`/`Day1` intervals, `typeLabel`/`type`/`bid1`/`ask1` presence, `/contract/detail` vs `/country`.
* **Telegram delivery** and **GitHub Actions** execution/scheduling.
* Equivalence of the PO3 port with TradingView (unchanged from v1).

## 7. Remaining limitations / decisions
* Global volume is a **partial** aggregate (top-40 exchanges, top-1000 markets each); coverage % is logged, not "the whole market".
* "Top 100 then filter to MEXC" (the literal spec order) usually yields **fewer than 100** contracts; `GLOBAL_FILL_TO_N=true` changes that.
* CMC might label a tokenised stock as a cryptocurrency; MEXC typeLabel/keyword/denylist are the backstop.
* TP2/TP3 are `n/a` (original PO3 has one TP) unless the optional `TP_EXTRA_RR` is set; those are labelled extensions.
* Default fallback is `halt`: without a valid ranking (no key / CMC down / cache > 72 h) the run fails and nothing is scanned.

## 8. First live checks (in this order)
1. Add secret `COINMARKETCAP_API_KEY`; run **Global Volume Ranking Refresh** (force = true). Expect `Global ranking refreshed: N assets`.
   A `401/403 ... plan does not include this endpoint` error means the free plan does not cover it → report back.
2. Run **PO3 SHORT Scanner** with `dry_run=true`, `timeframes=all`; read `Global ranking [...]`, `final contracts scanned=`, `excluded (...)` lines.
   Locally: `python scanner.py --list-contracts`.
3. If `absent from CoinMarketCap's MEXC derivatives listing` excludes nearly everything, CMC does not list MEXC under the expected slug → tell me, or use `CRYPTO_VERIFICATION=symbol`.
4. `telegram_test=true`, then a normal run.


## 9. Independent package audit (2026-10-29)
* Re-extracted the delivered ZIP and independently ran `python -m pytest -q`: **198 passed**.
* `python -m compileall -q .`, YAML parsing of all three workflow files, and `bash -n scripts/state_branch.sh` passed.
* Confirmed the ZIP contains no `.env`, `.git`, Python cache/bytecode, saved signal state, or global ranking cache.
* Confirmed the 15-second Telegram pacing is implemented between successful sends in each timeframe scan and added a regression test for two simultaneous SHORT candidates. The workflow runs timeframes sequentially with a default 600-second gap between timeframes.
* No live service verification was possible in this audit: CoinMarketCap API-key/plan access and real response schemas, MEXC endpoint/field compatibility, Telegram delivery, and GitHub Actions scheduling still require the user's live workflow runs.
* Required GitHub Actions secrets are exactly `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and `COINMARKETCAP_API_KEY`. No API keys or tokens are included in this package.
