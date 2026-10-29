"""Python port of "ICT Power Of Three | Flux Charts" (Pine Script v5, by fluxchart).

Only the SIGNAL logic is ported: accumulation -> breakout -> manipulation -> entry,
plus the TP/SL exit simulation (needed because Pine only starts a new PO3 once the
previous trade has exited). Visuals, zones, dashboard and alert plumbing are not ported.

The main loop in :func:`run_po3` mirrors the Pine execution order line by line.
Pine statements that are plain sequential ``if`` blocks (NOT ``else if``) are kept
sequential here on purpose. Because ``accumulationEndIndex`` remains the prior
non-breakout bar when a breakout occurs, a sufficiently large breakout candle
can cascade through manipulation and entry on that same candle.

Pine -> Python differences are documented in README.md ("Pine-to-Python differences").
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence

from models import Candle, Signal

STRATEGY_NAME = "ICT Power Of Three | Flux Charts (Python port)"
STRATEGY_SHORT_NAME = "ICT Power of Three"

ALGORITHM_MODES = ("Small Manipulation", "Short Accumulation", "Big Manipulation")
BREAKOUT_METHODS = ("Close", "Wick")
TPSL_METHODS = ("Dynamic", "Fixed")
# Pine: riskAmount -> slATRMult
RISK_TO_SL_ATR_MULT = {"Highest": 9.5, "High": 6.0, "Normal": 5.0, "Low": 4.0, "Lowest": 1.5}

# PO3 states (Pine strings)
S_WAIT_ACC = "Waiting For Accumulation"
S_WAIT_ACC_END = "Waiting For Accumulation End"
S_WAIT_MANIP = "Waiting For Manipulation"
S_WAIT_DIST = "Waiting For Distribution"
S_ENTRY = "Entry Taken"
S_DONE = "Done"


@dataclass(frozen=True)
class PO3Config:
    """Indicator inputs. Defaults equal the Pine script defaults."""

    algorithm_mode: str = "Small Manipulation"
    breakout_method: str = "Wick"
    tpsl_method: str = "Dynamic"
    risk: str = "Normal"
    tp_percent: float = 0.3  # only used when tpsl_method == "Fixed"
    sl_percent: float = 0.4  # only used when tpsl_method == "Fixed"
    rr: float = 0.86  # Pine: RR (reward = RR * risk, Dynamic mode)
    atr_len: int = 50  # Pine: atrLen
    accumulation_expand_mult: float = 0.5  # Pine: accumulationExpandMult

    def __post_init__(self) -> None:
        if self.algorithm_mode not in ALGORITHM_MODES:
            raise ValueError(f"algorithm_mode must be one of {ALGORITHM_MODES}")
        if self.breakout_method not in BREAKOUT_METHODS:
            raise ValueError(f"breakout_method must be one of {BREAKOUT_METHODS}")
        if self.tpsl_method not in TPSL_METHODS:
            raise ValueError(f"tpsl_method must be one of {TPSL_METHODS}")
        if self.risk not in RISK_TO_SL_ATR_MULT:
            raise ValueError(f"risk must be one of {tuple(RISK_TO_SL_ATR_MULT)}")

    # --- values the Pine script derives from algorithmMode -------------------
    @property
    def accumulation_length(self) -> int:
        return 40 if self.algorithm_mode == "Small Manipulation" else 11

    @property
    def accumulation_atr_mult(self) -> float:
        return 2.0 if self.algorithm_mode == "Short Accumulation" else 5.0

    @property
    def manipulation_atr_mult(self) -> float:
        return 0.6 if self.algorithm_mode == "Small Manipulation" else 1.0

    @property
    def sl_atr_mult(self) -> float:
        return RISK_TO_SL_ATR_MULT[self.risk]

    @property
    def min_candles(self) -> int:
        """Candles needed before the first accumulation can even be detected."""
        return max(self.atr_len, self.accumulation_length)


# ----------------------------------------------------------------------------
# Indicators (Pine built-ins)
# ----------------------------------------------------------------------------
def true_range(candles: Sequence[Candle]) -> List[float]:
    """Pine ``ta.tr(true)``: first bar = high-low, later bars use previous close."""
    out: List[float] = []
    prev_close: Optional[float] = None
    for c in candles:
        if prev_close is None:
            out.append(c.high - c.low)
        else:
            out.append(max(c.high - c.low, abs(c.high - prev_close), abs(c.low - prev_close)))
        prev_close = c.close
    return out


def rma(values: Sequence[float], length: int) -> List[float]:
    """Pine ``ta.rma``: NaN until ``length`` values exist, SMA seed, then Wilder smoothing."""
    out = [math.nan] * len(values)
    if length <= 0 or len(values) < length:
        return out
    alpha = 1.0 / length
    prev = sum(values[:length]) / length  # Pine seeds with ta.sma on the first valid bar
    out[length - 1] = prev
    for i in range(length, len(values)):
        prev = alpha * values[i] + (1.0 - alpha) * prev
        out[i] = prev
    return out


def atr(candles: Sequence[Candle], length: int = 50) -> List[float]:
    """Pine ``ta.atr(length)`` = ``ta.rma(ta.tr(true), length)``."""
    return rma(true_range(candles), length)


def rolling_highest(values: Sequence[float], length: int) -> List[float]:
    """Pine ``ta.highest(length)`` (window includes the current bar; NaN until full)."""
    out = [math.nan] * len(values)
    for i in range(length - 1, len(values)):
        out[i] = max(values[i - length + 1 : i + 1])
    return out


def rolling_lowest(values: Sequence[float], length: int) -> List[float]:
    """Pine ``ta.lowest(length)``."""
    out = [math.nan] * len(values)
    for i in range(length - 1, len(values)):
        out[i] = min(values[i - length + 1 : i + 1])
    return out


# ----------------------------------------------------------------------------
# PO3 state object (Pine: type PO3)
# ----------------------------------------------------------------------------
@dataclass
class _PO3:
    state: str
    acc_top: float = math.nan
    acc_bottom: float = math.nan
    acc_end_top: float = math.nan
    acc_end_bottom: float = math.nan
    acc_start_index: Optional[int] = None
    acc_end_index: Optional[int] = None
    manip_top: float = math.nan
    manip_bottom: float = math.nan
    manip_direction: Optional[str] = None
    entry_type: Optional[str] = None  # "Long" / "Short"
    entry_index: Optional[int] = None
    entry_time: Optional[int] = None
    entry_price: float = math.nan
    sl_target: float = math.nan
    tp_target: float = math.nan
    atr_at_entry: float = math.nan
    exit_price: Optional[float] = None  # Pine: na until exited
    exit_index: Optional[int] = None
    exit_reason: Optional[str] = None
    manip_atr_mult: float = math.nan  # context only (reason text)
    acc_length: int = 0  # context only (reason text)


def run_po3(candles: Sequence[Candle], cfg: Optional[PO3Config] = None) -> List[Signal]:
    """Run the PO3 state machine over CLOSED candles (oldest first); return all entries."""
    cfg = cfg or PO3Config()
    n = len(candles)
    atr_v = atr(candles, cfg.atr_len)
    highest_acc = rolling_highest([c.high for c in candles], cfg.accumulation_length)
    lowest_acc = rolling_lowest([c.low for c in candles], cfg.accumulation_length)
    wick = cfg.breakout_method == "Wick"

    po3s: List[_PO3] = []
    last: Optional[_PO3] = None

    for i in range(n):
        c = candles[i]
        a = atr_v[i]
        low_b = c.low if wick else c.close
        high_b = c.high if wick else c.close

        # --- create a new PO3 unless a trade is still open / PO3 still in progress
        # Pine: createNewPO3 = true; if not na(lastPO3) and na(lastPO3.exitPrice) -> false
        if last is None or last.exit_price is not None:
            last = _PO3(state=S_WAIT_ACC)
            po3s.append(last)

        # --- Find Accumulation
        if last.state == S_WAIT_ACC:
            # NaN (ATR / window not ready) makes the comparison False, same as Pine's na.
            if (highest_acc[i] - lowest_acc[i]) <= a * cfg.accumulation_atr_mult:
                last.state = S_WAIT_ACC_END
                last.acc_start_index = i - cfg.accumulation_length
                last.acc_end_index = i
                last.acc_top = highest_acc[i] + a * cfg.accumulation_expand_mult
                last.acc_bottom = lowest_acc[i] - a * cfg.accumulation_expand_mult

        # NOTE: plain `if` (not elif) -> cascades within the same bar, as in Pine.
        if last.state == S_WAIT_ACC_END:
            if high_b > last.acc_top or low_b < last.acc_bottom:
                last.state = S_WAIT_MANIP
                last.acc_end_top = high_b
                last.acc_end_bottom = low_b
            else:
                last.acc_end_index = i

        # --- Find Manipulation
        if last.state == S_WAIT_MANIP:
            if i > last.acc_end_index:
                if high_b > last.acc_top + a * cfg.manipulation_atr_mult:
                    last.state = S_WAIT_DIST
                    last.manip_direction = "Bullish"
                    last.manip_top = high_b
                    last.manip_bottom = last.acc_end_bottom
                elif low_b < last.acc_bottom - a * cfg.manipulation_atr_mult:
                    last.state = S_WAIT_DIST
                    last.manip_direction = "Bearish"
                    last.manip_top = last.acc_end_top
                    last.manip_bottom = low_b

        # --- "Distribution" = take the entry on this very bar's close
        if last.state == S_WAIT_DIST:
            last.state = S_ENTRY
            last.entry_index = i
            last.entry_time = c.open_time_ms
            last.entry_price = c.close
            last.atr_at_entry = a
            last.manip_atr_mult = cfg.manipulation_atr_mult
            last.acc_length = cfg.accumulation_length
            ep = last.entry_price
            if last.manip_direction == "Bearish":  # swept lows -> Long / BUY
                last.entry_type = "Long"
                if cfg.tpsl_method == "Fixed":
                    last.sl_target = ep * (1 - cfg.sl_percent / 100.0)
                    last.tp_target = ep * (1 + cfg.tp_percent / 100.0)
                else:
                    last.sl_target = ep - a * cfg.sl_atr_mult
                    last.tp_target = ep + abs(ep - last.sl_target) * cfg.rr
            else:  # swept highs -> Short / SELL
                last.entry_type = "Short"
                if cfg.tpsl_method == "Fixed":
                    last.sl_target = ep * (1 + cfg.sl_percent / 100.0)
                    last.tp_target = ep * (1 - cfg.tp_percent / 100.0)
                else:
                    last.sl_target = ep + a * cfg.sl_atr_mult
                    last.tp_target = ep - abs(ep - last.sl_target) * cfg.rr

        # --- Entry Taken: check TP, then SL, on LATER bars only (time > entryTime)
        # TP and SL blocks are sequential `if`s in Pine: if both are hit on one bar the
        # SL block runs last and overwrites the TP result (SL wins).
        if last.state == S_ENTRY and c.open_time_ms > last.entry_time:
            ep = last.entry_price
            is_long = last.entry_type == "Long"
            if cfg.tpsl_method == "Fixed":
                if is_long and ((c.high / ep) - 1) * 100 >= cfg.tp_percent:
                    _exit(last, i, "TP", ep * (1 + cfg.tp_percent / 100.0))
                if (not is_long) and ((c.low / ep) - 1) * 100 <= -cfg.tp_percent:
                    _exit(last, i, "TP", ep * (1 - cfg.tp_percent / 100.0))
                if is_long and ((c.low / ep) - 1) * 100 <= -cfg.sl_percent:
                    _exit(last, i, "SL", ep * (1 - cfg.sl_percent / 100.0))
                if (not is_long) and ((c.high / ep) - 1) * 100 >= cfg.sl_percent:
                    _exit(last, i, "SL", ep * (1 + cfg.sl_percent / 100.0))
            else:
                if is_long and c.high >= last.tp_target:
                    _exit(last, i, "TP", last.tp_target)
                if (not is_long) and c.low <= last.tp_target:
                    _exit(last, i, "TP", last.tp_target)
                if is_long and c.low <= last.sl_target:
                    _exit(last, i, "SL", last.sl_target)
                if (not is_long) and c.high >= last.sl_target:
                    _exit(last, i, "SL", last.sl_target)

    return [_to_signal(p) for p in po3s if p.entry_type is not None]


def _exit(po3: _PO3, index: int, reason: str, price: float) -> None:
    po3.exit_price = price
    po3.exit_index = index
    po3.exit_reason = reason
    po3.state = S_DONE  # Pine's render block moves TP/SL states to "Done"


def _to_signal(p: _PO3) -> Signal:
    return Signal(
        direction="BUY" if p.entry_type == "Long" else "SELL",
        entry_index=p.entry_index,
        candle_open_ms=p.entry_time,
        entry_price=p.entry_price,
        stop_loss=p.sl_target,
        take_profit=p.tp_target,
        atr=p.atr_at_entry,
        exit_index=p.exit_index,
        exit_reason=p.exit_reason,
        exit_price=p.exit_price,
        acc_top=p.acc_top,
        acc_bottom=p.acc_bottom,
        manip_extreme=(p.manip_top if p.manip_direction == "Bullish" else p.manip_bottom),
        manip_atr_mult=p.manip_atr_mult,
        acc_length=p.acc_length,
    )


def signal_id(symbol: str, timeframe: str, direction: str, candle_open_ms: int, exchange: str = "MEXC") -> str:
    """Stable identifier used for duplicate prevention: exchange + pair + timeframe + direction + candle."""
    return f"PO3|{exchange}|{symbol}|{timeframe}|{direction}|{candle_open_ms}"


def legacy_signal_id(symbol: str, timeframe: str, direction: str, candle_open_ms: int) -> str:
    """Pre-v2 identifier (no exchange part); still honoured so an upgrade cannot re-send old alerts."""
    return f"PO3|{symbol}|{timeframe}|{direction}|{candle_open_ms}"
