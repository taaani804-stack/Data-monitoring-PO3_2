"""Global crypto futures/perpetual 24h volume ranking from CoinMarketCap (ranking only; never used for signals).

Provider: CoinMarketCap Pro API, derivatives endpoints (documented as available on the free "Basic" plan):
  GET /v5/exchange/derivatives/list                         -> derivatives exchanges (+ their 24h derivative volume)
  GET /v5/exchange/derivatives/market-pairs/list/latest     -> all derivative markets of ONE exchange (batch)
The per-cryptocurrency endpoint (/v5/cryptocurrency/derivatives/market-pairs/list/latest) is deliberately NOT used:
it needs one call per coin. Instead the top exchanges' market lists are fetched once and aggregated per coin.

Methodology
* Metric = sum, over every tracked derivative market of a coin on the fetched exchanges, of CMC's USD-normalised
  ``quotes[USD].volume_24h`` (one USD currency for every market). Exchange-reported raw quote volumes
  (``exchange_reported_quotes``) are never mixed in. Open interest, spot volume and market cap are never used.
* A market is counted at most once (de-duplicated by CMC ``market_id`` across pages/exchanges).
* Markets flagged ``outlier_detected`` or carrying ``exclusions`` are NOT counted (conservative).
* Only bases with ``currency_type == "cryptocurrency"`` and an integer ``crypto_id`` are ranked; stablecoin bases dropped.
* Coverage is partial by construction (top-N exchanges by derivative volume); the covered share of the total
  derivative volume reported by CMC is stored in the cache and shown in logs. It is NOT the entire global market.
"""
from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import requests

from mexc_data import is_unsuitable_base

log = logging.getLogger("global_volume")

CMC_BASE_URL = "https://pro-api.coinmarketcap.com"
EP_EXCHANGES = "/v5/exchange/derivatives/list"
EP_EXCHANGE_PAIRS = "/v5/exchange/derivatives/market-pairs/list/latest"
PROVIDER = "CoinMarketCap"
MEXC_SLUG = "mexc"
CACHE_VERSION = 1


class GlobalVolumeError(Exception):
    """Global ranking could not be built (network, plan/key problem, malformed data...)."""


def _num(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


# ---------------------------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------------------------
class CmcClient:
    """Minimal CoinMarketCap Pro client: header-key auth, bounded retries, credit accounting. Key never logged."""

    def __init__(self, api_key: str, base_url: Optional[str] = None, timeout: float = 20.0,
                 session: Optional[requests.Session] = None, max_retries: int = 3,
                 backoff_seconds: float = 2.0, min_interval_seconds: float = 1.3,
                 max_credits: int = 400, sleep: Callable[[float], None] = time.sleep) -> None:
        if not api_key or not api_key.strip():
            raise GlobalVolumeError("COINMARKETCAP_API_KEY is not set")
        self._key = api_key.strip()
        self.base_url = (base_url or CMC_BASE_URL).rstrip("/")
        self.timeout, self.max_retries, self.backoff = timeout, max_retries, backoff_seconds
        self.min_interval, self.max_credits, self._sleep = min_interval_seconds, max_credits, sleep
        self.session = session or requests.Session()
        self.session.headers.update({"Accept": "application/json", "User-Agent": "po3-signal-scanner/2.0"})
        self.credits_used = 0
        self.calls = 0
        self._last_call = 0.0

    def _throttle(self) -> None:  # Basic plan: 50 requests/minute -> stay well below
        wait = self.min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            self._sleep(wait)
        self._last_call = time.monotonic()

    def get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        url = self.base_url + path
        last = "unknown error"
        for attempt in range(self.max_retries + 1):
            if self.credits_used >= self.max_credits:
                raise GlobalVolumeError(f"credit guard: {self.credits_used} credits used this refresh (limit {self.max_credits})")
            self._throttle()
            try:
                resp = self.session.get(url, params=params, headers={"X-CMC_PRO_API_KEY": self._key}, timeout=self.timeout)
            except requests.RequestException as exc:
                last = type(exc).__name__
                if attempt < self.max_retries:
                    self._sleep(self.backoff * (2 ** attempt))
                continue
            self.calls += 1
            if resp.status_code == 200:
                try:
                    body = resp.json()
                except ValueError:
                    raise GlobalVolumeError(f"{path}: response was not valid JSON")
                status = body.get("status") if isinstance(body, dict) else None
                credit = _num((status or {}).get("credit_count"))
                self.credits_used += int(credit) if credit else 1
                if status and status.get("error_code") not in (0, "0", None):
                    raise GlobalVolumeError(f"{path}: CMC error {status.get('error_code')}: {str(status.get('error_message'))[:150]}")
                return body
            if resp.status_code == 429 or resp.status_code >= 500:  # rate limit / temporary
                last = f"HTTP {resp.status_code}"
                delay = self.backoff * (2 ** attempt)
                try:
                    delay = max(delay, float(resp.headers.get("Retry-After", 0)))
                except ValueError:
                    pass
                if attempt < self.max_retries:
                    self._sleep(min(delay, 65.0))
                continue
            if resp.status_code in (401, 403):  # bad key / plan does not include the endpoint: do not retry
                raise GlobalVolumeError(f"{path}: HTTP {resp.status_code} - API key invalid or plan does not include this endpoint")
            raise GlobalVolumeError(f"{path}: HTTP {resp.status_code}")
        raise GlobalVolumeError(f"{path}: gave up after {self.max_retries + 1} attempts ({last})")

    def list_exchanges(self, limit: int) -> List[Dict[str, Any]]:
        body = self.get(EP_EXCHANGES, {"limit": limit, "sort": "volume_24h", "sort_dir": "desc", "convert": "USD"})
        exchanges = (body.get("data") or {}).get("exchanges")
        if not isinstance(exchanges, list):
            raise GlobalVolumeError(f"{EP_EXCHANGES}: data.exchanges missing")
        return exchanges

    def exchange_markets(self, exchange_id: Optional[int] = None, slug: Optional[str] = None, limit: int = 1000) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"limit": limit, "sort": "volume_24h_strict", "sort_dir": "desc",
                                  "category": "all", "convert": "USD"}
        if exchange_id is not None:
            params["exchange_id"] = exchange_id
        else:
            params["exchange_slug"] = slug
        body = self.get(EP_EXCHANGE_PAIRS, params)
        pairs = (body.get("data") or {}).get("market_pairs")
        if not isinstance(pairs, list):
            raise GlobalVolumeError(f"{EP_EXCHANGE_PAIRS}: data.market_pairs missing")
        return pairs


# ---------------------------------------------------------------------------------------------
# Pure aggregation
# ---------------------------------------------------------------------------------------------
def _usd_volume(market: Dict[str, Any]) -> Optional[float]:
    """CMC's USD-normalised 24h volume of one market (``quotes`` entry whose convert_symbol is USD)."""
    for q in market.get("quotes") or []:
        if isinstance(q, dict) and str(q.get("convert_symbol", "USD")).upper() == "USD":
            v = _num(q.get("volume_24h"))
            return v if v is not None and v >= 0 else None
    return None


@dataclass
class AggregationStats:
    markets_seen: int = 0
    markets_counted: int = 0
    duplicates: int = 0
    skipped: Dict[str, int] = field(default_factory=dict)

    def skip(self, why: str) -> None:
        self.skipped[why] = self.skipped.get(why, 0) + 1


def aggregate_assets(
    markets: Iterable[Tuple[str, Dict[str, Any]]],
    categories: Iterable[str] = ("perpetual", "futures"),
) -> Tuple[Dict[int, Dict[str, Any]], AggregationStats]:
    """Sum USD 24h volume per cryptocurrency over ``(exchange_slug, market)`` items, without double counting."""
    wanted = {c.lower() for c in categories}
    stats = AggregationStats()
    seen_ids: set = set()
    assets: Dict[int, Dict[str, Any]] = {}
    for slug, m in markets:
        stats.markets_seen += 1
        if not isinstance(m, dict):
            stats.skip("malformed market")
            continue
        mid = m.get("market_id")
        key = (mid if mid is not None else (slug, m.get("market_pair_symbol"), m.get("category")))
        if key in seen_ids:
            stats.duplicates += 1
            continue
        seen_ids.add(key)
        if str(m.get("category", "")).lower() not in wanted:
            stats.skip("category not in " + "/".join(sorted(wanted)))
            continue
        if m.get("outlier_detected") is True:
            stats.skip("outlier_detected")
            continue
        if m.get("exclusions"):
            stats.skip("exclusions present")
            continue
        base = m.get("market_pair_base") or {}
        cid = base.get("crypto_id")
        if base.get("currency_type") != "cryptocurrency" or not isinstance(cid, int) or isinstance(cid, bool):
            stats.skip("base is not a CMC-identified cryptocurrency")
            continue
        sym = str(base.get("symbol") or base.get("exchange_symbol") or "").upper()
        if not sym or is_unsuitable_base(sym):
            stats.skip("stablecoin / leveraged / empty symbol")
            continue
        vol = _usd_volume(m)
        if vol is None:
            stats.skip("no USD volume")
            continue
        a = assets.setdefault(cid, {"crypto_id": cid, "symbol": sym, "volume_usd": 0.0, "markets": 0, "exchanges": set()})
        a["volume_usd"] += vol
        a["markets"] += 1
        a["exchanges"].add(slug)
        stats.markets_counted += 1
    return assets, stats


@dataclass
class GlobalRanking:
    generated_at: str  # ISO-8601 UTC: when the data was fetched
    provider: str
    endpoints: List[str]
    assets: List[Dict[str, Any]]  # ranked, rank starts at 1
    mexc_markets: Dict[str, Dict[str, Any]]  # "BTC_USDT" -> {"crypto_id", "symbol", "category", "quote"} per CMC's MEXC listing
    coverage: Dict[str, Any]
    assets_received: int = 0
    credits_used: int = 0

    def age_hours(self, now: Optional[datetime] = None) -> float:
        now = now or datetime.now(timezone.utc)
        return (now - datetime.fromisoformat(self.generated_at)).total_seconds() / 3600.0

    def to_json(self) -> Dict[str, Any]:
        return {"version": CACHE_VERSION, **asdict(self)}

    @classmethod
    def from_json(cls, d: Dict[str, Any]) -> "GlobalRanking":
        if d.get("version") != CACHE_VERSION:
            raise ValueError("unsupported cache version")
        r = cls(d["generated_at"], d["provider"], list(d["endpoints"]), list(d["assets"]),
                dict(d["mexc_markets"]), dict(d["coverage"]), int(d.get("assets_received", 0)), int(d.get("credits_used", 0)))
        datetime.fromisoformat(r.generated_at)  # validates
        return r


def rank_assets(assets: Dict[int, Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for a in assets.values():
        out.append({"crypto_id": a["crypto_id"], "symbol": a["symbol"], "volume_usd": a["volume_usd"],
                    "markets": a["markets"], "exchanges": len(a["exchanges"])})
    out.sort(key=lambda a: (-a["volume_usd"], a["symbol"], a["crypto_id"]))
    for i, a in enumerate(out, 1):
        a["rank"] = i
    return out


def mexc_listing(markets: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """CMC's own list of MEXC derivative markets, keyed by MEXC contract symbol (``BASE_QUOTE``)."""
    out: Dict[str, Dict[str, Any]] = {}
    for m in markets:
        if not isinstance(m, dict):
            continue
        base, quote = m.get("market_pair_base") or {}, m.get("market_pair_quote") or {}
        b = str(base.get("exchange_symbol") or base.get("symbol") or "").upper()
        q = str(quote.get("exchange_symbol") or quote.get("symbol") or "").upper()
        cid = base.get("crypto_id")
        if not b or not q or not isinstance(cid, int) or base.get("currency_type") != "cryptocurrency":
            continue
        out[f"{b}_{q}"] = {"crypto_id": cid, "symbol": str(base.get("symbol") or b).upper(),
                           "category": str(m.get("category", "")).lower(), "quote": q}
    return out


def build_global_ranking(
    client: CmcClient,
    max_exchanges: int = 40,
    pairs_per_exchange: int = 1000,
    categories: Iterable[str] = ("perpetual", "futures"),
    now: Optional[datetime] = None,
) -> GlobalRanking:
    """Steps A-C: fetch, aggregate without double counting, rank. Raises GlobalVolumeError on failure."""
    now = now or datetime.now(timezone.utc)
    exchanges = client.list_exchanges(max(max_exchanges, 1) + 20)
    usable = [e for e in exchanges if isinstance(e, dict) and isinstance(e.get("exchange_id"), int)]
    if not usable:
        raise GlobalVolumeError("no derivatives exchanges returned")

    def vol(e: Dict[str, Any]) -> float:
        for q in e.get("quotes") or []:
            if isinstance(q, dict) and str(q.get("convert_symbol", "USD")).upper() == "USD":
                return _num(q.get("derivative_volume_usd") or q.get("derivative_volume")) or 0.0
        return 0.0

    usable.sort(key=vol, reverse=True)
    chosen = usable[:max_exchanges]
    total_vol = sum(vol(e) for e in usable)
    chosen_vol = sum(vol(e) for e in chosen)

    all_markets: List[Tuple[str, Dict[str, Any]]] = []
    mexc_pairs: Optional[List[Dict[str, Any]]] = None
    fetched, failed = [], []
    for e in chosen:
        slug = str(e.get("exchange_slug") or e["exchange_id"])
        try:
            pairs = client.exchange_markets(exchange_id=e["exchange_id"], limit=pairs_per_exchange)
        except GlobalVolumeError as exc:
            failed.append(slug)
            log.warning("CMC markets for %s failed: %s", slug, exc)
            if "credit guard" in str(exc):
                break
            continue
        fetched.append(slug)
        all_markets.extend((slug, m) for m in pairs)
        if slug == MEXC_SLUG:
            mexc_pairs = pairs
    if mexc_pairs is None:  # MEXC must be verifiable even if it is outside the top-N exchanges
        try:
            mexc_pairs = client.exchange_markets(slug=MEXC_SLUG, limit=pairs_per_exchange)
            if MEXC_SLUG not in fetched:
                fetched.append(MEXC_SLUG)
        except GlobalVolumeError as exc:
            log.warning("CMC MEXC market list failed: %s", exc)
            mexc_pairs = []
    min_fetched = max(3, max_exchanges // 2)
    if len(fetched) < min_fetched:
        raise GlobalVolumeError(f"only {len(fetched)} of {len(chosen)} exchange market lists could be fetched (need >= {min_fetched})")

    assets, agg = aggregate_assets(all_markets, categories)
    ranked = rank_assets(assets)
    if not ranked:
        raise GlobalVolumeError("aggregation produced no ranked assets")
    log.info("Global ranking: exchanges fetched=%d failed=%d | markets seen=%d counted=%d duplicates=%d skipped=%s",
             len(fetched), len(failed), agg.markets_seen, agg.markets_counted, agg.duplicates, agg.skipped)
    coverage = {
        "exchanges_listed": len(usable), "exchanges_fetched": len(fetched), "exchanges_failed": failed,
        "fetched_volume_share_pct": round(chosen_vol / total_vol * 100, 2) if total_vol > 0 else None,
        "markets_seen": agg.markets_seen, "markets_counted": agg.markets_counted,
        "duplicates_skipped": agg.duplicates, "skipped": agg.skipped,
        "categories": sorted({c.lower() for c in categories}), "pairs_per_exchange_cap": pairs_per_exchange,
    }
    return GlobalRanking(now.isoformat(), PROVIDER, [EP_EXCHANGES, EP_EXCHANGE_PAIRS], ranked[:600],
                         mexc_listing(mexc_pairs), coverage, len(ranked), client.credits_used)


# ---------------------------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------------------------
class RankingCache:
    """JSON file holding the latest SUCCESSFUL ranking (atomic writes). Persisted between runs by state_branch.sh."""

    def __init__(self, path: str) -> None:
        self.path = path

    def load(self) -> Optional[GlobalRanking]:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                return GlobalRanking.from_json(json.load(fh))
        except FileNotFoundError:
            return None
        except (ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
            log.warning("Ranking cache unreadable (%s) - ignoring it", type(exc).__name__)
            return None

    def save(self, ranking: GlobalRanking) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".rank_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(ranking.to_json(), fh, indent=1, sort_keys=True)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
