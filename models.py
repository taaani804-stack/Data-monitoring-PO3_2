"""Plain data containers shared by the strategy, data and alert modules."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class Candle:
    """One OHLCV candle. ``open_time_ms`` is the candle OPEN time (UTC, ms)."""

    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True)
class Signal:
    """An entry produced by the PO3 state machine (Pine: PO3 object with entryType set)."""

    direction: str  # "BUY" (Pine entryType "Long") or "SELL" (Pine "Short")
    entry_index: int  # index of the signal candle in the candle list
    candle_open_ms: int  # open time of the signal candle (Pine: entryTime)
    entry_price: float  # close of the signal candle (Pine: entryPrice := close)
    stop_loss: float
    take_profit: float
    atr: float  # ATR(50) on the signal candle
    # Filled in only if a later candle in the data already hit TP/SL.
    exit_index: Optional[int] = None
    exit_reason: Optional[str] = None  # "TP" or "SL"
    exit_price: Optional[float] = None
    # Context of the PO3 cycle (used only to write an honest "reason" line; never used to decide).
    acc_top: float = float("nan")  # accumulation box top (range high + 0.5*ATR)
    acc_bottom: float = float("nan")  # accumulation box bottom (range low - 0.5*ATR)
    manip_extreme: float = float("nan")  # manipulation extreme (sweep high for SELL / sweep low for BUY)
    manip_atr_mult: float = float("nan")  # manipulation threshold in ATRs used by the algorithm mode
    acc_length: int = 0  # accumulation length in candles used by the algorithm mode

    @property
    def reward_risk(self) -> float:
        """Actual reward / risk implied by the TP and SL levels."""
        risk = abs(self.entry_price - self.stop_loss)
        reward = abs(self.take_profit - self.entry_price)
        return reward / risk if risk else float("nan")


@dataclass(frozen=True)
class ContractInfo:
    """Verified USDT-M perpetual contract metadata (from MEXC ``contract/detail`` + ``contract/ticker``)."""

    symbol: str  # exchange symbol, e.g. "BTC_USDT"
    base: str
    turnover_24h: float  # 24h quote (USDT) turnover, futures only
    price_unit: float = 0.0  # tick size (MEXC ``priceUnit``); 0 = unknown
    price_scale: Optional[int] = None  # decimals (MEXC ``priceScale``); None = unknown
    spread_pct: Optional[float] = None  # (ask1-bid1)/mid*100 from the MEXC ticker; None = not available
    name: str = ""  # asset name (CoinMarketCap), for the Telegram message
    # --- global futures-volume ranking (CoinMarketCap); None / "" = unavailable ("n/a" in alerts) ---
    crypto_id: Optional[int] = None
    global_rank: Optional[int] = None
    global_volume_usd: Optional[float] = None
    global_source: str = ""  # e.g. "CoinMarketCap derivatives (top 40 exchanges)"
    ranking_ts: str = ""  # ISO-8601 UTC time the global ranking was built
    ranking_note: str = ""  # e.g. "cached, 5.0h old" / "stale cache" / "MEXC-only fallback"


@dataclass
class SelectionStats:
    """Numbers logged for every symbol selection."""

    discovered: int = 0  # contracts returned by the exchange
    eligible: int = 0  # passed all contract filters AND have a usable 24h turnover
    selected: int = 0
    excluded: dict = field(default_factory=dict)  # reason -> list of symbols


class ConfigError(Exception):
    """Safe-to-print configuration problem (never contains secret values)."""
