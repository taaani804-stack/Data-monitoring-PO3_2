"""MEXC USDT-M perpetual Futures public market data (read-only; no orders)."""
from __future__ import annotations

import logging
import math
import re
import time
from typing import Any, Callable, Dict, Iterable, List, Optional

import requests

from models import Candle, ContractInfo, SelectionStats

log = logging.getLogger("mexc")

# MEXC announced that Futures REST traffic moved to api.mexc.com in Jan 2026.
BASE_URL = "https://api.mexc.com"

# 15m is kept only for development / --inspect use; the production scanner runs 1h, 4h and 1d.
TIMEFRAME_MS: Dict[str, int] = {
    "15m": 15 * 60_000,
    "1h": 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "1d": 24 * 60 * 60_000,
}
# MEXC Futures kline interval names (each timeframe requests its OWN interval - never resampled).
INTERVALS: Dict[str, str] = {"15m": "Min15", "1h": "Min60", "4h": "Hour4", "1d": "Day1"}

STABLE_OR_PEGGED_BASES = {
    "USDT", "USDC", "FDUSD", "TUSD", "DAI", "BUSD", "USDD", "USDE", "USDP",
    "PYUSD", "USD1", "USDJ", "GUSD", "LUSD", "RLUSD", "XUSD", "EURC", "EURT",
    "AEUR", "EUR", "USDS",
}
_LEVERAGED_RE = re.compile(r"^[A-Z0-9]{2,}[2-5][LS]$")


class MexcApiError(Exception):
    """Raised for non-retryable API problems or exhausted retries."""


def timeframe_ms(timeframe: str) -> int:
    try:
        return TIMEFRAME_MS[timeframe]
    except KeyError:
        raise ValueError(
            f"Unsupported timeframe {timeframe!r}; supported: {sorted(TIMEFRAME_MS)}"
        )


def drop_unclosed_candles(
    candles: List[Candle], tf_ms: int, now_ms: int, buffer_ms: int = 0
) -> List[Candle]:
    """Keep only candles whose period has ended, including the safety buffer."""
    return [
        c for c in candles
        if c.open_time_ms + tf_ms + buffer_ms <= now_ms
    ]


def _to_float(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def parse_klines(rows: Any) -> List[Candle]:
    """Parse row-oriented candles; retained for compatibility with existing tests/tools."""
    if not isinstance(rows, list):
        raise MexcApiError("Unexpected klines payload (not a list)")
    by_time: Dict[int, Candle] = {}
    bad = 0
    for row in rows:
        try:
            t = int(row[0])
            o, h, l, c = (_to_float(row[i]) for i in (1, 2, 3, 4))
            v = _to_float(row[5]) if len(row) > 5 else 0.0
        except (TypeError, ValueError, IndexError):
            bad += 1
            continue
        if None in (o, h, l, c) or h < l or min(o, h, l, c) <= 0:
            bad += 1
            continue
        by_time[t] = Candle(t, o, h, l, c, v or 0.0)
    if bad:
        log.warning("Skipped %d malformed kline row(s)", bad)
    return [by_time[t] for t in sorted(by_time)]


def parse_futures_klines(data: Any) -> List[Candle]:
    """Parse MEXC Futures' column-oriented data arrays."""
    if not isinstance(data, dict):
        raise MexcApiError("Unexpected Futures kline payload (data is not an object)")
    keys = ("time", "open", "high", "low", "close")
    if any(not isinstance(data.get(k), list) for k in keys):
        raise MexcApiError("Unexpected Futures kline payload (missing candle arrays)")
    times = data["time"]
    columns = [data["open"], data["high"], data["low"], data["close"]]
    volumes = data.get("vol", [])
    by_time: Dict[int, Candle] = {}
    bad = 0
    for i, raw_t in enumerate(times):
        try:
            t = int(raw_t)
            o, h, l, c = (_to_float(col[i]) for col in columns)
            v = _to_float(volumes[i]) if isinstance(volumes, list) and i < len(volumes) else 0.0
        except (TypeError, ValueError, IndexError):
            bad += 1
            continue
        if t < 100_000_000_000:
            t *= 1000
        if None in (o, h, l, c) or h < l or min(o, h, l, c) <= 0:
            bad += 1
            continue
        by_time[t] = Candle(t, o, h, l, c, v or 0.0)
    if bad:
        log.warning("Skipped %d malformed Futures kline row(s)", bad)
    return [by_time[t] for t in sorted(by_time)]


def is_unsuitable_base(base: str) -> bool:
    base = base.upper()
    return base in STABLE_OR_PEGGED_BASES or bool(_LEVERAGED_RE.match(base))


def _unwrap_data(payload: Any, endpoint: str) -> Any:
    if not isinstance(payload, dict):
        raise MexcApiError(f"{endpoint}: unexpected response payload")
    if payload.get("success") is False:
        raise MexcApiError(
            f"{endpoint}: MEXC error {payload.get('code', '')} "
            f"{payload.get('message') or payload.get('msg') or ''}".strip()
        )
    if "data" not in payload:
        raise MexcApiError(f"{endpoint}: response has no data field")
    return payload["data"]


# --- Non-crypto instruments -------------------------------------------------------------------
# MEXC lists US-stock, commodity and index perpetuals next to crypto ones, also settled in USDT.
# The public contract/detail payload has no documented positive "asset class = crypto" field, so MEXC
# metadata can only EXCLUDE (defence in depth). The positive proof of "genuine cryptocurrency" comes from the
# global provider's asset identity (see selection.py). MEXC-side exclusions, from the documented fields:
#   1. ``type`` != 1 (MEXC docs: 2 = suspended), ``typeLabel`` 1 (TradFi) / 2 (stock), ``isHidden``
#   2. concept-plate / display-name keywords (stock, equity, forex, commodity, index ...)
#   3. a denylist of well-known equity / commodity / index tickers (extend with EXTRA_EXCLUDED_BASES)
# Every exclusion is logged with its reason so the list can be audited after the first dry run.
NON_CRYPTO_KEYWORDS = ("STOCK", "EQUITY", "EQUITIES", "FOREX", "COMMODIT", "ETF", "INDEX-FUT", "TRADFI")
KNOWN_NON_CRYPTO_BASES = {
    # US equities / ETFs
    "TSLA", "NVDA", "AAPL", "AMZN", "GOOGL", "GOOG", "META", "MSFT", "MCD", "COIN", "HOOD", "MSTR",
    "CRCL", "NFLX", "AMD", "INTC", "PLTR", "SPY", "QQQ", "BABA", "ORCL", "AVGO", "TSM", "UBER",
    "DIS", "NKE", "JPM", "V", "MA", "BAC", "WMT", "KO", "PEP", "PYPL", "SHOP", "SQ", "SNOW", "ARM",
    # commodities / indices / FX (tokenised gold such as XAUT or PAXG is a crypto token and stays eligible)
    "GOLD", "SILVER", "XAU", "XAG", "XPT", "XPD", "COPPER", "OIL", "USOIL", "UKOIL", "WTI", "BRENT",
    "NATGAS", "NAS100", "US100", "SPX", "SPX500", "SP500", "US500", "US30", "DJI", "DXY", "VIX",
    "EURUSD", "GBPUSD", "USDJPY",
}


def non_crypto_reason(item: Dict[str, Any], base: str, extra_excluded: Iterable[str] = ()) -> Optional[str]:
    """Return a reason string if the contract looks like a non-crypto instrument, else None."""
    try:
        label = int(item.get("typeLabel", 0) or 0)
        if label in (1, 2):
            return "MEXC typeLabel marks a TradFi / stock instrument"
        if int(item.get("type", 1) or 1) != 1:
            return "suspended contract (MEXC type != 1)"
    except (TypeError, ValueError):
        return "unparseable contract type/typeLabel"
    if item.get("isHidden") is True:
        return "hidden contract"
    if base in KNOWN_NON_CRYPTO_BASES or base in {b.upper() for b in extra_excluded}:
        return "known non-crypto ticker (stock/commodity/index/FX)"
    text_fields = [item.get("displayNameEn"), item.get("baseCoinName")]
    plates = item.get("conceptPlate")
    if isinstance(plates, list):
        text_fields.extend(plates)
    blob = " ".join(str(t).upper() for t in text_fields if t)
    for kw in NON_CRYPTO_KEYWORDS:
        if kw in blob:
            return f"non-crypto keyword {kw!r} in contract metadata"
    return None


def _contract_list(contract_info: Any) -> List[Any]:
    if isinstance(contract_info, dict):
        data = contract_info.get("data", contract_info.get("contracts", []))
        return data if isinstance(data, list) else []
    return contract_info if isinstance(contract_info, list) else []


def _spread_pct(ticker: Dict[str, Any]) -> Optional[float]:
    """(ask1 - bid1) / mid * 100 from the MEXC ticker; None if bid/ask are missing or crossed."""
    bid, ask = _to_float(ticker.get("bid1")), _to_float(ticker.get("ask1"))
    if not bid or not ask or bid <= 0 or ask <= 0 or ask < bid:
        return None
    return (ask - bid) / ((ask + bid) / 2) * 100


def filter_mexc_contracts(
    contract_info: Any,
    tickers: Iterable[Dict[str, Any]],
    extra_excluded: Iterable[str] = (),
) -> "tuple[List[ContractInfo], SelectionStats]":
    """All enabled USDT-margined PERPETUAL contracts passing the MEXC-side checks, ranked by MEXC turnover.

    Only MEXC Futures data is used (``contract/detail`` + ``contract/ticker``); spot is never consulted.
    The ``selected`` field of the returned stats is left at 0 (callers decide how many to keep).
    """
    contracts = _contract_list(contract_info)
    stats = SelectionStats(discovered=len(contracts))

    def exclude(reason: str, sym: str) -> None:
        stats.excluded.setdefault(reason, []).append(sym)

    eligible: Dict[str, Dict[str, Any]] = {}
    for item in contracts:
        if not isinstance(item, dict):
            exclude("malformed entry", "?")
            continue
        sym = str(item.get("symbol", "")).upper()
        base = str(item.get("baseCoin", "")).upper()
        quote = str(item.get("quoteCoin", "")).upper()
        settle = str(item.get("settleCoin", "")).upper()
        if not sym or not base:
            exclude("missing symbol/base", sym or "?")
            continue
        if quote != "USDT" or settle != "USDT":
            exclude("not USDT-margined (quote/settle != USDT: USDC, USD, coin-margined ...)", sym)
            continue
        if sym != f"{base}_USDT":
            exclude("unexpected symbol format", sym)
            continue
        try:
            if int(item.get("futureType", 0)) != 1:  # MUST be perpetual; missing/unknown = rejected
                exclude("not a perpetual contract (delivery or unknown futureType)", sym)
                continue
            if int(item.get("state", -1)) != 0:  # 0 = enabled; 1-4 = delivery/delivered/offline/paused
                exclude("not in normal trading state (delisted/paused/offline)", sym)
                continue
        except (TypeError, ValueError):
            exclude("unparseable futureType/state", sym)
            continue
        if is_unsuitable_base(base):
            exclude("stablecoin / leveraged-token base", sym)
            continue
        reason = non_crypto_reason(item, base, extra_excluded)
        if reason:
            exclude(reason, sym)
            continue
        eligible[sym] = item

    ranked = []
    seen = set()
    for ticker in tickers:
        if not isinstance(ticker, dict):
            continue
        sym = str(ticker.get("symbol", "")).upper()
        if sym not in eligible or sym in seen:
            continue
        seen.add(sym)
        last = _to_float(ticker.get("lastPrice"))
        turnover = _to_float(ticker.get("amount24"))
        if not last or last <= 0 or not turnover or turnover <= 0:
            exclude("no usable price / 24h turnover", sym)
            continue
        item = eligible[sym]
        unit = _to_float(item.get("priceUnit")) or 0.0
        scale = item.get("priceScale")
        try:
            scale = int(scale) if scale is not None else None
        except (TypeError, ValueError):
            scale = None
        ranked.append(ContractInfo(
            sym, str(item.get("baseCoin", "")).upper(), turnover, unit if unit > 0 else 0.0, scale,
            spread_pct=_spread_pct(ticker), name=str(item.get("baseCoinName") or ""),
        ))
    for sym in eligible:
        if sym not in seen:
            exclude("no ticker data", sym)
    ranked.sort(key=lambda ci: (-ci.turnover_24h, ci.symbol))
    stats.eligible = len(ranked)
    return ranked, stats


def select_top_contracts(
    contract_info: Any,
    tickers: Iterable[Dict[str, Any]],
    top_n: int = 100,
    extra_excluded: Iterable[str] = (),
) -> "tuple[List[ContractInfo], SelectionStats]":
    """MEXC-only ranking: the ``top_n`` eligible contracts by MEXC 24h turnover (legacy / fallback path)."""
    ranked, stats = filter_mexc_contracts(contract_info, tickers, extra_excluded)
    selected = ranked[: max(0, top_n)]
    stats.selected = len(selected)
    return selected, stats


def select_top_symbols(
    contract_info: Any, tickers: Iterable[Dict[str, Any]], top_n: int = 100
) -> List[str]:
    """Symbol-only convenience wrapper around :func:`select_top_contracts`."""
    selected, _ = select_top_contracts(contract_info, tickers, top_n)
    return [c.symbol for c in selected]


class MexcClient:
    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout: float = 15.0,
        session: Optional[requests.Session] = None,
        max_retries: int = 4,
        backoff_seconds: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = (base_url or BASE_URL).rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": "po3-signal-scanner/1.0",
            "Accept": "application/json",
        })
        self.max_retries = max_retries
        self.backoff = backoff_seconds
        self._sleep = sleep

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = self.base_url + path
        last_err = "unknown error"
        for attempt in range(self.max_retries + 1):
            delay = self.backoff * (2 ** attempt)
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                last_err = type(exc).__name__
                if attempt < self.max_retries:
                    self._sleep(delay)
                continue
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError:
                    raise MexcApiError(f"{path}: response was not valid JSON")
            if resp.status_code in (418, 429) or resp.status_code >= 500:
                last_err = f"HTTP {resp.status_code}"
                retry_after = resp.headers.get("Retry-After")
                try:
                    delay = max(delay, float(retry_after)) if retry_after else delay
                except ValueError:
                    pass
                if attempt < self.max_retries:
                    self._sleep(min(delay, 30.0))
                continue
            raise MexcApiError(f"{path}: HTTP {resp.status_code} {resp.text[:200]}")
        raise MexcApiError(
            f"{path}: gave up after {self.max_retries + 1} attempts ({last_err})"
        )

    def get_server_time_ms(self) -> int:
        payload = self._get("/api/v1/contract/ping")
        return int(_unwrap_data(payload, "/api/v1/contract/ping"))

    def get_exchange_info(self) -> List[Dict[str, Any]]:
        """Contract specifications. MEXC's current docs show ``/contract/detail/country`` (same fields plus a
        country leverage cap); the long-standing ``/contract/detail`` is tried first, the other is the fallback."""
        last: Optional[Exception] = None
        for path in ("/api/v1/contract/detail", "/api/v1/contract/detail/country"):
            try:
                data = _unwrap_data(self._get(path), path)
                if isinstance(data, list):
                    return data
                last = MexcApiError(f"{path}: data is not a list")
            except MexcApiError as exc:
                last = exc
        raise MexcApiError(f"contract specification unavailable: {last}")

    def get_tickers_24h(self) -> List[Dict[str, Any]]:
        payload = self._get("/api/v1/contract/ticker")
        data = _unwrap_data(payload, "/api/v1/contract/ticker")
        if isinstance(data, dict):
            return [data]
        if not isinstance(data, list):
            raise MexcApiError("/api/v1/contract/ticker: data is not a list/object")
        return data

    def get_top_contracts(
        self, top_n: int = 100, extra_excluded: Iterable[str] = ()
    ) -> "tuple[List[ContractInfo], SelectionStats]":
        """Fresh ranking (2 API calls). Called at the start of every scan so volume changes are picked up."""
        selected, stats = select_top_contracts(
            self.get_exchange_info(), self.get_tickers_24h(), top_n, extra_excluded
        )
        if not selected:
            raise MexcApiError("No eligible USDT-M perpetual Futures symbols found")
        return selected, stats

    def get_top_symbols(self, top_n: int = 100) -> List[str]:
        return [c.symbol for c in self.get_top_contracts(top_n)[0]]

    def get_klines(
        self, symbol: str, timeframe: str, limit: int = 500
    ) -> List[Candle]:
        """Fetch the most recent Futures candles; caller must drop the forming candle."""
        interval = INTERVALS.get(timeframe)
        if not interval:
            raise ValueError(f"Unsupported timeframe {timeframe!r}")
        tf_seconds = TIMEFRAME_MS[timeframe] // 1000
        end = int(time.time())
        start = end - max(1, min(int(limit), 2000)) * tf_seconds
        path = f"/api/v1/contract/kline/{symbol}"
        payload = self._get(path, {"interval": interval, "start": start, "end": end})
        data = _unwrap_data(payload, path)
        return parse_futures_klines(data)
