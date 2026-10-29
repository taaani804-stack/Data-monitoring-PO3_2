from models import Candle

TF15 = 15 * 60_000
T0 = 1_760_000_000_000 - (1_760_000_000_000 % TF15)  # aligned to a 15m boundary


def candle(i, o, h, l, c, t0=T0, tf=TF15):
    return Candle(t0 + i * tf, float(o), float(h), float(l), float(c), 1.0)


def flat(start, count, mid=100.0, half=0.5):
    """Tight range bars: TR == 2*half, close == mid."""
    return [candle(start + k, mid, mid + half, mid - half, mid) for k in range(count)]


def base_series():
    """60 flat bars (indices 0..59): ATR(50)=1 from bar 49, accumulation range 1 <= 5*ATR."""
    return flat(0, 60)


def buy_series(extra=()):
    """Bar 60 sweeps the lows (low 96) and closes 99.5 -> Bearish manipulation -> BUY."""
    return base_series() + [candle(60, 100, 100, 96, 99.5)] + list(extra)


def sell_series(extra=()):
    """Bar 60 sweeps the highs (high 104) and closes 100.5 -> Bullish manipulation -> SELL."""
    return base_series() + [candle(60, 100, 104, 100, 100.5)] + list(extra)


# ---------------------------------------------------------------------------------------------
# Multi-timeframe helpers (1h / 4h / 1d)
# ---------------------------------------------------------------------------------------------
from models import Candle, ContractInfo  # noqa: E402
from mexc_data import TIMEFRAME_MS, INTERVALS  # noqa: E402

DAY = 86_400_000
BOUNDARY = (1_760_000_000_000 // DAY) * DAY  # a UTC midnight: also a 4h and 1h boundary


def retime(candles, tf_ms, last_close_ms=BOUNDARY):
    """Re-stamp a candle list so the LAST candle closes exactly at ``last_close_ms`` for ``tf_ms``."""
    n = len(candles)
    return [
        Candle(last_close_ms - (n - i) * tf_ms, c.open, c.high, c.low, c.close, c.volume)
        for i, c in enumerate(candles)
    ]


def quiet_after(k, mid=100.0):
    """k quiet candles that do not touch the SELL signal's TP (95.9) or SL (105.8)."""
    return [candle(61 + j, mid, mid + 1, mid - 1, mid) for j in range(k)]


def contract(symbol="AAA_USDT", turnover=1e6, unit=0.01, scale=2):
    return ContractInfo(symbol, symbol.split("_")[0], turnover, unit, scale)


class FakeClient:
    """Fake MEXC client. ``series`` maps timeframe -> candle list (or callable symbol->list)."""

    def __init__(self, series, contracts=None, now_ms=None):
        self.series = series
        self.contracts = contracts or [contract()]
        self.now_ms = now_ms
        self.kline_calls = []  # (symbol, timeframe)
        self.fail_symbols = set()

    def get_klines(self, symbol, timeframe, limit):
        from mexc_data import MexcApiError
        self.kline_calls.append((symbol, timeframe))
        if symbol in self.fail_symbols:
            raise MexcApiError("boom")
        s = self.series[timeframe]
        return list(s(symbol) if callable(s) else s)

    def get_server_time_ms(self):
        return self.now_ms

    def get_top_contracts(self, top_n=100, extra_excluded=()):
        from models import SelectionStats
        sel = self.contracts[:top_n]
        return sel, SelectionStats(len(self.contracts), len(self.contracts), len(sel))


class FakeSender:
    def __init__(self, results=None):
        self.results, self.messages = list(results or []), []

    def send_text(self, text):
        self.messages.append(text)
        return self.results.pop(0) if self.results else True
