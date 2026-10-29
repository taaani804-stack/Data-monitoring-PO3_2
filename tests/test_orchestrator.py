"""Scheduling: due-detection from candle timestamps, 1d -> 4h -> 1h ordering, ~10 min gaps, retries."""
import pathlib

import yaml

import orchestrator
from config import ScannerConfig
from orchestrator import determine_due, parse_forced_timeframes, run_cycle
from po3_strategy import PO3Config
from state_store import SentSignalStore
from tests.helpers import BOUNDARY, DAY, FakeClient, FakeSender, TIMEFRAME_MS, contract, retime, sell_series, base_series

CFG = ScannerConfig(request_delay_seconds=0, close_buffer_seconds=0)
H = 3_600_000


def make_client(now_ms, sell=True, boundary=BOUNDARY):
    """Each timeframe's series ends with the candle that closed at the latest boundary for that tf."""
    series = {}
    for tf, ms in (("1h", H), ("4h", 4 * H), ("1d", DAY)):
        last_close = (now_ms // ms) * ms
        series[tf] = retime(sell_series() if sell else base_series(), ms, last_close)
    return FakeClient(series, now_ms=now_ms)


def cycle(tmp_path, client, sender=None, now_ms=None, **kw):
    sleeps = []
    store = kw.pop("store", None) or SentSignalStore(str(tmp_path / "s.json"))
    res = run_cycle(client, sender or FakeSender(), store, CFG, PO3Config(), sleep=sleeps.append,
                    now_fn=lambda: client.now_ms, **kw)
    return res, sleeps, store


def scan_order(res):
    return [s.timeframe for s in res.summaries]


def test_all_three_due_at_midnight_runs_1d_then_4h_then_1h_with_10min_gaps(tmp_path):
    client = make_client(BOUNDARY + 6 * 60_000)  # cron fired 6 min after 00:00 UTC
    res, sleeps, _ = cycle(tmp_path, client)
    assert res.due == ["1d", "4h", "1h"] and scan_order(res) == ["1d", "4h", "1h"]
    assert [x for x in sleeps if x >= 600] == [600, 600]  # exactly two 10-minute waits, none before the first scan
    assert res.exit_code == 0


def test_scans_never_overlap_each_other(tmp_path):
    client = make_client(BOUNDARY + 6 * 60_000)
    events = []
    orig_klines, orig_sleep = client.get_klines, None
    client.get_klines = lambda s, tf, l: (events.append(("scan", tf)), orig_klines(s, tf, l))[1]
    res, sleeps, _ = cycle(tmp_path, client)
    seq = [tf for kind, tf in events]
    # all fetches of one timeframe are contiguous blocks (no interleaving) after the due-detection probes
    scans = [tf for i, tf in enumerate(seq) if i == 0 or seq[i - 1] != tf]
    assert scans[-3:] == ["1d", "4h", "1h"] or scans[-3:] == ["1d", "4h", "1h"][-3:]


def test_four_hour_cycle_runs_4h_then_1h(tmp_path):
    t = BOUNDARY + 4 * H + 5 * 60_000  # 04:05 UTC: 4h + 1h due, 1d not (already scanned at midnight)
    store = SentSignalStore(str(tmp_path / "s.json"))
    store.mark_scanned("1d", BOUNDARY - DAY)  # the daily candle that closed at BOUNDARY
    res, sleeps, _ = cycle(tmp_path, make_client(t), store=store)
    assert res.due == ["4h", "1h"] and scan_order(res) == ["4h", "1h"] and [x for x in sleeps if x >= 600] == [600]


def test_plain_hourly_cycle_runs_only_1h(tmp_path):
    t = BOUNDARY + 5 * H + 5 * 60_000  # 05:05 UTC
    store = SentSignalStore(str(tmp_path / "s.json"))
    store.mark_scanned("1d", BOUNDARY - DAY)
    store.mark_scanned("4h", BOUNDARY + 4 * H - 4 * H)  # the 4h candle opened 00:00.. closed 04:00 -> opens at 00:00
    res, sleeps, _ = cycle(tmp_path, make_client(t), store=store)
    assert res.due == ["1h"] and [x for x in sleeps if x >= 600] == []


def test_same_scheduled_scan_is_not_executed_twice(tmp_path):
    t = BOUNDARY + 6 * 60_000
    client = make_client(t)
    store = SentSignalStore(str(tmp_path / "s.json"))
    sender = FakeSender()
    r1, _, _ = cycle(tmp_path, client, sender, store=store)
    assert len(r1.summaries) == 3 and len(sender.messages) == 3
    store2 = SentSignalStore(str(tmp_path / "s.json"))  # next process
    client.now_ms = t + 20 * 60_000  # a second tick in the same hour (delayed duplicate trigger)
    r2, _, _ = cycle(tmp_path, client, sender, store=store2)
    assert r2.due == [] and r2.summaries == [] and len(sender.messages) == 3


def test_late_cron_still_picks_up_due_scans(tmp_path):
    client = make_client(BOUNDARY + 50 * 60_000)  # GitHub started the run 50 minutes late
    res, _, _ = cycle(tmp_path, client)
    assert scan_order(res) == ["1d", "4h", "1h"]


def test_1d_newly_opened_candle_is_not_treated_as_completed(tmp_path):
    # just before midnight the day candle of "today" is still forming: probe sees yesterday's as the latest closed
    now = BOUNDARY - 60_000
    client = make_client(now)
    store = SentSignalStore(str(tmp_path / "s.json"))
    store.mark_scanned("1d", BOUNDARY - 2 * DAY)  # candle that closed at BOUNDARY - DAY
    due = determine_due(client, store, CFG, "AAA_USDT", now)
    assert "1d" not in due


def test_failed_telegram_keeps_timeframe_due_and_exit_code_nonzero(tmp_path):
    t = BOUNDARY + 6 * 60_000
    client = make_client(t)
    store = SentSignalStore(str(tmp_path / "s.json"))
    res, _, _ = cycle(tmp_path, client, FakeSender([False, False, False]), store=store)
    assert res.exit_code == 1 and set(res.failed) == {"1d", "4h", "1h"}
    assert store.last_scanned_candle("1h") is None
    # retry on the next cycle delivers (state registry is empty so nothing is lost)
    good = FakeSender()
    r2, _, _ = cycle(tmp_path, client, good, store=SentSignalStore(str(tmp_path / "s.json")))
    assert r2.exit_code == 0 and len(good.messages) == 3


def test_incomplete_scan_is_not_reported_as_success(tmp_path):
    t = BOUNDARY + 6 * 60_000
    client = make_client(t)
    client.contracts = [contract("AAA_USDT"), contract("BAD_USDT")]
    client.fail_symbols = {"BAD_USDT"}
    res, _, store = cycle(tmp_path, client)
    assert res.exit_code == 1 and store.last_scanned_candle("1h") is None


def test_dry_run_does_not_touch_state(tmp_path):
    client = make_client(BOUNDARY + 6 * 60_000)
    res, _, store = cycle(tmp_path, client, dry_run=True)
    assert len(res.summaries) == 3 and not (tmp_path / "s.json").exists()


def test_forced_timeframes_parse_in_priority_order():
    assert parse_forced_timeframes("1h,1d") == ["1d", "1h"]
    assert parse_forced_timeframes("all") == ["1d", "4h", "1h"]
    import pytest
    with pytest.raises(ValueError):
        parse_forced_timeframes("2h")


def test_forced_run_ignores_due_state(tmp_path):
    client = make_client(BOUNDARY + 5 * H + 300_000)
    store = SentSignalStore(str(tmp_path / "s.json"))
    for tf, ms in (("1d", DAY), ("4h", 4 * H), ("1h", H)):
        store.mark_scanned(tf, (client.now_ms // ms) * ms)
    res, _, _ = cycle(tmp_path, client, store=store, forced=["4h"])
    assert scan_order(res) == ["4h"]


def test_state_sync_command_runs_after_each_scan(tmp_path):
    marker = tmp_path / "sync.log"
    cfg = ScannerConfig(request_delay_seconds=0, close_buffer_seconds=0, state_sync_command=f"echo x >> {marker}")
    client = make_client(BOUNDARY + 6 * 60_000)
    store = SentSignalStore(str(tmp_path / "s.json"))
    run_cycle(client, FakeSender(), store, cfg, PO3Config(), sleep=lambda _s: None, now_fn=lambda: client.now_ms)
    assert marker.read_text().count("x") == 3


def test_contract_list_failure_fails_the_cycle(tmp_path):
    from mexc_data import MexcApiError
    client = make_client(BOUNDARY)
    def boom(*a, **k):
        raise MexcApiError("down")
    client.get_top_contracts = boom
    res, _, _ = cycle(tmp_path, client)
    assert res.exit_code == 1 and res.summaries == []


# ---- workflow file -------------------------------------------------------------------------------
WF = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows" / "scanner.yml"


def test_workflow_yaml_schedule_concurrency_and_dispatch():
    wf = yaml.safe_load(WF.read_text())
    on = wf.get("on") or wf.get(True)
    assert on["schedule"] == [{"cron": "5 * * * *"}]  # hourly; scanner decides what is due
    assert "workflow_dispatch" in on and "timeframes" in on["workflow_dispatch"]["inputs"]
    assert wf["concurrency"]["group"] and wf["concurrency"]["cancel-in-progress"] is False
    assert wf["jobs"]["scan"]["timeout-minutes"] >= 45  # room for two 10-minute gaps
    text = WF.read_text()
    assert "STATE_SYNC_COMMAND" in text and "TELEGRAM_BOT_TOKEN" in text and "secrets." in text
