import pytest
import requests

from mexc_data import (
    MexcApiError, MexcClient, drop_unclosed_candles, parse_futures_klines,
    parse_klines, select_top_symbols, timeframe_ms,
)
from tests.helpers import T0, TF15, candle, flat


def test_forming_candle_is_dropped():
    cs = flat(0, 5)
    now = T0 + 4 * TF15 + 60_000
    closed = drop_unclosed_candles(cs, TF15, now)
    assert [c.open_time_ms for c in closed] == [T0 + k * TF15 for k in range(4)]


def test_candle_counts_as_closed_exactly_at_its_end():
    cs = flat(0, 3)
    assert len(drop_unclosed_candles(cs, TF15, T0 + 3 * TF15)) == 3
    assert len(drop_unclosed_candles(cs, TF15, T0 + 3 * TF15 - 1)) == 2


def test_close_buffer_delays_closing():
    cs = flat(0, 3)
    now = T0 + 3 * TF15 + 2_000
    assert len(drop_unclosed_candles(cs, TF15, now, buffer_ms=5_000)) == 2
    assert len(drop_unclosed_candles(cs, TF15, now, buffer_ms=1_000)) == 3


def test_timeframe_ms_and_unsupported():
    assert timeframe_ms("15m") == 900_000 and timeframe_ms("1h") == 3_600_000
    assert timeframe_ms("4h") == 4 * 3_600_000 and timeframe_ms("1d") == 86_400_000
    with pytest.raises(ValueError):
        timeframe_ms("2h")


def test_parse_klines_sorts_dedupes_and_skips_bad_rows():
    rows = [
        [2000, "2", "3", "1", "2.5", "10", 2999, "25"],
        [1000, "1", "2", "0.5", "1.5", "10", 1999, "15"],
        [1000, "1", "2", "0.5", "1.6", "10", 1999, "15"],
        [3000, "x", "3", "1", "2", "1"],
        [4000, "5", "4", "6", "5", "1"],
    ]
    cs = parse_klines(rows)
    assert [c.open_time_ms for c in cs] == [1000, 2000]
    assert cs[0].close == 1.6


def test_parse_klines_rejects_non_list():
    with pytest.raises(MexcApiError):
        parse_klines({"code": 1})


def test_parse_futures_column_arrays_and_seconds_timestamps():
    data = {
        "time": [T0 // 1000 + 900, T0 // 1000],
        "open": [2, 1], "high": [3, 2], "low": [1, 0.5],
        "close": [2.5, 1.5], "vol": [10, 20],
    }
    cs = parse_futures_klines(data)
    assert [c.open_time_ms for c in cs] == [T0, T0 + 900_000]
    assert cs[0].close == 1.5 and cs[1].volume == 10


def test_parse_futures_klines_rejects_bad_payload():
    with pytest.raises(MexcApiError):
        parse_futures_klines({"time": []})


def _contract(base, quote="USDT", settle="USDT", future_type=1, state=0, **extra):
    d = {
        "symbol": f"{base}_{quote}", "baseCoin": base, "quoteCoin": quote,
        "settleCoin": settle, "futureType": future_type, "state": state,
    }
    d.update(extra)
    return d


def _tk(symbol, amount, last="1.0", **extra):
    d = {"symbol": symbol, "lastPrice": last, "amount24": str(amount)}
    d.update(extra)
    return d


def test_top_futures_symbols_sorted_by_turnover_and_limited():
    contracts = [_contract("AAA"), _contract("BBB"), _contract("CCC"), _contract("DDD")]
    tickers = [_tk("AAA_USDT", 10), _tk("BBB_USDT", 500), _tk("CCC_USDT", 50), _tk("DDD_USDT", 5)]
    assert select_top_symbols(contracts, tickers, 3) == ["BBB_USDT", "CCC_USDT", "AAA_USDT"]


def test_unsuitable_or_nonperpetual_contracts_are_excluded():
    contracts = [
        _contract("GOOD"),
        _contract("BAD1", state=1),
        _contract("USDC"),
        _contract("BTC3L"),
        _contract("ETH5S"),
        _contract("DELIVERY", future_type=2),
        _contract("NOTSETTLED", settle="BTC"),
        _contract("EURQ", quote="EUR", settle="EUR"),
        _contract("JUP"),
    ]
    tickers = [_tk(c["symbol"], 100) for c in contracts] + [_tk("GHOST_USDT", 999)]
    assert sorted(select_top_symbols(contracts, tickers, 50)) == ["GOOD_USDT", "JUP_USDT"]


def test_zero_turnover_is_excluded():
    contracts = [_contract("ZERO"), _contract("OK")]
    tickers = [_tk("ZERO_USDT", 0), _tk("OK_USDT", 150)]
    assert select_top_symbols(contracts, tickers, 50) == ["OK_USDT"]


class FakeResp:
    def __init__(self, status=200, payload=None, text="", headers=None):
        self.status_code, self._payload, self.text, self.headers = status, payload, text, headers or {}

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _client(responses):
    s = FakeSession(responses)
    return MexcClient(session=s, sleep=lambda _s: None, max_retries=3), s


def test_client_retries_on_429_and_network_errors():
    c, s = _client([
        requests.ConnectionError("x"), FakeResp(429),
        FakeResp(200, {"success": True, "data": 123}),
    ])
    assert c.get_server_time_ms() == 123
    assert len(s.calls) == 3


def test_client_gives_up_after_retries():
    c, _ = _client([FakeResp(500)] * 10)
    with pytest.raises(MexcApiError):
        c.get_server_time_ms()


def test_futures_kline_uses_correct_path_interval_and_time_range():
    data = {
        "time": [T0 // 1000], "open": [1], "high": [2],
        "low": [0.5], "close": [1.5], "vol": [1],
    }
    c, session = _client([FakeResp(200, {"success": True, "data": data})])
    out = c.get_klines("BTC_USDT", "1h", 500)
    url, params = session.calls[0]
    assert url.endswith("/api/v1/contract/kline/BTC_USDT")
    assert params["interval"] == "Min60"
    assert params["end"] > params["start"]
    assert len(out) == 1


def test_server_time_and_contract_ticker_response_shapes():
    c, _ = _client([FakeResp(200, {"success": True, "data": 123})])
    assert c.get_server_time_ms() == 123
    c, _ = _client([FakeResp(200, {"success": True, "data": [_tk("BTC_USDT", 100)]})])
    assert c.get_tickers_24h()[0]["symbol"] == "BTC_USDT"


def test_contract_detail_response_shape():
    c, _ = _client([FakeResp(200, {"success": True, "data": [_contract("BTC")]})])
    assert c.get_exchange_info()[0]["symbol"] == "BTC_USDT"
