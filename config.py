"""Scanner (not strategy) settings, overridable through environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, Mapping, Tuple

from models import ConfigError

# Scan priority when several timeframes are due in one cycle: longest first.
TIMEFRAME_PRIORITY: Tuple[str, ...] = ("1d", "4h", "1h")

# How many of the most recent CLOSED candles may contain the (new) signal candle.
#   1h: last 7 candles (recovers from delayed / missed hourly runs)
#   4h, 1d: last 2 candles (the latest completed candle + the one before it)
# This limits only where a NEWLY CONFIRMED entry is searched; the PO3 state machine still runs over
# the whole fetched history (accumulation needs 40 candles + ATR(50) context).
DEFAULT_LOOKBACK_CANDLES: Dict[str, int] = {"15m": 2, "1h": 7, "4h": 2, "1d": 2}


def _float_env(env: Mapping[str, str], name: str, default: float, minimum: float = 0.0) -> float:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"Environment variable {name} must be a number.")
    if value != value or value < minimum:
        raise ConfigError(f"Environment variable {name} must be >= {minimum}.")
    return value


def _float_list_env(env: Mapping[str, str], name: str) -> Tuple[float, ...]:
    raw = (env.get(name) or "").strip()
    if not raw:
        return ()
    try:
        vals = tuple(float(x) for x in raw.split(",") if x.strip())
    except ValueError:
        raise ConfigError(f"Environment variable {name} must be a comma-separated list of numbers.")
    if any(v != v or v <= 0 or v > 50 for v in vals) or len(vals) > 2:
        raise ConfigError(f"Environment variable {name}: at most 2 multiples, each > 0.")
    return vals


def _bool_env(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = (env.get(name) or "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"Environment variable {name} must be true or false.")


def _choice_env(env: Mapping[str, str], name: str, default: str, choices: Tuple[str, ...]) -> str:
    raw = (env.get(name) or "").strip().lower() or default
    if raw not in choices:
        raise ConfigError(f"Environment variable {name} must be one of {', '.join(choices)}.")
    return raw


def _int_env(env: Mapping[str, str], name: str, default: int, minimum: int = 0) -> int:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"Environment variable {name} must be an integer.")
    if value < minimum:
        raise ConfigError(f"Environment variable {name} must be >= {minimum}.")
    return value


@dataclass(frozen=True)
class ScannerConfig:
    top_n: int = 100
    timeframes: Tuple[str, ...] = TIMEFRAME_PRIORITY
    candle_limit: int = 500  # candles fetched per symbol/timeframe (PO3 needs >= ~60)
    close_buffer_seconds: int = 5  # safety margin after a candle's end before it counts as closed
    state_path: str = "state/sent_signals.json"
    retention_days: int = 14
    request_delay_seconds: float = 0.1  # politeness delay between kline requests
    lookback_candles: Dict[str, int] = field(default_factory=lambda: dict(DEFAULT_LOOKBACK_CANDLES))
    scan_gap_seconds: int = 600  # pause between consecutive timeframe scans in one cycle (~10 min)
    min_success_ratio: float = 0.8  # a scan only counts as complete if >= 80% of symbols were checked
    extra_excluded_bases: Tuple[str, ...] = ()
    state_sync_command: str = ""  # optional shell command run after each scan to persist state (CI)
    # --- global futures-volume ranking (CoinMarketCap). The API key is read from COINMARKETCAP_API_KEY at
    # --- runtime and deliberately NOT stored here (so it can never end up in a repr / log line).
    global_enabled: bool = True  # False = rank by MEXC volume only (not "global"; alerts say so)
    global_refresh_interval_hours: float = 24.0  # rebuild the ranking when the cache is at least this old
    global_max_age_hours: float = 72.0  # a cache older than this is never used as the ranking
    global_fallback_policy: str = "halt"  # when no valid ranking: "halt" (scan nothing) | "mexc_only"
    global_max_exchanges: int = 40  # top-N derivatives exchanges (by CMC derivative volume) aggregated
    global_pairs_per_exchange: int = 1000  # top markets per exchange (by volume) requested from CMC
    global_max_credits: int = 400  # abort a refresh that would spend more CMC credits than this
    global_categories: Tuple[str, ...] = ("perpetual", "futures")
    global_fill_to_n: bool = False  # False: top-N global assets, THEN keep those on MEXC (may be < N)
    crypto_verification: str = "strict"  # "strict": CMC must list the contract on MEXC | "symbol": symbol match
    min_mexc_turnover_usdt: float = 1_000_000.0  # minimum MEXC 24h turnover per contract
    max_spread_pct: float = 0.5  # max (ask-bid)/mid in %, only enforced when bid/ask are available
    ranking_cache_path: str = "state/global_ranking.json"
    cmc_min_interval_seconds: float = 1.3  # pause between CoinMarketCap calls (Basic plan: 50 requests/minute)
    tp_extra_rr: Tuple[float, ...] = ()  # optional extra take-profits as multiples of risk (empty = original single TP)

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "ScannerConfig":
        lookback = dict(DEFAULT_LOOKBACK_CANDLES)
        for tf in lookback:
            lookback[tf] = _int_env(env, f"LOOKBACK_CANDLES_{tf.upper()}", lookback[tf], 1)
        excluded = tuple(
            b.strip().upper() for b in (env.get("EXTRA_EXCLUDED_BASES") or "").split(",") if b.strip()
        )
        return cls(
            top_n=_int_env(env, "TOP_N_SYMBOLS", 100, 1),
            candle_limit=min(_int_env(env, "CANDLE_LIMIT", 500, 100), 1000),
            close_buffer_seconds=_int_env(env, "CANDLE_CLOSE_BUFFER_SECONDS", 5, 0),
            state_path=env.get("STATE_PATH", "state/sent_signals.json") or "state/sent_signals.json",
            retention_days=_int_env(env, "STATE_RETENTION_DAYS", 14, 1),
            lookback_candles=lookback,
            scan_gap_seconds=_int_env(env, "SCAN_GAP_SECONDS", 600, 0),
            extra_excluded_bases=excluded,
            state_sync_command=(env.get("STATE_SYNC_COMMAND") or "").strip(),
            global_enabled=_bool_env(env, "GLOBAL_RANKING_ENABLED", True),
            global_refresh_interval_hours=_float_env(env, "GLOBAL_RANKING_REFRESH_HOURS", 24.0, 1.0),
            global_max_age_hours=_float_env(env, "GLOBAL_RANKING_MAX_AGE_HOURS", 72.0, 1.0),
            global_fallback_policy=_choice_env(env, "GLOBAL_FALLBACK_POLICY", "halt", ("halt", "mexc_only")),
            global_max_exchanges=_int_env(env, "GLOBAL_MAX_EXCHANGES", 40, 3),
            global_pairs_per_exchange=min(_int_env(env, "GLOBAL_PAIRS_PER_EXCHANGE", 1000, 50), 5000),
            global_max_credits=_int_env(env, "GLOBAL_MAX_CREDITS_PER_REFRESH", 400, 10),
            global_categories=tuple(c for c in _choice_env(
                env, "GLOBAL_VOLUME_CATEGORIES", "all", ("all", "perpetual", "futures")).replace("all", "perpetual,futures").split(",")),
            global_fill_to_n=_bool_env(env, "GLOBAL_FILL_TO_N", False),
            crypto_verification=_choice_env(env, "CRYPTO_VERIFICATION", "strict", ("strict", "symbol")),
            min_mexc_turnover_usdt=_float_env(env, "MIN_MEXC_TURNOVER_USDT", 1_000_000.0, 0.0),
            max_spread_pct=_float_env(env, "MAX_SPREAD_PCT", 0.5, 0.0),
            cmc_min_interval_seconds=_float_env(env, "CMC_MIN_INTERVAL_SECONDS", 1.3, 0.0),
            tp_extra_rr=_float_list_env(env, "TP_EXTRA_RR"),
            ranking_cache_path=env.get("RANKING_CACHE_PATH", "state/global_ranking.json") or "state/global_ranking.json",
        )
