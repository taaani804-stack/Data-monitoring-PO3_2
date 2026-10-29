import math

import pytest

from po3_strategy import atr, rma, rolling_highest, rolling_lowest, true_range
from tests.helpers import candle


def test_true_range_first_bar_is_high_minus_low():
    assert true_range([candle(0, 10, 12, 9, 11)]) == [3.0]


def test_true_range_uses_previous_close_gaps():
    cs = [candle(0, 10, 11, 9, 10), candle(1, 15, 16, 14, 15)]  # gap up
    assert true_range(cs)[1] == 6.0  # max(2, |16-10|=6, |14-10|=4)


def test_atr_matches_independent_wilder_reference():
    # length 3. TR = [2, 3, 4, 5] (hand-computed). seed = (2+3+4)/3 = 3; next = (3*2+5)/3 = 11/3
    cs = [candle(0, 10, 12, 10, 11), candle(1, 11, 13, 10, 12), candle(2, 12, 14, 10, 13), candle(3, 13, 15, 10, 14)]
    out = atr(cs, 3)
    assert math.isnan(out[0]) and math.isnan(out[1])
    assert out[2] == pytest.approx(3.0)
    assert out[3] == pytest.approx(11 / 3)


def test_atr_is_nan_until_length_bars():
    cs = [candle(i, 100, 101, 99, 100) for i in range(60)]
    out = atr(cs, 50)
    assert all(math.isnan(v) for v in out[:49])
    assert out[49] == pytest.approx(2.0)
    assert out[59] == pytest.approx(2.0)


def test_atr_default_length_wilder_step():
    cs = [candle(i, 100, 100.5, 99.5, 100) for i in range(50)] + [candle(50, 100, 100, 96, 99.5)]
    out = atr(cs, 50)
    assert out[49] == pytest.approx(1.0)
    assert out[50] == pytest.approx((1.0 * 49 + 4.0) / 50)  # 1.06


def test_rolling_extremes_include_current_bar_and_nan_warmup():
    v = [1, 5, 2, 4, 3]
    hi, lo = rolling_highest(v, 3), rolling_lowest(v, 3)
    assert math.isnan(hi[0]) and math.isnan(hi[1]) and math.isnan(lo[1])
    assert hi[2:] == [5, 5, 4]
    assert lo[2:] == [1, 2, 2]


def test_rma_short_input_all_nan():
    assert all(math.isnan(x) for x in rma([1.0, 2.0], 5))
