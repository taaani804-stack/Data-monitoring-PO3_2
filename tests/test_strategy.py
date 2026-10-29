import pytest

from po3_strategy import PO3Config, run_po3
from tests.helpers import base_series, buy_series, candle, flat, sell_series

ATR60 = (1.0 * 49 + 4.0) / 50  # 1.06: ATR on the sweep bar (TR = 4)


def test_defaults_match_pine_defaults():
    c = PO3Config()
    assert (c.algorithm_mode, c.breakout_method, c.tpsl_method, c.risk) == ("Small Manipulation", "Wick", "Dynamic", "Normal")
    assert c.accumulation_length == 40
    assert c.accumulation_atr_mult == 5
    assert c.manipulation_atr_mult == 0.6
    assert c.sl_atr_mult == 5
    assert c.rr == 0.86 and c.atr_len == 50 and c.accumulation_expand_mult == 0.5
    assert (c.tp_percent, c.sl_percent) == (0.3, 0.4)


@pytest.mark.parametrize(
    "mode,length,acc_mult,manip_mult",
    [("Small Manipulation", 40, 5, 0.6), ("Short Accumulation", 11, 2, 1), ("Big Manipulation", 11, 5, 1)],
)
def test_algorithm_mode_mapping(mode, length, acc_mult, manip_mult):
    c = PO3Config(algorithm_mode=mode)
    assert (c.accumulation_length, c.accumulation_atr_mult, c.manipulation_atr_mult) == (length, acc_mult, manip_mult)


def test_no_signal_in_quiet_market():
    assert run_po3(base_series()) == []


def test_buy_direction_after_low_sweep_and_dynamic_levels():
    sigs = run_po3(buy_series())
    assert len(sigs) == 1
    s = sigs[0]
    assert s.direction == "BUY"
    assert s.entry_index == 60
    assert s.entry_price == 99.5  # close of the signal candle
    assert s.atr == pytest.approx(ATR60)
    assert s.stop_loss == pytest.approx(99.5 - 5 * ATR60)  # 94.2
    assert s.take_profit == pytest.approx(99.5 + 5 * ATR60 * 0.86)  # 104.058
    assert s.stop_loss < s.entry_price < s.take_profit
    assert s.reward_risk == pytest.approx(0.86)
    assert s.exit_index is None


def test_sell_direction_after_high_sweep_and_dynamic_levels():
    sigs = run_po3(sell_series())
    assert len(sigs) == 1
    s = sigs[0]
    assert s.direction == "SELL"
    assert s.entry_price == 100.5
    assert s.stop_loss == pytest.approx(100.5 + 5 * ATR60)  # 105.8
    assert s.take_profit == pytest.approx(100.5 - 5 * ATR60 * 0.86)  # 95.942
    assert s.stop_loss > s.entry_price > s.take_profit


def test_small_sweep_below_manipulation_threshold_gives_no_signal():
    # accBottom = 99; threshold = 99 - 0.6*ATR. Low 98.8 breaks the box but is not far enough.
    assert run_po3(base_series() + [candle(60, 100, 100, 98.8, 99.5)]) == []


def test_close_breakout_method_ignores_wicks():
    wick_only = base_series() + [candle(60, 100, 100.4, 96, 99.8)]  # deep wick, close inside box
    assert run_po3(wick_only, PO3Config(breakout_method="Close")) == []
    assert len(run_po3(wick_only)) == 1  # Wick method does trigger


def test_exit_take_profit_on_later_bar():
    s = run_po3(buy_series([candle(61, 100, 105, 99, 104)]))[0]
    assert (s.exit_index, s.exit_reason) == (61, "TP")
    assert s.exit_price == pytest.approx(s.take_profit)


def test_exit_stop_loss_on_later_bar():
    s = run_po3(buy_series([candle(61, 100, 101, 94, 95)]))[0]
    assert (s.exit_index, s.exit_reason) == (61, "SL")
    assert s.exit_price == pytest.approx(s.stop_loss)


def test_stop_loss_wins_when_tp_and_sl_hit_same_bar():
    s = run_po3(buy_series([candle(61, 100, 105, 94, 100)]))[0]
    assert s.exit_reason == "SL"  # Pine's SL block runs last and overwrites TP


def test_sell_exit_take_profit_and_stop():
    assert run_po3(sell_series([candle(61, 100, 101, 95, 96)]))[0].exit_reason == "TP"  # low <= 95.942
    assert run_po3(sell_series([candle(61, 100, 106, 99, 105)]))[0].exit_reason == "SL"  # high >= 105.8


def test_no_second_signal_while_trade_is_open():
    sigs = run_po3(buy_series(flat(61, 15, mid=100, half=1.0)))  # stays inside (94.2, 104.058)
    assert len(sigs) == 1 and sigs[0].exit_index is None


def test_new_cycle_starts_after_exit():
    series = buy_series([candle(61, 100, 105, 99, 104)])  # TP exit on bar 61
    series += flat(62, 45, mid=104.0, half=0.5)  # new accumulation
    series += [candle(107, 104, 104, 90, 103)]  # low sweep -> second BUY
    sigs = run_po3(series)
    assert [s.direction for s in sigs] == ["BUY", "BUY"]
    assert sigs[0].exit_reason == "TP"
    assert sigs[1].entry_index == 107


def test_fixed_tpsl_method_uses_percentages():
    cfg = PO3Config(tpsl_method="Fixed")
    s = run_po3(buy_series(), cfg)[0]
    assert s.stop_loss == pytest.approx(99.5 * (1 - 0.4 / 100))
    assert s.take_profit == pytest.approx(99.5 * (1 + 0.3 / 100))
    sell = run_po3(sell_series(), cfg)[0]
    assert sell.stop_loss == pytest.approx(100.5 * (1 + 0.4 / 100))
    assert sell.take_profit == pytest.approx(100.5 * (1 - 0.3 / 100))


def test_fixed_exit_triggers_on_percent_move():
    cfg = PO3Config(tpsl_method="Fixed")
    s = run_po3(buy_series([candle(61, 99.6, 99.9, 99.55, 99.8)]), cfg)[0]  # +0.402% >= 0.3%
    assert s.exit_reason == "TP"
    assert s.exit_price == pytest.approx(99.5 * 1.003)


def test_risk_setting_changes_stop_distance():
    hi = run_po3(buy_series(), PO3Config(risk="Highest"))[0]
    lo = run_po3(buy_series(), PO3Config(risk="Lowest"))[0]
    assert (hi.entry_price - hi.stop_loss) == pytest.approx(9.5 * hi.atr)
    assert (lo.entry_price - lo.stop_loss) == pytest.approx(1.5 * lo.atr)


def test_invalid_config_rejected():
    with pytest.raises(ValueError):
        PO3Config(algorithm_mode="nope")
    with pytest.raises(ValueError):
        PO3Config(risk="Extreme")


def test_entry_is_unchanged_when_later_candles_are_appended():
    a = run_po3(buy_series())[0]
    b = run_po3(buy_series([candle(61, 100, 101, 99, 100)]))[0]
    assert (a.entry_index, a.entry_price, a.stop_loss, a.take_profit) == (b.entry_index, b.entry_price, b.stop_loss, b.take_profit)



def test_breakout_bar_can_also_be_manipulation_and_entry_bar():
    # Pine updates accumulationEndIndex on each non-breakout bar, so at the
    # breakout it still points to the preceding bar. The same oversized candle
    # can therefore pass the strict index guard, qualify as manipulation, and
    # trigger entry because the source uses sequential `if` blocks.
    series = base_series() + [candle(60, 100, 105, 95, 100)]
    signals = run_po3(series)
    assert len(signals) == 1
    assert signals[0].entry_index == 60
    assert signals[0].direction == "SELL"


def test_fixed_stop_loss_wins_when_tp_and_sl_hit_same_bar():
    cfg = PO3Config(tpsl_method="Fixed")
    # Pine checks TP first and SL second using sequential `if` blocks, so SL
    # overwrites TP when both fixed levels are touched by one later candle.
    s = run_po3(buy_series([candle(61, 100, 101, 98, 99)]), cfg)[0]
    assert s.exit_reason == "SL"
    assert s.exit_price == pytest.approx(s.stop_loss)


def test_pine_strict_breakout_thresholds_do_not_use_equality():
    # Equality with the expanded box boundary is not a breakout in Pine:
    # the source uses > and <, not >= and <=.
    # Flat bars establish accTop=100.5 and accBottom=99.5; this candle only
    # touches both boundaries and therefore must not leave accumulation.
    series = flat(0, 60) + [candle(60, 100, 100.5, 99.5, 100)]
    assert run_po3(series) == []
