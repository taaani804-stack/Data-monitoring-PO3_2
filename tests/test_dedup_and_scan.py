"""Scan-level behaviour: SHORT-only, lookback windows, duplicates, Telegram failure handling, state store."""
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest

import scanner
from config import ScannerConfig
from po3_strategy import PO3Config, legacy_signal_id, signal_id
from state_store import SentSignalStore
from tests.helpers import (BOUNDARY, FakeClient, FakeSender, TIMEFRAME_MS, base_series, buy_series, candle,
                           contract, quiet_after, retime, sell_series)

CFG = ScannerConfig(request_delay_seconds=0, close_buffer_seconds=0)


def scan(tmp_path, candles, tf, sender, now_ms=None, dry_run=False, contracts=None, store=None, last_close=BOUNDARY):
    tf_ms = TIMEFRAME_MS[tf]
    series = retime(candles, tf_ms, last_close)
    now_ms = now_ms if now_ms is not None else last_close + 10_000
    client = FakeClient({tf: series})
    store = store if store is not None else SentSignalStore(str(tmp_path / "sent.json"))
    summary = scanner.run_scan(client, sender, store, CFG, PO3Config(), contracts or [contract()], now_ms, tf,
                               dry_run=dry_run, sleep=lambda _s: None)
    return summary, series, client


# ---- SHORT only ------------------------------------------------------------------------------
@pytest.mark.parametrize("tf", ["1h", "4h", "1d"])
def test_bearish_setup_sends_one_short_signal(tmp_path, tf):
    s = FakeSender()
    summary, series, _ = scan(tmp_path, sell_series(), tf, s)
    assert summary.sent == 1 and summary.valid_short == 1 and summary.setups_found == 1
    msg = s.messages[0]
    assert "SHORT SIGNAL" in msg and "SELL / SHORT" in msg and f"<b>{tf.upper()}</b>" in msg
    assert "BUY" not in msg and "LONG" not in msg.upper().replace("<B>", "")


@pytest.mark.parametrize("tf", ["1h", "4h", "1d"])
def test_bullish_setup_never_alerts(tmp_path, tf):
    s = FakeSender()
    summary, _, _ = scan(tmp_path, buy_series(), tf, s)
    assert s.messages == [] and summary.sent == 0
    assert summary.bullish_ignored == 1 and summary.setups_found == 0


def test_no_setup_no_signal(tmp_path):
    s = FakeSender()
    summary, _, _ = scan(tmp_path, base_series(), "1h", s)
    assert s.messages == [] and summary.setups_found == 0 and summary.checked == 1


def test_bullish_trade_still_blocks_next_cycle_like_pine(tmp_path):
    """A BUY entry that has not exited keeps the PO3 cycle busy, so no SELL can follow inside it."""
    s = FakeSender()
    summary, _, _ = scan(tmp_path, buy_series(quiet_after(1)), "1h", s)
    assert s.messages == []


def test_defensive_buy_is_refused_by_formatter_and_validator():
    from short_signals import SignalRejected, validate_short
    from telegram_bot import format_signal_message
    sig = scanner.run_po3(buy_series())[0]
    with pytest.raises(SignalRejected):
        validate_short(sig, contract())
    with pytest.raises(ValueError):
        format_signal_message(sig, "AAA_USDT", "1h", 3_600_000)


# ---- lookback windows: 1h = 7 candles, 4h/1d = 2 candles ---------------------------------------
@pytest.mark.parametrize("tf,lookback", [("1h", 7), ("4h", 2), ("1d", 2)])
def test_lookback_window_boundaries(tmp_path, tf, lookback):
    # signal candle is index 60; with k quiet candles after it, it is the (k+1)-th most recent candle
    inside = FakeSender()
    s_in, _, _ = scan(tmp_path / "a", sell_series(quiet_after(lookback - 1)), tf, inside)
    assert s_in.sent == 1, f"{tf}: signal {lookback} candles back must still alert"
    outside = FakeSender()
    s_out, _, _ = scan(tmp_path / "b", sell_series(quiet_after(lookback)), tf, outside)
    assert s_out.sent == 0 and s_out.skipped_stale == 1, f"{tf}: signal {lookback + 1} candles back must not alert"


def test_each_timeframe_requests_its_own_interval(tmp_path):
    for tf in ("1h", "4h", "1d"):
        _, _, client = scan(tmp_path / tf, sell_series(), tf, FakeSender())
        assert client.kline_calls == [("AAA_USDT", tf)]
    from mexc_data import INTERVALS
    assert (INTERVALS["1h"], INTERVALS["4h"], INTERVALS["1d"]) == ("Min60", "Hour4", "Day1")


def test_forming_candle_never_creates_signal(tmp_path):
    s = FakeSender()
    # now is in the middle of the signal candle -> it is still forming
    series = retime(sell_series(), 3_600_000)
    mid = series[-1].open_time_ms + 5 * 60_000
    summary, _, _ = scan(tmp_path, sell_series(), "1h", s, now_ms=mid)
    assert s.messages == [] and summary.setups_found == 0


def test_already_resolved_trade_is_skipped(tmp_path):
    s = FakeSender()
    # next candle drops below TP (95.94) -> trade resolved, stale idea
    summary, _, _ = scan(tmp_path, sell_series([candle(61, 100, 100.5, 94, 95)]), "1h", s)
    assert s.messages == [] and summary.skipped_resolved == 1


def test_stale_or_missing_data_is_skipped_not_crashing(tmp_path):
    s = FakeSender()
    summary, _, _ = scan(tmp_path, sell_series(), "1h", s, now_ms=BOUNDARY + 10 * 3_600_000)
    assert summary.skipped_data == 1 and s.messages == []
    summary, _, _ = scan(tmp_path / "x", base_series()[:20], "1h", s)
    assert summary.skipped_data == 1


def test_fetch_error_for_one_pair_does_not_stop_others(tmp_path):
    cs = [contract("BAD_USDT"), contract("AAA_USDT")]
    client = FakeClient({"1h": retime(sell_series(), 3_600_000)})
    client.fail_symbols = {"BAD_USDT"}
    store = SentSignalStore(str(tmp_path / "s.json"))
    s = FakeSender()
    summary = scanner.run_scan(client, s, store, CFG, PO3Config(), cs, BOUNDARY + 10_000, "1h", sleep=lambda _x: None)
    assert summary.fetch_errors == 1 and summary.checked == 1 and summary.sent == 1
    assert not summary.is_complete(0.8)  # 1/2 checked: not reported as a full scan


def test_unexpected_exception_in_one_symbol_is_contained(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(scanner, "find_short_candidates", boom)
    summary, _, _ = scan(tmp_path, sell_series(), "1h", FakeSender())
    assert summary.internal_errors == 1


# ---- duplicates ---------------------------------------------------------------------------------
def test_same_scan_twice_sends_once_and_survives_restart(tmp_path):
    s1, s2 = FakeSender(), FakeSender()
    assert scan(tmp_path, sell_series(), "1h", s1)[0].sent == 1
    assert (tmp_path / "sent.json").exists()
    # new process / new store object, later in the same lookback window
    sm2, _, _ = scan(tmp_path, sell_series(), "1h", s2, now_ms=BOUNDARY + 70 * 60_000)
    assert s2.messages == [] and sm2.duplicates == 1


def test_multiple_short_alerts_are_spaced_by_15_seconds(tmp_path):
    """When multiple contracts signal in one timeframe, Telegram sends are paced by 15 seconds."""
    cs = [contract("AAA_USDT"), contract("BBB_USDT")]
    client = FakeClient({"1h": retime(sell_series(), TIMEFRAME_MS["1h"])})
    sender = FakeSender()
    sleeps = []
    store = SentSignalStore(str(tmp_path / "paced.json"))
    summary = scanner.run_scan(
        client, sender, store, CFG, PO3Config(), cs, BOUNDARY + 10_000, "1h",
        sleep=sleeps.append,
    )
    assert summary.sent == 2
    assert len(sender.messages) == 2
    assert 15 in sleeps


def test_failed_send_is_not_recorded_and_is_retried(tmp_path):
    bad = FakeSender([False])
    r1, _, _ = scan(tmp_path, sell_series(), "1h", bad)
    assert r1.send_failures == 1 and r1.sent == 0 and not r1.is_complete(0.8)
    assert not (tmp_path / "sent.json").exists() or len(SentSignalStore(str(tmp_path / "sent.json"))) == 0
    good = FakeSender([True])
    r2, _, _ = scan(tmp_path, sell_series(), "1h", good)
    assert r2.sent == 1


def test_new_setup_on_a_new_candle_sends_again(tmp_path):
    s = FakeSender()
    assert scan(tmp_path, sell_series(), "1h", s)[0].sent == 1
    # a later, genuinely new setup has a different signal-candle timestamp -> different id -> alerts
    later = BOUNDARY + 5 * 3_600_000
    sm, _, _ = scan(tmp_path, sell_series(), "1h", s, last_close=later)
    assert sm.sent == 1 and sm.duplicates == 0 and len(s.messages) == 2


def test_timeframes_are_tracked_independently(tmp_path):
    s = FakeSender()
    store = SentSignalStore(str(tmp_path / "sent.json"))
    for tf in ("1h", "4h", "1d"):
        assert scan(tmp_path, sell_series(), tf, s, store=store)[0].sent == 1
    assert len(s.messages) == 3 and len(store) == 3
    for tf in ("1h", "4h", "1d"):
        assert scan(tmp_path, sell_series(), tf, s, store=store)[0].duplicates == 1
    assert len(s.messages) == 3


def test_legacy_v1_signal_id_still_suppresses(tmp_path):
    series = retime(sell_series(), 3_600_000)
    store = SentSignalStore(str(tmp_path / "sent.json"))
    store.add(legacy_signal_id("AAA_USDT", "1h", "SELL", series[60].open_time_ms))
    s = FakeSender()
    sm, _, _ = scan(tmp_path, sell_series(), "1h", s, store=store)
    assert s.messages == [] and sm.duplicates == 1


def test_signal_id_contains_exchange_pair_timeframe_direction_candle():
    sid = signal_id("BTC_USDT", "4h", "SELL", 1000)
    assert sid == "PO3|MEXC|BTC_USDT|4h|SELL|1000"
    assert len({sid, signal_id("ETH_USDT", "4h", "SELL", 1000), signal_id("BTC_USDT", "1h", "SELL", 1000),
                signal_id("BTC_USDT", "4h", "SELL", 2000)}) == 4


def test_dry_run_prints_but_does_not_persist(tmp_path):
    s = FakeSender()
    summary, _, _ = scan(tmp_path, sell_series(), "1h", s, dry_run=True)
    assert summary.sent == 1 and not (tmp_path / "sent.json").exists()


# ---- state store ---------------------------------------------------------------------------------
def test_corrupt_state_file_does_not_crash(tmp_path):
    p = tmp_path / "sent.json"
    p.write_text("{not json")
    assert len(SentSignalStore(str(p))) == 0


def test_prune_removes_only_old_entries(tmp_path):
    st = SentSignalStore(str(tmp_path / "s.json"), retention_days=14)
    now = datetime(2026, 10, 18, tzinfo=timezone.utc)
    st.add("old", now - timedelta(days=15))
    st.add("new", now - timedelta(days=1))
    assert st.prune(now) == 1
    assert "old" not in st and "new" in st


def test_save_is_atomic_and_roundtrips(tmp_path):
    p = str(tmp_path / "deep" / "s.json")
    st = SentSignalStore(p)
    st.add("x")
    st.mark_scanned("4h", 12345)
    st.save()
    st2 = SentSignalStore(p)
    assert "x" in st2 and st2.last_scanned_candle("4h") == 12345 and st2.last_scanned_candle("1h") is None
    assert not [f for f in (tmp_path / "deep").iterdir() if f.suffix == ".tmp"]


def test_scan_progress_never_moves_backwards(tmp_path):
    st = SentSignalStore(str(tmp_path / "s.json"))
    st.mark_scanned("1h", 200)
    st.mark_scanned("1h", 100)
    assert st.last_scanned_candle("1h") == 200


def test_two_processes_saving_same_file_merge_instead_of_overwriting(tmp_path):
    p = str(tmp_path / "s.json")
    a, b = SentSignalStore(p), SentSignalStore(p)  # both loaded the same (empty) state
    a.add("sig-a"); a.mark_scanned("1h", 10)
    b.add("sig-b"); b.mark_scanned("4h", 20)
    ts = [threading.Thread(target=a.save), threading.Thread(target=b.save)]
    [t.start() for t in ts]; [t.join() for t in ts]
    final = SentSignalStore(p)
    assert "sig-a" in final and "sig-b" in final
    assert final.last_scanned_candle("1h") == 10 and final.last_scanned_candle("4h") == 20
    json.loads(open(p).read())  # valid JSON


def test_version1_state_file_loads(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"version": 1, "sent": {"old-id": {"sent_at": datetime.now(timezone.utc).isoformat()}}}))
    st = SentSignalStore(str(p))
    assert "old-id" in st and st.last_scanned_candle("1h") is None


# ---- configuration -------------------------------------------------------------------------------
def test_main_without_telegram_env_exits_2_with_safe_message(monkeypatch, capsys):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert scanner.main(["--symbols", "BTCUSDT"]) == 2
    err = capsys.readouterr().err
    assert "TELEGRAM_BOT_TOKEN" in err and "TELEGRAM_CHAT_ID" in err


def test_scanner_config_defaults_and_validation():
    from models import ConfigError
    with pytest.raises(ConfigError):
        ScannerConfig.from_env({"TOP_N_SYMBOLS": "abc"})
    c = ScannerConfig.from_env({})
    assert c.top_n == 100 and c.timeframes == ("1d", "4h", "1h") and c.scan_gap_seconds == 600
    assert (c.lookback_candles["1h"], c.lookback_candles["4h"], c.lookback_candles["1d"]) == (7, 2, 2)
    assert ScannerConfig.from_env({"LOOKBACK_CANDLES_1H": "6"}).lookback_candles["1h"] == 6


def test_futures_symbol_normalization():
    assert scanner.normalize_symbol("BTCUSDT") == "BTC_USDT"
    assert scanner.normalize_symbol("btc_usdt") == "BTC_USDT"
    assert scanner.normalize_symbol(" ETHUSDT ") == "ETH_USDT"
