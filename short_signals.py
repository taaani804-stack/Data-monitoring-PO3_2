"""SHORT-only gate: validation, exchange-precision rounding and human-readable setup reason.

The PO3 state machine (po3_strategy.py) is untouched; this module only decides whether a signal that
the strategy produced may be ALERTED, and prepares its prices for the exchange.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from decimal import ROUND_HALF_UP, Decimal
from typing import Callable, Optional, Tuple

from models import ContractInfo, Signal


class SignalRejected(Exception):
    """A signal that must not be sent (the reason is logged and counted)."""


def tick_size(contract: Optional[ContractInfo]) -> Optional[Decimal]:
    """Tick size from MEXC ``priceUnit`` (preferred) or ``priceScale``; None if unknown."""
    if contract is None:
        return None
    if contract.price_unit and contract.price_unit > 0:
        return Decimal(repr(contract.price_unit))
    if contract.price_scale is not None and contract.price_scale >= 0:
        return Decimal(1).scaleb(-contract.price_scale)
    return None


def round_to_tick(price: float, tick: Optional[Decimal]) -> float:
    if tick is None or tick <= 0:
        return price
    steps = (Decimal(repr(price)) / tick).to_integral_value(rounding=ROUND_HALF_UP)
    return float(steps * tick)


def decimals_for(contract: Optional[ContractInfo]) -> Optional[int]:
    tick = tick_size(contract)
    if tick is None:
        return None
    return max(0, -tick.normalize().as_tuple().exponent)


def _ok(x: float) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x) and x > 0


def validate_short(signal: Signal, contract: Optional[ContractInfo] = None) -> Signal:
    """Return the signal with exchange-rounded prices, or raise :class:`SignalRejected`.

    Checks: SELL direction, entry/SL/TP present, finite and > 0, SL above entry, TP below entry,
    and a mathematically valid risk/reward after rounding to the contract's tick size.
    """
    if signal.direction != "SELL":
        raise SignalRejected(f"direction {signal.direction!r} is not SELL (SHORT-only scanner)")
    for name, value in (("entry", signal.entry_price), ("stop_loss", signal.stop_loss), ("take_profit", signal.take_profit)):
        if not _ok(value):
            raise SignalRejected(f"{name} is missing / not finite / not > 0 ({value!r})")
    tick = tick_size(contract)
    entry = round_to_tick(signal.entry_price, tick)
    sl = round_to_tick(signal.stop_loss, tick)
    tp = round_to_tick(signal.take_profit, tick)
    if not (_ok(entry) and _ok(sl) and _ok(tp)):
        raise SignalRejected("a price became <= 0 after rounding to the tick size")
    if not sl > entry:
        raise SignalRejected(f"stop loss {sl} is not above entry {entry}")
    if not tp < entry:
        raise SignalRejected(f"take profit {tp} is not below entry {entry}")
    risk, reward = sl - entry, entry - tp
    if not (math.isfinite(risk) and math.isfinite(reward) and risk > 0 and reward > 0):
        raise SignalRejected("invalid risk/reward")
    return replace(signal, entry_price=entry, stop_loss=sl, take_profit=tp)


def risk_reward(signal: Signal) -> float:
    """reward / risk for a SHORT: (entry - TP) / (SL - entry)."""
    return (signal.entry_price - signal.take_profit) / (signal.stop_loss - signal.entry_price)


def describe_setup(signal: Signal, fmt: Callable[[float], str]) -> str:
    """One-sentence reason built ONLY from values the strategy actually produced."""
    parts = []
    if math.isfinite(signal.acc_top) and math.isfinite(signal.acc_bottom):
        length = f"{signal.acc_length}-candle " if signal.acc_length else ""
        parts.append(f"{length}accumulation range {fmt(signal.acc_bottom)}–{fmt(signal.acc_top)}")
    if math.isfinite(signal.manip_extreme):
        thr = f" by more than {signal.manip_atr_mult:g}×ATR" if math.isfinite(signal.manip_atr_mult) else ""
        parts.append(f"manipulation swept the range highs{thr} (high {fmt(signal.manip_extreme)})")
    parts.append("distribution: SELL taken at the close of the confirming candle")
    return "; ".join(parts).capitalize() + "."


def setup_lines(signal: Signal, fmt: Callable[[float], str]) -> dict:
    """Evidence lines for the alert, built ONLY from values the strategy produced.

    The original Flux Charts PO3 logic has no separate BOS / MSS / displacement step (it is accumulation ->
    breakout -> manipulation -> distribution entry), so none of those are claimed.
    """
    acc = ""
    if math.isfinite(signal.acc_top) and math.isfinite(signal.acc_bottom):
        length = f"{signal.acc_length}-candle " if signal.acc_length else ""
        acc = f"{length}accumulation range {fmt(signal.acc_bottom)}–{fmt(signal.acc_top)}"
    sweep = "n/a"
    if math.isfinite(signal.manip_extreme):
        thr = f" by more than {signal.manip_atr_mult:g}×ATR" if math.isfinite(signal.manip_atr_mult) else ""
        sweep = f"range highs swept{thr} (high {fmt(signal.manip_extreme)}) after {acc or 'the accumulation range'}"
    return {
        "structure": "PO3 distribution phase after an upside liquidity sweep (original logic has no separate BOS/MSS step)",
        "sweep": sweep,
        "confirmation": "entry taken at the close of the confirming closed candle",
    }


def extra_take_profits(signal: Signal, rr_multiples, contract: Optional[ContractInfo] = None):
    """OPTIONAL user-configured extra targets (TP_EXTRA_RR): entry - m * risk, rounded, always below entry."""
    risk = signal.stop_loss - signal.entry_price
    tick = tick_size(contract)
    out = []
    for m in rr_multiples:
        tp = round_to_tick(signal.entry_price - m * risk, tick)
        if _ok(tp) and tp < signal.entry_price:
            out.append((m, tp))
    return out
