"""Runs scanner.main() through real HTTP against a local fake MEXC + Telegram server."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

import mexc_data
import scanner
import telegram_bot
from models import Candle
from tests.helpers import BOUNDARY, buy_series, retime, sell_series

SECRET = "999:E2E-SECRET-TOKEN"
H = 3_600_000
MS = {"Min60": H, "Hour4": 4 * H, "Day1": 24 * H}
TF_OF = {"Min60": "1h", "Hour4": "4h", "Day1": "1d"}


class State:
    sent = []
    kline_requests = []
    now_ms = BOUNDARY + 6 * 60_000


def _rows(candles, forming_open=None):
    cs = list(candles)
    if forming_open is not None:  # MEXC returns the still-forming candle as the last row, with a wild range
        cs.append(Candle(forming_open, 100, 500, 1, 400, 1))
    return {
        "time": [c.open_time_ms // 1000 for c in cs],
        "open": [str(c.open) for c in cs], "high": [str(c.high) for c in cs],
        "low": [str(c.low) for c in cs], "close": [str(c.close) for c in cs],
        "vol": ["1"] * len(cs), "amount": ["1"] * len(cs),
    }


def _contract(base, **extra):
    d = {"symbol": f"{base}_USDT", "baseCoin": base, "quoteCoin": "USDT", "settleCoin": "USDT",
         "futureType": 1, "state": 0, "priceUnit": 0.01, "priceScale": 2}
    d.update(extra)
    return d


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/api/v1/contract/ping":
            return self._json({"success": True, "data": State.now_ms})
        if u.path == "/api/v1/contract/detail":
            return self._json({"success": True, "data": [
                _contract("AAA"), _contract("BBB"), _contract("USDC"),
                _contract("TSLA"),                       # US stock perpetual: must be excluded
                _contract("DEAD", state=3),              # delisted: must be excluded
                {**_contract("COINM"), "settleCoin": "BTC"},  # coin-margined: must be excluded
            ]})
        if u.path == "/api/v1/contract/ticker":
            return self._json({"success": True, "data": [
                {"symbol": "TSLA_USDT", "lastPrice": "200", "amount24": "9000000000"},
                {"symbol": "USDC_USDT", "lastPrice": "1", "amount24": "8000000000"},
                {"symbol": "AAA_USDT", "lastPrice": "100", "amount24": "5000000"},
                {"symbol": "BBB_USDT", "lastPrice": "100", "amount24": "4000000"},
                {"symbol": "DEAD_USDT", "lastPrice": "1", "amount24": "7000000000"},
                {"symbol": "COINM_USDT", "lastPrice": "1", "amount24": "7000000000"},
            ]})
        if u.path.startswith("/api/v1/contract/kline/"):
            symbol = u.path.rsplit("/", 1)[-1]
            interval = q["interval"][0]
            State.kline_requests.append((symbol, interval))
            if interval not in MS:
                return self._json({"success": False, "code": 500, "msg": "Invalid interval"}, 400)
            tf_ms = MS[interval]
            last_close = (State.now_ms // tf_ms) * tf_ms
            base = sell_series() if symbol == "AAA_USDT" else buy_series()
            series = retime(base, tf_ms, last_close)
            return self._json({"success": True, "data": _rows(series, forming_open=last_close)})
        self._json({"msg": "nope"}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(n))
        State.sent.append((self.path, payload))
        self._json({"ok": True})


@pytest.fixture
def server(monkeypatch, tmp_path):
    State.sent, State.kline_requests, State.now_ms = [], [], BOUNDARY + 6 * 60_000
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}"
    monkeypatch.setattr(mexc_data, "BASE_URL", url)
    monkeypatch.setattr(telegram_bot, "API_URL", url)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", SECRET)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "4242")
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "sent.json"))
    monkeypatch.setenv("SCAN_GAP_SECONDS", "0")
    monkeypatch.setenv("GLOBAL_RANKING_ENABLED", "false")  # legacy MEXC-only path; global ranking has its own tests
    monkeypatch.setenv("MIN_MEXC_TURNOVER_USDT", "0")
    yield url
    srv.shutdown()


def test_auto_cycle_over_http_sends_only_short_signals_and_second_run_is_silent(server, capsys):
    assert scanner.main([]) == 0
    texts = [p["text"] for _, p in State.sent]
    assert len(texts) == 3  # AAA SELL on 1D, 4H, 1H ; BBB is bullish -> nothing
    assert all("SHORT SIGNAL" in t and "AAA_USDT" in t for t in texts)
    assert all("BBB" not in t and "BUY" not in t for t in texts)
    for tf in ("1D", "4H", "1H"):  # exactly one alert per timeframe, delivered in priority order
        assert sum(f"<b>{tf}</b>" in t for t in texts) == 1
    assert ["1D" in t for t in texts] == [True, False, False] and "<b>4H</b>" in texts[1]
    assert State.sent[0][0] == f"/bot{SECRET}/sendMessage" and State.sent[0][1]["chat_id"] == "4242"
    # the three timeframes used three different exchange intervals, in 1d -> 4h -> 1h order
    intervals = [i for _, i in State.kline_requests]
    assert {"Min60", "Hour4", "Day1"} <= set(intervals)
    # excluded contracts (stock / stable / delisted / coin-margined) never had candles fetched
    assert {s for s, _ in State.kline_requests} == {"AAA_USDT", "BBB_USDT"}

    State.now_ms += 20 * 60_000  # a second trigger in the same hour
    n_before = len(State.kline_requests)
    assert scanner.main([]) == 0
    assert len(State.sent) == 3  # nothing due, nothing re-sent
    assert len(State.kline_requests) - n_before <= 3  # only the cheap due-probes
    out = capsys.readouterr()
    assert SECRET not in out.out and SECRET not in out.err


def test_next_hour_only_1h_is_due_over_http(server):
    scanner.main([])
    State.sent.clear()
    State.now_ms = BOUNDARY + H + 5 * 60_000  # 01:05 UTC -> only the 1h candle is new
    assert scanner.main([]) == 0
    texts = [p["text"] for _, p in State.sent]
    # AAA's signal candle is the latest candle again in this synthetic feed (new timestamp) -> one 1H alert only
    assert len(texts) == 1 and "<b>1H</b>" in texts[0]


def test_forming_candle_spike_never_triggers_alert(server):
    scanner.main(["--timeframes", "1h"])
    assert all("500" not in p["text"] for _, p in State.sent)


def test_dry_run_needs_no_secrets_and_sends_nothing(server, monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    monkeypatch.delenv("TELEGRAM_CHAT_ID")
    assert scanner.main(["--dry-run", "--timeframes", "1h"]) == 0
    assert State.sent == []
    assert "DRY RUN" in capsys.readouterr().out
    assert not (tmp_path / "sent.json").exists()


def test_telegram_test_mode_sends_one_message_and_no_scan(server):
    assert scanner.main(["--telegram-test"]) == 0
    assert len(State.sent) == 1 and "configuration works" in State.sent[0][1]["text"]
    assert State.kline_requests == []


def test_inspect_lists_signals_without_sending(server, monkeypatch, capsys):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    monkeypatch.delenv("TELEGRAM_CHAT_ID")
    assert scanner.main(["--inspect", "AAAUSDT", "--timeframes", "4h"]) == 0
    out = capsys.readouterr().out
    assert "1 signal(s)" in out and "SELL" in out and State.sent == []
