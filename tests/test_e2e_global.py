"""End-to-end over real HTTP against local fake MEXC + CoinMarketCap + Telegram servers (no internet)."""
import json
import threading
from http.server import HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

import global_volume
import mexc_data
import scanner
import telegram_bot
from tests.cmc_fakes import exch, mk, ok
from tests.helpers import BOUNDARY
from tests import test_end_to_end_local_server as base

KEY = "E2E-CMC-SECRET-KEY"


class Stats:
    requests = []   # (method, path)
    cmc_calls = 0


class Handler(base.Handler):
    def do_GET(self):
        u = urlparse(self.path)
        Stats.requests.append(("GET", u.path))
        if u.path.startswith("/v5/"):
            return self._cmc(u)
        return super().do_GET()

    def do_POST(self):
        Stats.requests.append(("POST", urlparse(self.path).path))
        return super().do_POST()

    def _cmc(self, u):
        if self.headers.get("X-CMC_PRO_API_KEY") != KEY:
            return self._json({"status": {"error_code": 1002, "error_message": "bad key"}}, 401)
        Stats.cmc_calls += 1
        q = parse_qs(u.query)
        if u.path.endswith("/exchange/derivatives/list"):
            return self._json(ok({"exchanges": [exch(1, "mexc", 500.0), exch(2, "binance", 900.0), exch(3, "okx", 400.0)]}))
        if u.path.endswith("/exchange/derivatives/market-pairs/list/latest"):
            eid = int(q.get("exchange_id", ["1"])[0])
            pairs = {
                1: [mk(1, "AAA", 11, 300.0), mk(2, "BBB", 12, 100.0), mk(3, "TSLA", 900, 50.0, ctype="fiat")],  # MEXC's own list
                2: [mk(4, "AAA", 11, 500.0), mk(5, "BBB", 12, 200.0), mk(6, "TSLA", 900, 999.0, ctype="fiat")],
                3: [mk(7, "AAA", 11, 100.0), mk(8, "BBB", 12, 50.0)],
            }[eid]
            return self._json(ok({"market_pairs": pairs}, credits=2))
        self._json({"msg": "nope"}, 404)


@pytest.fixture
def world(monkeypatch, tmp_path):
    Stats.requests, Stats.cmc_calls = [], 0
    base.State.sent, base.State.kline_requests, base.State.now_ms = [], [], BOUNDARY + 6 * 60_000
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}"
    monkeypatch.setattr(mexc_data, "BASE_URL", url)
    monkeypatch.setattr(telegram_bot, "API_URL", url)
    monkeypatch.setattr(global_volume, "CMC_BASE_URL", url)
    for k, v in {"TELEGRAM_BOT_TOKEN": base.SECRET, "TELEGRAM_CHAT_ID": "4242", "COINMARKETCAP_API_KEY": KEY,
                 "STATE_PATH": str(tmp_path / "sent.json"), "RANKING_CACHE_PATH": str(tmp_path / "rank.json"),
                 "SCAN_GAP_SECONDS": "0", "CMC_MIN_INTERVAL_SECONDS": "0", "GLOBAL_MAX_EXCHANGES": "3",
                 "MIN_MEXC_TURNOVER_USDT": "1000"}.items():
        monkeypatch.setenv(k, v)
    yield tmp_path
    srv.shutdown()


def test_global_ranking_drives_selection_and_alerts_carry_real_global_data(world, capsys, caplog):
    caplog.set_level("DEBUG")
    assert scanner.main([]) == 0
    texts = [p["text"] for _, p in base.State.sent]
    assert len(texts) == 3 and all("AAA_USDT" in t and "SHORT SIGNAL" in t for t in texts)
    t = texts[0]
    assert "Global futures-volume rank:</b> #1" in t and "24h futures volume:</b> $900\n" in t        # AAA: 300+500+100 USD across 3 exchanges
    assert "CoinMarketCap derivatives, 3 exchanges" in t and "refreshed this run" in t
    # only AAA & BBB (CMC-verified crypto perps on MEXC) were scanned; TSLA / USDC / DEAD / COINM never fetched
    assert {s for s, _ in base.State.kline_requests} == {"AAA_USDT", "BBB_USDT"}
    assert (world / "rank.json").exists()
    ranking = json.loads((world / "rank.json").read_text())
    assert [(a["symbol"], a["volume_usd"]) for a in ranking["assets"]] == [("AAA", 900.0), ("BBB", 350.0)]  # TSLA (fiat) never ranked
    # second run in the same hour: cache used, ZERO further CoinMarketCap calls, nothing re-sent
    first_calls = Stats.cmc_calls
    base.State.now_ms += 20 * 60_000
    assert scanner.main([]) == 0
    assert Stats.cmc_calls == first_calls and len(base.State.sent) == 3
    # secrets never leak
    out = capsys.readouterr()
    for secret in (KEY, base.SECRET):
        assert secret not in out.out and secret not in out.err and secret not in caplog.text
        assert all(secret not in x for x in texts)


def test_no_order_placement_traffic_only_public_gets_and_telegram_posts(world):
    scanner.main([])
    methods = {m for m, _ in Stats.requests}
    assert methods == {"GET", "POST"}
    for method, path in Stats.requests:
        if method == "POST":
            assert path == f"/bot{base.SECRET}/sendMessage"
        else:
            assert path.startswith("/api/v1/contract/") or path.startswith("/v5/exchange/derivatives/")
        assert not any(w in path.lower() for w in ("order", "private", "account", "position", "trade"))


def test_source_code_contains_no_order_or_private_api_usage():
    import pathlib, re
    root = pathlib.Path(__file__).resolve().parent.parent
    pat = re.compile(r"(/private/|submit_order|place_order|create_order|api/v1/private|order/submit|\.post\(\s*[\"']https://contract\.mexc)", re.I)
    for f in list(root.glob("*.py")):
        assert not pat.search(f.read_text()), f.name


def test_halt_without_key_or_cache_exits_1_and_sends_nothing(world, monkeypatch):
    monkeypatch.delenv("COINMARKETCAP_API_KEY")
    assert scanner.main([]) == 1
    assert base.State.sent == [] and base.State.kline_requests == []


def test_wrong_key_falls_to_halt_policy(world, monkeypatch):
    monkeypatch.setenv("COINMARKETCAP_API_KEY", "WRONG-KEY")
    assert scanner.main([]) == 1 and base.State.sent == []


def test_refresh_ranking_mode_and_force(world, capsys):
    assert scanner.main(["--refresh-ranking"]) == 0 and "refreshed" in capsys.readouterr().out
    calls = Stats.cmc_calls
    assert scanner.main(["--refresh-ranking"]) == 0 and "fresh" in capsys.readouterr().out and Stats.cmc_calls == calls
    assert scanner.main(["--refresh-ranking", "--force"]) == 0 and Stats.cmc_calls > calls
    assert base.State.sent == [] and base.State.kline_requests == []  # refresh mode never scans or messages


def test_list_contracts_shows_selection_and_every_exclusion_reason(world, capsys):
    assert scanner.main(["--list-contracts"]) == 0
    out = capsys.readouterr().out
    assert "AAA_USDT" in out and "BBB_USDT" in out and "EXCLUDED:" in out
    for needle in ("TSLA_USDT", "DEAD_USDT", "COINM_USDT", "USDC_USDT", "known non-crypto"):
        assert needle in out, needle
    assert base.State.sent == []


def test_dry_run_with_global_ranking_prints_alert_and_sends_nothing(world, capsys):
    assert scanner.main(["--dry-run", "--timeframes", "1h"]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "Global futures-volume rank:</b> #1" in out and base.State.sent == []
