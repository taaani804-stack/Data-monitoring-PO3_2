#!/usr/bin/env python3
"""MEXC USDT-M ICT Power Of Three SHORT-only signal scanner -> Telegram.  SIGNAL ONLY: never places orders."""
from __future__ import annotations

import argparse
import logging
import math
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, List, Optional, Protocol, Sequence

from config import ScannerConfig
from models import Candle, ConfigError, ContractInfo, Signal
from mexc_data import MexcApiError, MexcClient, drop_unclosed_candles, timeframe_ms
from po3_strategy import PO3Config, STRATEGY_NAME, legacy_signal_id, run_po3, signal_id
from short_signals import SignalRejected, validate_short
from state_store import SentSignalStore
from telegram_bot import TelegramSender, format_signal_message, load_credentials

log = logging.getLogger("scanner")


class Sender(Protocol):
    def send_text(self, text: str) -> bool: ...


class PrintSender:
    """Used by --dry-run: prints instead of sending."""

    def send_text(self, text: str) -> bool:
        print("---- DRY RUN (not sent) ----")
        print(text)
        return True


@dataclass
class ScanSummary:
    timeframe: str = ""
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    discovered: int = 0  # contracts returned by the exchange
    eligible: int = 0  # eligible USDT-M crypto perpetuals with usable volume
    selected: int = 0  # top-N chosen for this scan
    symbols: int = 0
    checked: int = 0  # symbols whose candles were successfully evaluated
    fetch_errors: int = 0
    skipped_data: int = 0  # too few / stale / invalid candle data
    internal_errors: int = 0
    setups_found: int = 0  # bearish PO3 entries inside the lookback window
    bullish_ignored: int = 0  # bullish PO3 entries in the window - never alerted
    skipped_resolved: int = 0
    skipped_stale: int = 0  # entries older than the lookback window
    rejected_invalid: int = 0
    valid_short: int = 0
    duplicates: int = 0
    sent: int = 0
    send_failures: int = 0
    reference_candle_ms: Optional[int] = None
    notes: List[str] = field(default_factory=list)

    def is_complete(self, min_ratio: float) -> bool:
        """A scan counts as done only if enough symbols were checked and every alert was delivered."""
        if self.symbols == 0 or self.send_failures:
            return False
        return self.checked >= math.ceil(self.symbols * min_ratio)

    def log_block(self) -> None:
        fmt = lambda d: d.strftime("%Y-%m-%d %H:%M:%S UTC") if d else "n/a"
        log.info(
            "SCAN SUMMARY [%s] start=%s end=%s | contracts discovered=%d eligible=%d selected=%d | "
            "checked=%d skipped_data=%d fetch_errors=%d internal_errors=%d | bearish_setups=%d bullish_ignored=%d "
            "resolved=%d stale=%d rejected_invalid=%d valid_short=%d | duplicates_suppressed=%d "
            "telegram_sent=%d telegram_failures=%d",
            self.timeframe.upper(), fmt(self.started_at), fmt(self.finished_at), self.discovered, self.eligible,
            self.selected, self.checked, self.skipped_data, self.fetch_errors, self.internal_errors,
            self.setups_found, self.bullish_ignored, self.skipped_resolved, self.skipped_stale,
            self.rejected_invalid, self.valid_short, self.duplicates, self.sent, self.send_failures,
        )


def find_short_candidates(
    closed: Sequence[Candle],
    lookback: int,
    strat_cfg: PO3Config,
    summary: Optional[ScanSummary] = None,
) -> List[Signal]:
    """Run PO3 over ALL closed candles, keep bearish (SELL) entries inside the recent window.

    The whole history is needed for context (ATR(50), 40-candle accumulation, a PO3 cycle only restarts
    after the previous trade exits), but a NEW signal must sit within the last ``lookback`` closed candles
    (1h: 7, 4h: 2, 1d: 2). Bullish entries are processed by the state machine (as in Pine) but never alerted.
    Entries whose TP/SL was already reached on later closed candles are skipped (stale trade ideas).
    """
    n = len(closed)
    window_start = n - lookback
    out: List[Signal] = []
    for s in run_po3(list(closed), strat_cfg):
        if s.entry_index < window_start:
            if summary and s.direction == "SELL":
                summary.skipped_stale += 1
            continue
        if s.direction != "SELL":
            if summary:
                summary.bullish_ignored += 1
            continue
        if summary:
            summary.setups_found += 1
        if s.exit_index is not None:
            if summary:
                summary.skipped_resolved += 1
            continue
        out.append(s)
    return out


def as_contracts(symbols: Sequence) -> List[ContractInfo]:
    """Accept ContractInfo objects or bare symbol strings (CLI --symbols; precision then unknown)."""
    return [s if isinstance(s, ContractInfo) else ContractInfo(s, s.split("_")[0], 0.0) for s in symbols]


def run_scan(
    client,
    sender: Sender,
    store: SentSignalStore,
    cfg: ScannerConfig,
    strat_cfg: PO3Config,
    contracts: Sequence,
    now_ms: int,
    timeframe: str,
    dry_run: bool = False,
    sleep: Callable[[float], None] = time.sleep,
) -> ScanSummary:
    """Scan ONE timeframe over the given contracts. Candles for this timeframe only (own MEXC interval)."""
    contracts = as_contracts(contracts)
    summary = ScanSummary(timeframe=timeframe, started_at=datetime.now(timezone.utc),
                          symbols=len(contracts), selected=len(contracts))
    tf_ms = timeframe_ms(timeframe)
    buffer_ms = cfg.close_buffer_seconds * 1000
    lookback = cfg.lookback_candles[timeframe]
    last_alert_sent = False

    for ci in contracts:
        symbol = ci.symbol
        try:
            candles = client.get_klines(symbol, timeframe, cfg.candle_limit)
        except (MexcApiError, ValueError) as exc:
            summary.fetch_errors += 1
            log.warning("%s %s: fetch failed (%s)", symbol, timeframe, exc)
            continue
        finally:
            if cfg.request_delay_seconds:
                sleep(cfg.request_delay_seconds)
        try:
            closed = drop_unclosed_candles(candles, tf_ms, now_ms, buffer_ms)
            if len(closed) < strat_cfg.min_candles + 10:
                summary.skipped_data += 1
                log.info("%s %s: only %d closed candles - skipped", symbol, timeframe, len(closed))
                continue
            if closed[-1].open_time_ms + tf_ms < now_ms - 2 * tf_ms:  # halted / stale feed
                summary.skipped_data += 1
                log.info("%s %s: latest closed candle is stale - skipped", symbol, timeframe)
                continue
            summary.checked += 1
            if summary.reference_candle_ms is None or closed[-1].open_time_ms > summary.reference_candle_ms:
                summary.reference_candle_ms = closed[-1].open_time_ms

            for sig in find_short_candidates(closed, lookback, strat_cfg, summary):  # oldest first
                try:
                    sig = validate_short(sig, ci)
                except SignalRejected as exc:
                    summary.rejected_invalid += 1
                    log.warning("%s %s: signal rejected: %s", symbol, timeframe, exc)
                    continue
                summary.valid_short += 1
                sid = signal_id(symbol, timeframe, sig.direction, sig.candle_open_ms)
                old = legacy_signal_id(symbol, timeframe, sig.direction, sig.candle_open_ms)
                if sid in store or old in store:
                    summary.duplicates += 1
                    continue
                text = format_signal_message(sig, symbol, timeframe, tf_ms, int(time.time() * 1000), ci, cfg.tp_extra_rr)
                if last_alert_sent:
                    sleep(15)  # pacing: avoid bursts when several pairs signal together
                if sender.send_text(text):
                    summary.sent += 1
                    last_alert_sent = True
                    if not dry_run:  # ONLY after Telegram confirmed delivery
                        store.add(sid)
                        store.save()
                else:
                    summary.send_failures += 1  # not recorded -> retried while still inside the window
        except Exception as exc:  # one bad symbol must never abort the whole scan
            summary.internal_errors += 1
            log.exception("%s %s: unexpected error (%s) - symbol skipped", symbol, timeframe, type(exc).__name__)

    if not dry_run:
        store.prune()
        store.save()
    summary.finished_at = datetime.now(timezone.utc)
    return summary


def inspect(client, symbol: str, timeframe: str, cfg: ScannerConfig, strat_cfg: PO3Config, now_ms: int) -> int:
    """Print every signal PO3 finds in the fetched window (both directions), for comparing against TradingView."""
    tf_ms = timeframe_ms(timeframe)
    candles = drop_unclosed_candles(client.get_klines(symbol, timeframe, cfg.candle_limit), tf_ms, now_ms, cfg.close_buffer_seconds * 1000)
    signals = run_po3(candles, strat_cfg)
    print(f"{symbol} {timeframe}: {len(candles)} closed candles, first={_fmt(candles[0].open_time_ms) if candles else 'n/a'}, {len(signals)} signal(s)")
    for s in signals:
        outcome = f"{s.exit_reason}@{s.exit_price:.8g} (candle {_fmt(candles[s.exit_index].open_time_ms)})" if s.exit_index is not None else "open"
        print(f"  {s.direction:4} candle={_fmt(s.candle_open_ms)} entry={s.entry_price:.8g} SL={s.stop_loss:.8g} TP={s.take_profit:.8g} -> {outcome}")
    return 0


def _fmt(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _now_ms(client) -> int:
    try:
        return client.get_server_time_ms()
    except Exception as exc:  # fall back to the runner clock
        log.warning("Server time unavailable (%s) - using local clock", type(exc).__name__)
        return int(time.time() * 1000)


def normalize_symbol(symbol: str) -> str:
    """Normalize common BTCUSDT input to the MEXC Futures symbol BTC_USDT."""
    symbol = symbol.strip().upper()
    if symbol.endswith("_USDT"):
        return symbol
    if symbol.endswith("USDT"):
        return symbol[:-4] + "_USDT"
    return symbol


def _build_resolver(client, cfg: ScannerConfig):
    """Resolver = cache + refresh policy + fallback policy. The CMC key is read from the environment only."""
    import os
    from global_volume import CmcClient, RankingCache
    from selection import ContractResolver

    key = (os.environ.get("COINMARKETCAP_API_KEY") or "").strip()
    factory = (lambda: CmcClient(key, max_credits=cfg.global_max_credits, min_interval_seconds=cfg.cmc_min_interval_seconds)) if key else None
    return ContractResolver(client, cfg, RankingCache(cfg.ranking_cache_path), factory)


def _refresh_ranking(resolver, cfg: ScannerConfig, force: bool) -> int:
    from global_volume import GlobalVolumeError

    # The scheduled workflow runs once a day, a little BEFORE the interval is up, so it uses a slack
    # (otherwise a refresh that happened 23h50m ago would be skipped and the cache would age past a day).
    try:
        outcome = resolver.refresh_if_due(force=force, slack_hours=3.0)
    except GlobalVolumeError as exc:
        print(f"Global ranking refresh FAILED: {exc}", file=sys.stderr)
        return 1
    print(f"Global ranking: {outcome}")
    return 0


def _list_contracts(resolver) -> int:
    from selection import GlobalRankingUnavailable

    try:
        res = resolver.resolve()
    except (GlobalRankingUnavailable, MexcApiError) as exc:
        print(f"Cannot build the contract list: {exc}", file=sys.stderr)
        return 1
    print(f"mode={res.mode} ranking_ts={res.report.get('ranking_timestamp')} final={len(res.contracts)}")
    for c in res.contracts:
        vol = f"{c.global_volume_usd:,.0f}" if c.global_volume_usd else "n/a"
        spread = f"{c.spread_pct:.3f}%" if c.spread_pct is not None else "n/a"
        print(f"  #{c.global_rank if c.global_rank else '-':>4} {c.symbol:<16} global24h=${vol:>18} mexc24h=${c.turnover_24h:>16,.0f} spread={spread}")
    print("EXCLUDED:")
    for reason, syms in sorted(res.stats.excluded.items()):
        print(f"  [{len(syms)}] {reason}: {', '.join(syms[:40])}{' ...' if len(syms) > 40 else ''}")
    return 0


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="MEXC USDT-M ICT Power Of Three SHORT-only signal scanner (signal-only).")
    p.add_argument("--dry-run", action="store_true", help="print signals instead of sending; state is not modified; Telegram secrets not required")
    p.add_argument("--telegram-test", action="store_true", help="send one test message to verify TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID, then exit")
    p.add_argument("--symbols", help="comma-separated Futures symbols (e.g. BTC_USDT,ETH_USDT) instead of the automatic top-N")
    p.add_argument("--timeframes", help="FORCE these timeframes (comma-separated from 1d,4h,1h or 'all') instead of auto due-detection")
    p.add_argument("--gap-seconds", type=int, help="pause between consecutive timeframe scans (default 600, env SCAN_GAP_SECONDS)")
    p.add_argument("--refresh-ranking", action="store_true", help="only (re)build the global futures-volume ranking cache (no scan, no Telegram); skipped if the cache is recent unless --force")
    p.add_argument("--force", action="store_true", help="with --refresh-ranking: refresh even if the cache is recent")
    p.add_argument("--list-contracts", action="store_true", help="print the final contracts (global rank, volumes) and every exclusion with its reason; no scan, no Telegram")
    p.add_argument("--inspect", metavar="SYMBOL", help="list all PO3 signals in the fetched window for SYMBOL (no alerts)")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    from orchestrator import parse_forced_timeframes, run_cycle  # local import: orchestrator imports this module

    args = parse_args(argv)
    if args.telegram_test and (args.dry_run or args.inspect):
        print("--telegram-test cannot be combined with --dry-run or --inspect.", file=sys.stderr)
        return 2

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    try:
        cfg = ScannerConfig.from_env()
        forced = parse_forced_timeframes(args.timeframes) if args.timeframes else None
        if args.gap_seconds is not None:
            cfg = ScannerConfig(**{**cfg.__dict__, "scan_gap_seconds": max(0, args.gap_seconds)})
        needs_telegram = not (args.dry_run or args.inspect or args.refresh_ranking or args.list_contracts)
        sender: Sender = TelegramSender(load_credentials()) if needs_telegram else PrintSender()
    except (ConfigError, ValueError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    if args.telegram_test:
        ok = sender.send_text(f"✅ {STRATEGY_NAME}: Telegram configuration works. SHORT-only alert scanner, no orders are placed.")
        print("Test message sent." if ok else "Test message FAILED (check token / chat id; see log above).")
        return 0 if ok else 1

    strat_cfg = PO3Config()
    client = MexcClient()
    resolver = _build_resolver(client, cfg)

    if args.refresh_ranking:
        return _refresh_ranking(resolver, cfg, args.force)
    if args.list_contracts:
        return _list_contracts(resolver)

    if args.inspect:
        tf = (forced or ["1h"])[0]
        return inspect(client, normalize_symbol(args.inspect), tf, cfg, strat_cfg, _now_ms(client))

    store = SentSignalStore(cfg.state_path, cfg.retention_days)
    symbols = [normalize_symbol(s) for s in args.symbols.split(",") if s.strip()] if args.symbols else None
    result = run_cycle(client, sender, store, cfg, strat_cfg, forced=forced, symbols_override=symbols,
                       dry_run=args.dry_run, resolver=resolver)
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
