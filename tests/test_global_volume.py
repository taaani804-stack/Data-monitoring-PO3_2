"""CoinMarketCap client, aggregation (no double counting), ranking and cache."""
import logging
import os
from datetime import datetime, timedelta, timezone

import pytest

import global_volume as gv
from global_volume import (CmcClient, GlobalRanking, GlobalVolumeError, RankingCache, aggregate_assets,
                           build_global_ranking, mexc_listing, rank_assets)
from tests.cmc_fakes import FakeCmcSession, Resp, exch, mk, ok

NOW = datetime(2026, 10, 28, 0, 10, tzinfo=timezone.utc)


def client(session, **kw):
    kw.setdefault("sleep", lambda _s: None)
    kw.setdefault("min_interval_seconds", 0)
    return CmcClient("SECRET-CMC-KEY-123", session=session, **kw)


# ---- aggregation --------------------------------------------------------------------------------
def test_volume_is_summed_across_exchanges_per_cryptocurrency_and_ranked():
    items = [("binance", mk(1, "BTC", 1, 300.0)), ("okx", mk(2, "BTC", 1, 200.0)), ("bybit", mk(3, "BTC", 1, 100.0)),
             ("binance", mk(4, "ETH", 1027, 450.0)), ("okx", mk(5, "SOL", 5426, 50.0))]
    assets, stats = aggregate_assets(items)
    ranked = rank_assets(assets)
    assert [(a["symbol"], a["rank"], a["volume_usd"], a["exchanges"]) for a in ranked] == [
        ("BTC", 1, 600.0, 3), ("ETH", 2, 450.0, 1), ("SOL", 3, 50.0, 1)]
    assert stats.markets_counted == 5 and stats.duplicates == 0


def test_same_market_is_never_counted_twice():
    m = mk(77, "BTC", 1, 500.0)
    assets, stats = aggregate_assets([("binance", m), ("binance", dict(m)), ("okx", dict(m))])
    assert assets[1]["volume_usd"] == 500.0 and stats.duplicates == 2 and stats.markets_counted == 1


def test_open_interest_and_exchange_reported_volume_are_not_used():
    big_oi = mk(1, "AAA", 10, 100.0, oi=9e12, reported=9e15)
    other = mk(2, "BBB", 11, 200.0, oi=1, reported=1)
    ranked = rank_assets(aggregate_assets([("x", big_oi), ("x", other)])[0])
    assert [a["symbol"] for a in ranked] == ["BBB", "AAA"]
    assert ranked[1]["volume_usd"] == 100.0


def test_ineligible_markets_are_skipped_with_reasons():
    items = [("x", mk(1, "GOOD", 1, 10.0)),
             ("x", mk(2, "OUT", 2, 999.0, outlier=True)),
             ("x", mk(3, "EXC", 3, 999.0, exclusions=["wash"])),
             ("x", mk(4, "TSLA", 4, 999.0, ctype="fiat")),          # not a CMC cryptocurrency
             ("x", mk(5, "USDC", 825, 999.0)),                       # stablecoin base
             ("x", mk(6, "SPOT", 6, 999.0, category="spot")),        # spot market is never futures volume
             ("x", mk(7, "OPT", 7, 999.0, category="options")),
             ("x", {**mk(8, "NOUSD", 8, 1.0), "quotes": []}),         # no USD volume -> not invented
             ("x", {**mk(9, "NOID", 9, 1.0), "market_pair_base": {"symbol": "NOID", "currency_type": "cryptocurrency"}}),
             ("x", "garbage")]
    assets, stats = aggregate_assets(items)
    assert set(a["symbol"] for a in assets.values()) == {"GOOD"}
    assert stats.skipped["outlier_detected"] == 1 and stats.skipped["exclusions present"] == 1
    assert stats.skipped["base is not a CMC-identified cryptocurrency"] == 2 and stats.skipped["no USD volume"] == 1
    assert stats.skipped["malformed market"] == 1 and stats.skipped["stablecoin / leveraged / empty symbol"] == 1


def test_perpetual_only_category_option():
    items = [("x", mk(1, "AAA", 1, 10.0)), ("x", mk(2, "AAA", 1, 5.0, category="futures"))]
    assert aggregate_assets(items, ("perpetual",))[0][1]["volume_usd"] == 10.0
    assert aggregate_assets(items, ("perpetual", "futures"))[0][1]["volume_usd"] == 15.0


def test_mexc_listing_maps_exchange_symbols_to_cmc_ids():
    listing = mexc_listing([mk(1, "PEPE", 24478, 1.0, exch_sym="1000PEPE"), mk(2, "BTC", 1, 1.0),
                            mk(3, "TSLA", 99, 1.0, ctype="fiat")])
    assert listing["1000PEPE_USDT"]["crypto_id"] == 24478 and listing["BTC_USDT"]["category"] == "perpetual"
    assert "TSLA_USDT" not in listing


# ---- full ranking build ----------------------------------------------------------------------------
def world():
    exchanges = [exch(1, "binance", 600.0), exch(2, "okx", 300.0), exch(3, "bybit", 200.0), exch(4, "gate", 50.0)]
    markets = {1: [mk(1, "BTC", 1, 400.0), mk(2, "ETH", 1027, 150.0)],
               2: [mk(3, "BTC", 1, 150.0), mk(4, "SOL", 5426, 100.0)],
               3: [mk(5, "ETH", 1027, 120.0), mk(6, "BTC", 1, 10.0, outlier=True)],
               4: [mk(7, "DOGE", 74, 30.0)],
               9999: [mk(8, "BTC", 1, 60.0), mk(9, "SOL", 5426, 20.0)]}  # MEXC itself (not among the top exchanges)
    return exchanges, markets


def test_build_ranking_aggregates_ranks_and_reports_partial_coverage():
    ex, mk_ = world()
    sess = FakeCmcSession(ex, mk_)
    r = build_global_ranking(client(sess), max_exchanges=3, pairs_per_exchange=1000, now=NOW)
    top = [(a["symbol"], a["rank"], a["volume_usd"]) for a in r.assets]
    assert top == [("BTC", 1, 550.0), ("ETH", 2, 270.0), ("SOL", 3, 100.0)]  # top-3 exchanges only; MEXC not summed in
    assert r.coverage["exchanges_fetched"] == 4 and r.coverage["fetched_volume_share_pct"] == round(1100 / 1150 * 100, 2)
    assert r.provider == "CoinMarketCap" and r.generated_at == NOW.isoformat() and r.assets_received == 3
    assert r.mexc_markets["BTC_USDT"]["crypto_id"] == 1
    urls = [c[0].replace(gv.CMC_BASE_URL, "") for c in sess.calls]
    assert urls.count(gv.EP_EXCHANGES) == 1  # ONE batch call for the exchange list, never per coin
    assert all(u in (gv.EP_EXCHANGES, gv.EP_EXCHANGE_PAIRS) for u in urls)


def test_mexc_is_summed_when_it_is_one_of_the_top_exchanges():
    ex = [exch(1, "binance", 600.0), exch(2, "mexc", 500.0), exch(3, "okx", 400.0), exch(4, "bybit", 300.0)]
    mk_ = {1: [mk(1, "BTC", 1, 10.0)], 2: [mk(2, "BTC", 1, 20.0)], 3: [mk(3, "BTC", 1, 30.0)], 4: [mk(4, "BTC", 1, 40.0)]}
    sess = FakeCmcSession(ex, mk_)
    r = build_global_ranking(client(sess), max_exchanges=4, now=NOW)
    assert r.assets[0]["volume_usd"] == 100.0 and "BTC_USDT" in r.mexc_markets
    assert sum(1 for c in sess.calls if c[1].get("exchange_slug") == "mexc") == 0  # no extra MEXC fetch needed


def test_one_failed_exchange_is_tolerated_but_too_many_failures_abort():
    ex, mk_ = world()
    r = build_global_ranking(client(FakeCmcSession(ex, mk_, failing_ids={3}), max_retries=0), max_exchanges=4, now=NOW)
    assert r.coverage["exchanges_failed"] == ["bybit"]
    with pytest.raises(GlobalVolumeError):
        build_global_ranking(client(FakeCmcSession(ex, mk_, failing_ids={1, 2, 3, 4}), max_retries=0), max_exchanges=4, now=NOW)


def test_credit_guard_stops_runaway_spending():
    ex, mk_ = world()
    with pytest.raises(GlobalVolumeError):
        build_global_ranking(client(FakeCmcSession(ex, mk_), max_credits=3), max_exchanges=4, now=NOW)


def test_empty_exchange_list_is_an_error():
    with pytest.raises(GlobalVolumeError):
        build_global_ranking(client(FakeCmcSession([], {})), now=NOW)


# ---- client: errors, rate limits, secrets ----------------------------------------------------------------
def test_rate_limit_429_is_retried_with_backoff_then_succeeds():
    waits = []
    sess = FakeCmcSession([], {}, script=[Resp(429, {}, {"Retry-After": "7"}), Resp(200, ok({"exchanges": [exch(1, "a", 1.0)]}))])
    c = CmcClient("k", session=sess, sleep=waits.append, min_interval_seconds=0)
    assert len(c.list_exchanges(10)) == 1
    assert max(waits) >= 7 and len(sess.calls) == 2


def test_retries_are_bounded_and_raise():
    sess = FakeCmcSession([], {}, script=[Resp(500, {})] * 10)
    c = client(sess, max_retries=2)
    with pytest.raises(GlobalVolumeError, match="gave up after 3 attempts"):
        c.list_exchanges(5)
    assert len(sess.calls) == 3


@pytest.mark.parametrize("status", [401, 403])
def test_auth_or_plan_errors_are_not_retried(status):
    sess = FakeCmcSession([], {}, script=[Resp(status, {})] * 5)
    with pytest.raises(GlobalVolumeError, match="plan does not include"):
        client(sess).list_exchanges(5)
    assert len(sess.calls) == 1


def test_cmc_error_code_in_body_is_an_error():
    sess = FakeCmcSession([], {}, script=[Resp(200, {"status": {"error_code": 1008, "error_message": "rate limit"}, "data": None})])
    with pytest.raises(GlobalVolumeError, match="1008"):
        client(sess).list_exchanges(5)


def test_missing_key_is_rejected():
    with pytest.raises(GlobalVolumeError):
        CmcClient("   ")


def test_api_key_is_sent_in_header_only_and_never_logged(caplog):
    caplog.set_level(logging.DEBUG)
    ex, mk_ = world()
    sess = FakeCmcSession(ex, mk_)
    c = client(sess)
    build_global_ranking(c, max_exchanges=3, now=NOW)
    assert all(h.get("X-CMC_PRO_API_KEY") == "SECRET-CMC-KEY-123" for _, _, h in sess.calls)
    assert all("SECRET-CMC-KEY-123" not in str(p) and "SECRET" not in u for u, p, _ in sess.calls)
    assert "SECRET-CMC-KEY-123" not in caplog.text and "SECRET-CMC-KEY-123" not in repr(c.__dict__.get("session", ""))
    with pytest.raises(GlobalVolumeError) as ei:
        client(FakeCmcSession([], {}, script=[Resp(401, {})])).list_exchanges(1)
    assert "SECRET" not in str(ei.value)


# ---- cache -----------------------------------------------------------------------------------------
def _ranking(age_h=0.0):
    ex, mk_ = world()
    return build_global_ranking(client(FakeCmcSession(ex, mk_)), max_exchanges=3, now=NOW - timedelta(hours=age_h))


def test_cache_roundtrip_age_and_atomic_write(tmp_path):
    cache = RankingCache(str(tmp_path / "d" / "rank.json"))
    assert cache.load() is None
    r = _ranking(5.0)
    cache.save(r)
    loaded = cache.load()
    assert loaded.assets == r.assets and loaded.mexc_markets == r.mexc_markets
    assert loaded.age_hours(NOW) == pytest.approx(5.0)
    assert not [f for f in os.listdir(tmp_path / "d") if f.endswith(".tmp")]


@pytest.mark.parametrize("content", ["{broken", "[]", '{"version": 99}', '{"version": 1}'])
def test_corrupt_or_foreign_cache_is_ignored(tmp_path, content):
    p = tmp_path / "rank.json"
    p.write_text(content)
    assert RankingCache(str(p)).load() is None
