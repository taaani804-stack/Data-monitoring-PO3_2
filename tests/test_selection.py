"""Steps D-G (top-100, MEXC verification, strict asset-class filtering), resolver cache/fallback policy."""
import logging
from datetime import datetime, timedelta, timezone

import pytest

import orchestrator
from config import ScannerConfig
from global_volume import GlobalRanking, GlobalVolumeError, RankingCache
from mexc_data import filter_mexc_contracts
from models import ContractInfo
from po3_strategy import PO3Config
from selection import ContractResolver, GlobalRankingUnavailable, select_global_contracts
from state_store import SentSignalStore
from tests.helpers import BOUNDARY, FakeClient, FakeSender, retime, sell_series

NOW = datetime(2026, 10, 28, 0, 10, tzinfo=timezone.utc)
CFG = ScannerConfig(min_mexc_turnover_usdt=1_000.0, request_delay_seconds=0, close_buffer_seconds=0)


def mexc(base, quote="USDT", settle="USDT", ft=1, state=0, **extra):
    d = {"symbol": f"{base}_{quote}", "baseCoin": base, "quoteCoin": quote, "settleCoin": settle, "futureType": ft,
         "state": state, "priceUnit": 0.01, "priceScale": 2, "type": 1, "baseCoinName": base}
    d.update(extra)
    return d


def tick(sym, amount=5e6, bid=99.99, ask=100.01):
    return {"symbol": sym, "lastPrice": "100", "amount24": str(amount), "bid1": bid, "ask1": ask}


def asset(rank, sym, cid, vol=None):
    return {"crypto_id": cid, "symbol": sym, "volume_usd": vol or 1e9 / rank, "markets": 3, "exchanges": 3, "rank": rank}


def ranking(assets, listing, age_h=0.0):
    return GlobalRanking((NOW - timedelta(hours=age_h)).isoformat(), "CoinMarketCap", ["e1", "e2"], assets, listing,
                         {"exchanges_fetched": 40, "fetched_volume_share_pct": 91.5}, len(assets), 120)


def listing_for(*items):
    return {f"{sym}_USDT": {"crypto_id": cid, "symbol": sym, "category": "perpetual", "quote": "USDT"} for sym, cid in items}


def run_select(contracts, tickers, rk, cfg=CFG):
    ranked, stats = filter_mexc_contracts(contracts, tickers)
    final, excluded, report = select_global_contracts(rk, ranked, cfg)
    return final, excluded, report, stats


# ---- verification / strict asset-class filtering --------------------------------------------------------
def test_only_verified_crypto_usdt_perpetuals_pass_and_each_exclusion_has_a_reason():
    contracts = [
        mexc("BTC"), mexc("ETH"),
        mexc("TSLA"),                                   # stock perp, looks like a normal USDT contract
        mexc("NVDA", state=4, type=2, typeLabel=2),     # MEXC-labelled stock
        mexc("ABC", quote="USDC", settle="USDC"),       # USDC-margined
        mexc("DEF", quote="USD", settle="USD"),         # USD-margined
        mexc("GHI", settle="BTC"),                      # coin-margined
        mexc("JKL", ft=2),                              # delivery future
        mexc("MNO", state=1),                           # delivery state
        mexc("OPT", ft=3),                              # option-like / unknown contract type
        mexc("UNK"),                                    # real-looking contract, unknown to the global provider
        mexc("GLD", conceptPlate=["mc-trade-zone-commodity"]),
        mexc("USDC"),                                   # stablecoin base
    ]
    tickers = [tick(c["symbol"]) for c in contracts] + [tick("BTCUSDT"), tick("ETHUSDT")]  # spot-style tickers ignored
    rk = ranking([asset(1, "BTC", 1), asset(2, "ETH", 1027), asset(3, "TSLA", 777)],  # even if CMC ranked a "TSLA" asset...
                 listing_for(("BTC", 1), ("ETH", 1027)))                              # ...it is NOT listed on MEXC as a crypto market
    final, excluded, report, stats = run_select(contracts, tickers, rk)
    assert [c.symbol for c in final] == ["BTC_USDT", "ETH_USDT"]
    flat = {s: r for r, syms in {**stats.excluded, **excluded}.items() for s in syms}
    assert "TSLA_USDT" in flat and "NVDA_USDT" in flat and "GLD_USDT" in flat and "ABC_USDC" in flat and "DEF_USD" in flat
    assert "absent from CoinMarketCap" in flat["UNK_USDT"]
    assert "known non-crypto" in flat["TSLA_USDT"]  # MEXC-side denylist is a second layer, not the only one
    assert report["final"] == 2 and report["global_assets_valid"] == 3


def test_stock_and_other_non_crypto_never_pass_even_with_matching_symbol_in_symbol_mode():
    # symbol mode is weaker, but the MEXC-side stock/TradFi/suspended filters still run first
    contracts = [mexc("TSLA"), mexc("NVDA", typeLabel=2), mexc("AAPL", type=2), mexc("BTC")]
    tickers = [tick(c["symbol"]) for c in contracts]
    rk = ranking([asset(1, "BTC", 1)], {})
    cfg = ScannerConfig(min_mexc_turnover_usdt=0, crypto_verification="symbol")
    final, excluded, _, stats = run_select(contracts, tickers, rk, cfg)
    assert [c.symbol for c in final] == ["BTC_USDT"]
    mexc_side = {s for syms in stats.excluded.values() for s in syms}
    assert {"TSLA_USDT", "NVDA_USDT", "AAPL_USDT"} <= mexc_side


def test_strict_mode_requires_cmc_to_list_it_as_a_perpetual_usdt_market_on_mexc():
    contracts = [mexc("AAA"), mexc("BBB"), mexc("CCC")]
    rk = ranking([asset(1, "AAA", 1), asset(2, "BBB", 2), asset(3, "CCC", 3)], {
        "AAA_USDT": {"crypto_id": 1, "symbol": "AAA", "category": "futures", "quote": "USDT"},   # dated future per CMC
        "BBB_USDT": {"crypto_id": 2, "symbol": "BBB", "category": "perpetual", "quote": "USD"},   # not USDT per CMC
        "CCC_USDT": {"crypto_id": 3, "symbol": "CCC", "category": "perpetual", "quote": "USDT"}})
    final, excluded, _, _ = run_select(contracts, [tick(c["symbol"]) for c in contracts], rk)
    assert [c.symbol for c in final] == ["CCC_USDT"]
    assert len(excluded["CoinMarketCap lists it on MEXC as non-perpetual / non-USDT"]) == 2


def test_listing_pointing_to_an_unranked_crypto_is_rejected():
    rk = ranking([asset(1, "BTC", 1)], listing_for(("AAA", 424242)))
    final, excluded, _, _ = run_select([mexc("AAA")], [tick("AAA_USDT")], rk)
    assert final == [] and "CoinMarketCap crypto not in the global futures-volume ranking" in excluded


def test_symbol_mode_rejects_ambiguous_symbols():
    rk = ranking([asset(1, "AAA", 1), asset(2, "AAA", 2), asset(3, "BTC", 3)], {})
    cfg = ScannerConfig(min_mexc_turnover_usdt=0, crypto_verification="symbol")
    final, excluded, _, _ = run_select([mexc("AAA"), mexc("BTC")], [tick("AAA_USDT"), tick("BTC_USDT")], rk, cfg)
    assert [c.symbol for c in final] == ["BTC_USDT"] and "AAA_USDT" in sum(excluded.values(), [])


def test_exchange_symbol_prefix_contracts_map_through_cmc_ids():
    rk = ranking([asset(1, "PEPE", 24478)], {"1000PEPE_USDT": {"crypto_id": 24478, "symbol": "PEPE", "category": "perpetual", "quote": "USDT"}})
    final, _, _, _ = run_select([mexc("1000PEPE")], [tick("1000PEPE_USDT")], rk)
    assert final[0].symbol == "1000PEPE_USDT" and final[0].global_rank == 1


# ---- top-100 ----------------------------------------------------------------------------------------------
def big_world(n_assets=150, n_on_mexc=150):
    assets = [asset(i, f"C{i:03d}", i) for i in range(1, n_assets + 1)]
    contracts = [mexc(f"C{i:03d}") for i in range(1, n_on_mexc + 1)]
    return assets, contracts, listing_for(*[(f"C{i:03d}", i) for i in range(1, n_on_mexc + 1)])


def test_top_100_by_global_rank_not_by_mexc_volume():
    assets, contracts, listing = big_world()
    # MEXC turnover is reversed relative to global rank: MEXC volume must NOT decide who is selected
    tickers = [tick(f"C{i:03d}_USDT", amount=1e6 + i * 1000) for i in range(1, 151)]
    final, _, report, _ = run_select(contracts, tickers, ranking(assets, listing))
    assert len(final) == 100 and [c.global_rank for c in final] == list(range(1, 101))
    assert final[0].symbol == "C001_USDT" and final[-1].symbol == "C100_USDT" and report["top_candidates"] == 100


def test_fewer_than_100_when_top_assets_are_missing_on_mexc_and_never_padded():
    assets, contracts, listing = big_world(150, 60)  # only 60 of the top assets trade on MEXC
    final, _, report, _ = run_select(contracts, [tick(c["symbol"]) for c in contracts], ranking(assets, listing))
    assert len(final) == 60 and report["mexc_verified"] == 60


def test_fill_to_n_mode_walks_down_the_ranking():
    assets = [asset(i, f"C{i:03d}", i) for i in range(1, 201)]
    on_mexc = [i for i in range(1, 201) if i % 2 == 0]  # every other asset is on MEXC
    contracts = [mexc(f"C{i:03d}") for i in on_mexc]
    rk = ranking(assets, listing_for(*[(f"C{i:03d}", i) for i in on_mexc]))
    tickers = [tick(c["symbol"]) for c in contracts]
    default, _, _, _ = run_select(contracts, tickers, rk)
    filled, _, _, _ = run_select(contracts, tickers, rk, ScannerConfig(min_mexc_turnover_usdt=0, global_fill_to_n=True))
    assert len(default) == 50 and len(filled) == 100 and filled[-1].global_rank == 200


def test_global_fields_are_attached_to_each_contract():
    rk = ranking([asset(1, "BTC", 1, 12_340_000_000.0)], listing_for(("BTC", 1)))
    final, _, _, _ = run_select([mexc("BTC")], [tick("BTC_USDT")], rk)
    c = final[0]
    assert (c.global_rank, c.global_volume_usd, c.crypto_id, c.ranking_ts) == (1, 12_340_000_000.0, 1, rk.generated_at)
    assert "CoinMarketCap" in c.global_source and "40 exchanges" in c.global_source


# ---- liquidity / data-quality safeguards ------------------------------------------------------------------
def test_liquidity_spread_and_metadata_safeguards():
    contracts = [mexc("AAA"), mexc("BBB"), mexc("CCC"), mexc("DDD", priceUnit=0, priceScale=None), mexc("EEE")]
    tickers = [tick("AAA_USDT", amount=500),                      # below minimum turnover
               tick("BBB_USDT", bid=90, ask=110),                  # 20% spread
               tick("CCC_USDT"),                                   # fine
               tick("DDD_USDT"),                                   # no tick metadata
               {"symbol": "EEE_USDT", "lastPrice": "100", "amount24": "5000000"}]  # no bid/ask -> spread not enforced
    rk = ranking([asset(i, s, i) for i, s in enumerate(["AAA", "BBB", "CCC", "DDD", "EEE"], 1)],
                 listing_for(("AAA", 1), ("BBB", 2), ("CCC", 3), ("DDD", 4), ("EEE", 5)))
    final, excluded, _, _ = run_select(contracts, tickers, rk)
    assert [c.symbol for c in final] == ["CCC_USDT", "EEE_USDT"]
    assert final[1].spread_pct is None  # unavailable, not invented
    reasons = " | ".join(excluded)
    assert "turnover below" in reasons and "spread above" in reasons and "missing tick-size" in reasons


def test_one_contract_per_asset():
    rk = ranking([asset(1, "BTC", 1)], {"BTC_USDT": {"crypto_id": 1, "symbol": "BTC", "category": "perpetual", "quote": "USDT"},
                                       "1000BTC_USDT": {"crypto_id": 1, "symbol": "BTC", "category": "perpetual", "quote": "USDT"}})
    final, excluded, _, _ = run_select([mexc("BTC"), mexc("1000BTC")], [tick("BTC_USDT", 9e6), tick("1000BTC_USDT", 5e6)], rk)
    assert [c.symbol for c in final] == ["BTC_USDT"] and "second MEXC contract" in " ".join(excluded)


def test_no_eligible_contracts_gives_empty_list_without_crash():
    final, _, report, _ = run_select([mexc("TSLA")], [tick("TSLA_USDT")], ranking([asset(1, "BTC", 1)], listing_for(("BTC", 1))))
    assert final == [] and report["final"] == 0


# ---- resolver: cache, refresh and fallback policy --------------------------------------------------------
class StubMexc:
    def __init__(self, contracts, tickers):
        self.contracts, self.tickers = contracts, tickers

    def get_exchange_info(self):
        return self.contracts

    def get_tickers_24h(self):
        return self.tickers


def resolver(tmp_path, cached=None, cmc=None, cfg=None, key=True, now=NOW):
    cfg = cfg or ScannerConfig(min_mexc_turnover_usdt=0)
    cache = RankingCache(str(tmp_path / "rank.json"))
    if cached is not None:
        cache.save(cached)
    calls = []

    def factory():
        calls.append(1)
        return cmc

    stub = StubMexc([mexc("BTC"), mexc("ETH")], [tick("BTC_USDT"), tick("ETH_USDT")])
    return ContractResolver(stub, cfg, cache, factory if key else None, lambda: now), calls, cache


class FakeRefresh:
    """Replaces build_global_ranking via monkeypatch (see fixtures below)."""


@pytest.fixture
def patch_build(monkeypatch):
    state = {"calls": 0, "fail": False, "ranking": None}

    def fake_build(client, *a, **k):
        state["calls"] += 1
        if state["fail"]:
            raise GlobalVolumeError("CMC down")
        return state["ranking"]
    monkeypatch.setattr("selection.build_global_ranking", fake_build)
    return state


RK = lambda age=0.0: ranking([asset(1, "BTC", 1), asset(2, "ETH", 1027)], listing_for(("BTC", 1), ("ETH", 1027)), age)


def test_recent_cache_is_used_without_any_cmc_call(tmp_path, patch_build):
    r, calls, _ = resolver(tmp_path, cached=RK(5.0), cmc=object())
    res = r.resolve()
    assert res.mode == "global-cached" and patch_build["calls"] == 0 and calls == []
    assert "5.0h old" in res.contracts[0].ranking_note and len(res.contracts) == 2


def test_old_cache_triggers_refresh_and_is_saved(tmp_path, patch_build):
    patch_build["ranking"] = RK(0.0)
    r, calls, cache = resolver(tmp_path, cached=RK(30.0), cmc=object())
    res = r.resolve()
    assert res.mode == "global-fresh" and patch_build["calls"] == 1
    assert cache.load().age_hours(NOW) == pytest.approx(0.0)


def test_refresh_failure_with_cache_inside_max_age_uses_it_but_labels_it_stale(tmp_path, patch_build, caplog):
    patch_build["fail"] = True
    r, _, _ = resolver(tmp_path, cached=RK(30.0), cmc=object())
    with caplog.at_level(logging.WARNING):
        res = r.resolve()
    assert res.mode == "global-stale-cache"
    assert all("STALE" in c.ranking_note for c in res.contracts)
    assert "STALE cached global ranking" in caplog.text


def test_cache_older_than_max_age_is_not_used_policy_halt(tmp_path, patch_build):
    patch_build["fail"] = True
    r, _, _ = resolver(tmp_path, cached=RK(100.0), cmc=object())
    with pytest.raises(GlobalRankingUnavailable):
        r.resolve()


def test_no_key_and_no_cache_halts_with_actionable_message(tmp_path):
    r, _, _ = resolver(tmp_path, key=False)
    with pytest.raises(GlobalRankingUnavailable, match="COINMARKETCAP_API_KEY"):
        r.resolve()


def test_policy_mexc_only_falls_back_and_never_pretends_to_be_global(tmp_path, patch_build):
    patch_build["fail"] = True
    cfg = ScannerConfig(min_mexc_turnover_usdt=0, global_fallback_policy="mexc_only")
    r, _, _ = resolver(tmp_path, cfg=cfg, cmc=object())
    res = r.resolve()
    assert res.mode == "mexc-only" and res.ranking is None
    assert all(c.global_rank is None and c.global_volume_usd is None for c in res.contracts)
    assert all("not global" in c.global_source and "unavailable" in c.ranking_note for c in res.contracts)


def test_global_ranking_can_be_disabled_explicitly(tmp_path):
    cfg = ScannerConfig(min_mexc_turnover_usdt=0, global_enabled=False)
    r, _, _ = resolver(tmp_path, cfg=cfg, key=False)
    res = r.resolve()
    assert res.mode == "mexc-only" and "disabled" in res.contracts[0].ranking_note


def test_refresh_if_due_respects_age_slack_and_force(tmp_path, patch_build):
    patch_build["ranking"] = RK(0.0)
    r, _, _ = resolver(tmp_path, cached=RK(22.0), cmc=object())
    assert r.refresh_if_due(slack_hours=3.0) == "refreshed" and patch_build["calls"] == 1   # 22h >= 24h - 3h
    r2, _, _ = resolver(tmp_path / "b", cached=RK(10.0), cmc=object()) if (tmp_path / "b").mkdir() is None else None
    assert r2.refresh_if_due(slack_hours=3.0) == "fresh" and patch_build["calls"] == 1      # 10h < 21h
    assert r2.refresh_if_due(force=True) == "refreshed" and patch_build["calls"] == 2


def test_refresh_if_due_raises_on_failure(tmp_path, patch_build):
    patch_build["fail"] = True
    r, _, _ = resolver(tmp_path, cmc=object())
    with pytest.raises(GlobalVolumeError):
        r.refresh_if_due()


# ---- orchestrator integration -----------------------------------------------------------------------------
class ResolverStub:
    def __init__(self, behaviour):
        self.behaviour = behaviour

    def resolve(self):
        if isinstance(self.behaviour, Exception):
            raise self.behaviour
        return self.behaviour


def test_cycle_logs_every_required_count_and_scans_only_resolved_contracts(tmp_path, caplog):
    assets, contracts, listing = big_world(120, 90)
    rk = ranking(assets, listing)
    ranked, stats = filter_mexc_contracts(contracts + [mexc("TSLA")], [tick(c["symbol"]) for c in contracts] + [tick("TSLA_USDT")])
    final, excluded, report = select_global_contracts(rk, ranked, CFG)
    for reason, syms in excluded.items():
        stats.excluded.setdefault(reason, []).extend(syms)
    stats.selected = len(final)
    from selection import Resolution
    res = Resolution(final, stats, rk, "global-cached", {**report, "mode": "global-cached"})
    series = {tf: retime(sell_series(), ms, (BOUNDARY // ms) * ms) for tf, ms in (("1h", 3_600_000), ("4h", 14_400_000), ("1d", 86_400_000))}
    client = FakeClient(series, now_ms=BOUNDARY + 360_000)
    store = SentSignalStore(str(tmp_path / "s.json"))
    sender = FakeSender()
    with caplog.at_level(logging.INFO):
        out = orchestrator.run_cycle(client, sender, store, ScannerConfig(request_delay_seconds=0, close_buffer_seconds=0, scan_gap_seconds=0),
                                     PO3Config(), forced=["1h"], resolver=ResolverStub(res), sleep=lambda _s: None)
    text = caplog.text
    for needle in ["assets received=120", "valid futures volume=120", "selected top candidates=100", "verified MEXC USDT perpetuals=90",
                   "final contracts scanned=90", "Excluded contracts:", "excluded (", "built 2026-10-28T00:10:00+00:00 UTC"]:
        assert needle in text, needle
    assert {s for s, _ in client.kline_calls} == {c.symbol for c in final} and "TSLA_USDT" not in {s for s, _ in client.kline_calls}
    assert out.summaries[0].selected == len(final)


def test_halt_policy_scans_nothing_sends_nothing_and_fails_the_run(tmp_path):
    client = FakeClient({}, now_ms=BOUNDARY)
    sender = FakeSender()
    out = orchestrator.run_cycle(client, sender, SentSignalStore(str(tmp_path / "s.json")), CFG, PO3Config(),
                                 resolver=ResolverStub(GlobalRankingUnavailable("no ranking")), sleep=lambda _s: None)
    assert out.exit_code == 1 and out.summaries == [] and sender.messages == [] and client.kline_calls == []


def test_empty_final_contract_list_is_reported_not_silently_ok(tmp_path):
    from selection import Resolution
    from models import SelectionStats
    client = FakeClient({}, now_ms=BOUNDARY)
    out = orchestrator.run_cycle(client, FakeSender(), SentSignalStore(str(tmp_path / "s.json")), CFG, PO3Config(),
                                 resolver=ResolverStub(Resolution([], SelectionStats(), None, "mexc-only", {})), sleep=lambda _s: None)
    assert out.exit_code == 1 and out.summaries == []
