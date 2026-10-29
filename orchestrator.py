"""Scheduler / orchestrator: decides which timeframe scans are DUE and runs them one at a time.

One GitHub Actions run (cron: every hour) calls :func:`run_cycle`:

1. Fetch the top-N USDT-M contracts (fresh volume ranking).
2. For each timeframe, ask the exchange for the latest CLOSED candle of the highest-volume contract.
   A timeframe is *due* when that candle is newer than the last candle whose scan completed (persisted in
   the state file).  Due-ness therefore follows the EXCHANGE's own candle boundaries/timezone and is
   immune to late-starting cron runs; nothing is hard-coded about 4h/1d boundaries.
3. Run due scans in priority order 1d -> 4h -> 1h, waiting ``scan_gap_seconds`` (default 600 s = 10 min)
   between consecutive scans.  A scan never starts while another one is running (sequential, one process).
4. After each scan the progress is saved (and ``STATE_SYNC_COMMAND`` is run in CI to push the state), but
   a timeframe is marked processed only if the scan was complete and every alert was delivered; otherwise
   it stays due and is retried at the next hourly cycle (duplicates are impossible thanks to the registry).
"""
from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, List, Optional, Sequence

from config import TIMEFRAME_PRIORITY, ScannerConfig
from mexc_data import MexcApiError, drop_unclosed_candles, timeframe_ms
from po3_strategy import PO3Config
from selection import GlobalRankingUnavailable
from scanner import ScanSummary, Sender, as_contracts, run_scan
from state_store import SentSignalStore

log = logging.getLogger("orchestrator")


@dataclass
class CycleResult:
    summaries: List[ScanSummary] = field(default_factory=list)
    due: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    exit_code: int = 0


def parse_forced_timeframes(raw: str) -> List[str]:
    """'all' or a comma list; returned in priority order (1d, 4h, 1h)."""
    items = {t.strip().lower() for t in raw.split(",") if t.strip()}
    if "all" in items:
        return list(TIMEFRAME_PRIORITY)
    for t in items:
        timeframe_ms(t)  # raises ValueError for unsupported values
    order = list(TIMEFRAME_PRIORITY) + [t for t in sorted(items) if t not in TIMEFRAME_PRIORITY]
    return [t for t in order if t in items]


def latest_closed_open_ms(client, symbol: str, timeframe: str, now_ms: int, buffer_ms: int) -> Optional[int]:
    """Open time of the newest CLOSED candle of ``symbol`` for ``timeframe`` (None if unavailable)."""
    candles = client.get_klines(symbol, timeframe, 5)
    closed = drop_unclosed_candles(candles, timeframe_ms(timeframe), now_ms, buffer_ms)
    return closed[-1].open_time_ms if closed else None


def determine_due(client, store: SentSignalStore, cfg: ScannerConfig, reference_symbol: str, now_ms: int) -> List[str]:
    due = []
    buffer_ms = cfg.close_buffer_seconds * 1000
    for tf in TIMEFRAME_PRIORITY:
        if tf not in cfg.timeframes:
            continue
        try:
            latest = latest_closed_open_ms(client, reference_symbol, tf, now_ms, buffer_ms)
        except (MexcApiError, ValueError) as exc:
            log.warning("Cannot determine latest %s candle (%s) - treating %s as due", tf, exc, tf)
            due.append(tf)
            continue
        done = store.last_scanned_candle(tf)
        if latest is None or done is None or latest > done:
            log.info("%s due: latest closed candle %s, last completed scan %s", tf.upper(), latest, done)
            due.append(tf)
        else:
            log.info("%s not due (latest closed candle %s already scanned)", tf.upper(), latest)
    return due


def log_exclusions(stats) -> None:
    for reason, syms in sorted(stats.excluded.items()):
        log.info("  excluded (%s): %d e.g. %s", reason, len(syms), ",".join(syms[:8]))


def log_resolution(res) -> None:
    """The counts required for auditing the global-ranking selection."""
    r = res.report
    if res.ranking is None:
        log.warning("Contracts: MEXC-ONLY ranking (%s) | MEXC discovered=%d eligible=%d final scanned=%d",
                    r.get("note"), res.stats.discovered, res.stats.eligible, r.get("final", 0))
    else:
        log.info("Global ranking [%s] built %s UTC | assets received=%s with valid futures volume=%s | selected top candidates=%s | "
                 "verified MEXC USDT perpetuals=%s | final contracts scanned=%s | exchange coverage %s%%",
                 res.mode, r.get("ranking_timestamp"), r.get("global_assets_received"), r.get("global_assets_valid"),
                 r.get("top_candidates"), r.get("mexc_verified"),
                 r.get("final"), res.ranking.coverage.get("fetched_volume_share_pct"))
        log.info("MEXC discovered=%d, passed MEXC-side filters=%d", res.stats.discovered, res.stats.eligible)
    total_ex = sum(len(v) for v in res.stats.excluded.values())
    log.info("Excluded contracts: %d", total_ex)
    log_exclusions(res.stats)


def _sync_state(cfg: ScannerConfig) -> None:
    if not cfg.state_sync_command:
        return
    try:
        subprocess.run(cfg.state_sync_command, shell=True, check=True, timeout=300)
    except Exception as exc:  # never lose the scan result because the push failed; final CI step retries
        log.warning("State sync command failed (%s)", type(exc).__name__)


def run_cycle(
    client,
    sender: Sender,
    store: SentSignalStore,
    cfg: ScannerConfig,
    strat_cfg: Optional[PO3Config] = None,
    forced: Optional[Sequence[str]] = None,
    symbols_override: Optional[Sequence[str]] = None,
    dry_run: bool = False,
    resolver=None,
    sleep: Callable[[float], None] = time.sleep,
    now_fn: Optional[Callable[[], int]] = None,
) -> CycleResult:
    strat_cfg = strat_cfg or PO3Config()
    now_fn = now_fn or (lambda: _server_now(client))
    result = CycleResult()
    log.info("Cycle start %s UTC (dry_run=%s)", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), dry_run)

    def load_contracts():
        if symbols_override:
            return as_contracts(list(symbols_override)), None
        if resolver is not None:
            res = resolver.resolve()  # global ranking (cache/refresh/fallback) + MEXC verification
            log_resolution(res)
            return res.contracts, res.stats
        contracts, stats = client.get_top_contracts(cfg.top_n, cfg.extra_excluded_bases)
        log.info("Contracts (MEXC-only ranking): discovered=%d eligible=%d selected=%d", stats.discovered, stats.eligible, stats.selected)
        log_exclusions(stats)
        return contracts, stats

    try:
        contracts, stats = load_contracts()
    except (MexcApiError, GlobalRankingUnavailable) as exc:
        log.error("Could not build the contract list: %s", exc)
        result.exit_code = 1
        return result
    if not contracts:
        log.error("No eligible contracts after all filters - nothing to scan.")
        result.exit_code = 1
        return result

    if forced:
        due = [t for t in forced if t in cfg.lookback_candles]
    else:
        due = determine_due(client, store, cfg, contracts[0].symbol, now_fn())
    result.due = list(due)
    if not due:
        log.info("No timeframe scan is due - nothing to do.")
        return result
    log.info("Due scans, in order: %s (gap %ds)", " -> ".join(t.upper() for t in due), cfg.scan_gap_seconds)

    for i, tf in enumerate(due):
        if i > 0:
            log.info("Waiting %ds before the %s scan", cfg.scan_gap_seconds, tf.upper())
            sleep(cfg.scan_gap_seconds)
            try:  # refresh the volume ranking for every scan
                contracts, stats = load_contracts()
            except (MexcApiError, GlobalRankingUnavailable) as exc:
                log.error("Contract refresh failed before %s scan: %s", tf.upper(), exc)
                result.failed.append(tf)
                continue
        summary = run_scan(client, sender, store, cfg, strat_cfg, contracts, now_fn(), tf, dry_run=dry_run, sleep=sleep)
        if stats is not None:
            summary.discovered, summary.eligible, summary.selected = stats.discovered, stats.eligible, stats.selected
        summary.log_block()
        result.summaries.append(summary)
        if summary.is_complete(cfg.min_success_ratio) and summary.reference_candle_ms is not None:
            if not dry_run and not symbols_override:
                store.mark_scanned(tf, summary.reference_candle_ms)
                store.save()
        else:
            log.error("%s scan INCOMPLETE (checked %d/%d, telegram failures %d) - stays due for retry",
                      tf.upper(), summary.checked, summary.symbols, summary.send_failures)
            result.failed.append(tf)
        if not dry_run:
            _sync_state(cfg)

    result.exit_code = 1 if result.failed else 0
    return result


def _server_now(client) -> int:
    try:
        return client.get_server_time_ms()
    except Exception as exc:
        log.warning("Server time unavailable (%s) - using local clock", type(exc).__name__)
        return int(time.time() * 1000)
