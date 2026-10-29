"""Steps D-G: turn the global ranking + MEXC contract data into the final list of contracts to scan.

A contract is scanned only if ALL of these hold:
  1. MEXC side (mexc_data.filter_mexc_contracts): enabled, USDT-margined AND USDT-settled, perpetual (not delivery),
     not suspended/hidden/TradFi/stock-labelled, not a stablecoin/leveraged token, has price + 24h turnover.
  2. Crypto identity (global provider): the contract is mapped to a CoinMarketCap *cryptocurrency* (crypto_id) that is
     inside the global top-N by futures volume. In "strict" mode the mapping comes from CMC's own list of MEXC
     derivative markets (category perpetual, quote USDT) - so a stock perpetual such as TSLA_USDT, which CMC does not
     list as a cryptocurrency market on MEXC, can never pass merely because its symbol ends in USDT.
  3. Liquidity / metadata: MEXC 24h turnover >= MIN_MEXC_TURNOVER_USDT, spread <= MAX_SPREAD_PCT (when bid/ask exist),
     tick-size metadata present.
Anything unknown or ambiguous is excluded and logged with the reason.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from config import ScannerConfig
from global_volume import (CmcClient, GlobalRanking, GlobalVolumeError, PROVIDER, RankingCache, build_global_ranking)
from mexc_data import MexcApiError, filter_mexc_contracts
from models import ContractInfo, SelectionStats

log = logging.getLogger("selection")


class GlobalRankingUnavailable(Exception):
    """No valid global ranking and the fallback policy is 'halt'."""


@dataclass
class Resolution:
    contracts: List[ContractInfo]
    stats: SelectionStats
    ranking: Optional[GlobalRanking] = None
    mode: str = "mexc-only"  # global-fresh | global-cached | global-stale-cache | mexc-only
    report: Dict[str, Any] = field(default_factory=dict)


def _add(excluded: Dict[str, List[str]], reason: str, sym: str) -> None:
    excluded.setdefault(reason, []).append(sym)


def select_global_contracts(
    ranking: GlobalRanking,
    mexc_contracts: List[ContractInfo],
    cfg: ScannerConfig,
    note: str = "",
) -> "tuple[List[ContractInfo], Dict[str, List[str]], Dict[str, Any]]":
    """Steps D-G. Returns (final contracts ordered by global rank, exclusions, report counters)."""
    excluded: Dict[str, List[str]] = {}
    assets = ranking.assets
    by_id = {a["crypto_id"]: a for a in assets}
    top = assets[: cfg.top_n]
    top_ids = {a["crypto_id"] for a in top}
    pool_ids = set(by_id) if cfg.global_fill_to_n else top_ids

    ids_by_symbol: Dict[str, List[int]] = {}
    for a in assets:
        ids_by_symbol.setdefault(a["symbol"], []).append(a["crypto_id"])

    verified: List[tuple] = []  # (asset, ContractInfo) before liquidity checks
    for ci in mexc_contracts:
        asset = None
        if cfg.crypto_verification == "strict":
            listing = ranking.mexc_markets.get(ci.symbol)
            if not listing:
                _add(excluded, "asset not verifiable: contract absent from CoinMarketCap's MEXC derivatives listing", ci.symbol)
                continue
            if listing.get("category") != "perpetual" or listing.get("quote") != "USDT":
                _add(excluded, "CoinMarketCap lists it on MEXC as non-perpetual / non-USDT", ci.symbol)
                continue
            asset = by_id.get(listing["crypto_id"])
            if asset is None:
                _add(excluded, "CoinMarketCap crypto not in the global futures-volume ranking", ci.symbol)
                continue
        else:  # "symbol": weaker - base symbol must map to exactly one ranked cryptocurrency
            ids = ids_by_symbol.get(ci.base, [])
            if len(ids) == 0:
                _add(excluded, "base symbol not in the global futures-volume ranking", ci.symbol)
                continue
            if len(ids) > 1:
                _add(excluded, "ambiguous base symbol (several CMC cryptocurrencies share it)", ci.symbol)
                continue
            asset = by_id[ids[0]]
        if asset["crypto_id"] not in pool_ids:
            _add(excluded, f"not in global top {cfg.top_n} by futures volume", ci.symbol)
            continue
        verified.append((asset, ci))

    verified.sort(key=lambda t: t[0]["rank"])
    mexc_verified = len(verified)

    final: List[ContractInfo] = []
    used_ids = set()
    for asset, ci in verified:
        if asset["crypto_id"] in used_ids:
            _add(excluded, "second MEXC contract for an already selected asset", ci.symbol)
            continue
        if ci.price_unit <= 0 and ci.price_scale is None:
            _add(excluded, "missing tick-size metadata (priceUnit/priceScale)", ci.symbol)
            continue
        if ci.turnover_24h < cfg.min_mexc_turnover_usdt:
            _add(excluded, f"MEXC 24h turnover below {cfg.min_mexc_turnover_usdt:,.0f} USDT", ci.symbol)
            continue
        if ci.spread_pct is not None and ci.spread_pct > cfg.max_spread_pct:
            _add(excluded, f"MEXC spread above {cfg.max_spread_pct}%", ci.symbol)
            continue
        used_ids.add(asset["crypto_id"])
        final.append(replace(
            ci, crypto_id=asset["crypto_id"], global_rank=asset["rank"], global_volume_usd=asset["volume_usd"],
            global_source=f"{PROVIDER} derivatives, {ranking.coverage.get('exchanges_fetched', '?')} exchanges",
            ranking_ts=ranking.generated_at, ranking_note=note,
        ))
        if cfg.global_fill_to_n and len(final) >= cfg.top_n:
            break
    final = final[: cfg.top_n]
    report = {
        "global_assets_received": ranking.assets_received,
        "global_assets_valid": len(assets),
        "ranking_timestamp": ranking.generated_at,
        "top_candidates": len(top),
        "mexc_verified": mexc_verified,
        "final": len(final),
    }
    return final, excluded, report


class ContractResolver:
    """Combines cache handling, refresh policy, fallback policy and selection. One instance per process."""

    def __init__(self, mexc_client, cfg: ScannerConfig, cache: Optional[RankingCache] = None,
                 cmc_factory: Optional[Callable[[], CmcClient]] = None,
                 now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.mexc, self.cfg = mexc_client, cfg
        self.cache = cache or RankingCache(cfg.ranking_cache_path)
        self.cmc_factory, self.now_fn = cmc_factory, now_fn

    # --- ranking lifecycle ------------------------------------------------------------------
    def _refresh(self) -> GlobalRanking:
        if self.cmc_factory is None:
            raise GlobalVolumeError("COINMARKETCAP_API_KEY is not set")
        client = self.cmc_factory()
        ranking = build_global_ranking(client, self.cfg.global_max_exchanges, self.cfg.global_pairs_per_exchange,
                                       self.cfg.global_categories, self.now_fn())
        self.cache.save(ranking)
        log.info("Global ranking refreshed: %d assets (%d CMC calls, %d credits); exchange volume coverage %s%%",
                 ranking.assets_received, getattr(client, "calls", 0), getattr(client, "credits_used", 0), ranking.coverage.get("fetched_volume_share_pct"))
        return ranking

    def refresh_if_due(self, force: bool = False, slack_hours: float = 0.0) -> str:
        """Used by the scheduled refresh workflow. Returns "refreshed" | "fresh" (cache still recent)."""
        cached = self.cache.load()
        if not force and cached is not None:
            age = cached.age_hours(self.now_fn())
            if age < self.cfg.global_refresh_interval_hours - slack_hours:
                log.info("Global ranking is %.1fh old (< %.1fh) - no refresh needed", age, self.cfg.global_refresh_interval_hours - slack_hours)
                return "fresh"
        self._refresh()
        return "refreshed"

    def obtain_ranking(self) -> "tuple[Optional[GlobalRanking], str, str]":
        """Return (ranking | None, mode, human note). None => caller applies the fallback policy."""
        if not self.cfg.global_enabled:
            return None, "mexc-only", "global ranking disabled (MEXC-only ranking)"
        now = self.now_fn()
        cached = self.cache.load()
        age = cached.age_hours(now) if cached else None
        if cached is not None and age < self.cfg.global_refresh_interval_hours:
            return cached, "global-cached", f"latest successful ranking, {age:.1f}h old"
        try:
            return self._refresh(), "global-fresh", "refreshed this run"
        except GlobalVolumeError as exc:
            log.error("Global ranking refresh FAILED: %s", exc)
        if cached is not None and age <= self.cfg.global_max_age_hours:
            log.warning("Using STALE cached global ranking (%.1fh old; refresh interval %.0fh, max age %.0fh)",
                        age, self.cfg.global_refresh_interval_hours, self.cfg.global_max_age_hours)
            return cached, "global-stale-cache", f"STALE cache {age:.1f}h old - refresh failed"
        if cached is not None:
            log.error("Cached global ranking is %.1fh old, over the %.0fh maximum - not used", age, self.cfg.global_max_age_hours)
        return None, "unavailable", "no valid global ranking"

    # --- main entry ---------------------------------------------------------------------------
    def resolve(self) -> Resolution:
        mexc_info, tickers = self.mexc.get_exchange_info(), self.mexc.get_tickers_24h()
        mexc_contracts, stats = filter_mexc_contracts(mexc_info, tickers, self.cfg.extra_excluded_bases)
        ranking, mode, note = self.obtain_ranking()

        if ranking is None:
            if mode != "mexc-only" and self.cfg.global_fallback_policy == "halt":
                raise GlobalRankingUnavailable(
                    "No valid global futures-volume ranking (refresh failed and cache missing/too old). "
                    "Policy GLOBAL_FALLBACK_POLICY=halt: nothing is scanned. Set COINMARKETCAP_API_KEY, or use "
                    "GLOBAL_FALLBACK_POLICY=mexc_only / GLOBAL_RANKING_ENABLED=false for a (weaker) MEXC-only ranking.")
            note = "MEXC-only ranking (global ranking unavailable)" if mode != "mexc-only" else note
            selected = [replace(c, ranking_note=note, global_source="MEXC futures only (not global)")
                        for c in mexc_contracts if c.turnover_24h >= self.cfg.min_mexc_turnover_usdt
                        and (c.spread_pct is None or c.spread_pct <= self.cfg.max_spread_pct)][: self.cfg.top_n]
            stats.selected = len(selected)
            return Resolution(selected, stats, None, "mexc-only", {
                "ranking_timestamp": None, "mexc_verified": 0, "final": len(selected), "note": note})

        final, excluded, report = select_global_contracts(ranking, mexc_contracts, self.cfg, note)
        for reason, syms in excluded.items():
            stats.excluded.setdefault(reason, []).extend(syms)
        stats.selected = len(final)
        report["mode"] = mode
        return Resolution(final, stats, ranking, mode, report)
