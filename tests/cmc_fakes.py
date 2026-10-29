"""Deterministic fakes of the CoinMarketCap derivatives endpoints (shapes copied from the official docs)."""
import json

from global_volume import EP_EXCHANGES, EP_EXCHANGE_PAIRS


def mk(market_id, sym, cid, vol, quote="USDT", category="perpetual", ctype="cryptocurrency",
       outlier=False, exclusions=None, oi=5000, reported=None, exch_sym=None):
    """One CMC derivative market pair (`quotes[].volume_24h` = USD-normalised volume)."""
    return {
        "market_id": market_id, "market_pair_symbol": f"{sym}/{quote}", "category": category,
        "outlier_detected": outlier, "exclusions": exclusions,
        "market_pair_base": {"exchange_symbol": exch_sym or sym, "symbol": sym, "crypto_id": cid, "currency_type": ctype},
        "market_pair_quote": {"exchange_symbol": quote, "symbol": quote, "crypto_id": 825, "currency_type": "cryptocurrency"},
        "exchange_reported_quotes": [{"convert_id": 2781, "convert_symbol": "USD", "price": 1,
                                      "volume_24h_base": 1, "volume_24h_quote": reported if reported is not None else vol * 100,
                                      "open_interest": oi}],
        "quotes": [{"convert_id": 2781, "convert_symbol": "USD", "price": 1, "volume_24h": vol, "open_interest": oi}],
    }


def exch(eid, slug, vol):
    return {"exchange_id": eid, "exchange_name": slug.title(), "exchange_slug": slug,
            "quotes": [{"convert_symbol": "USD", "derivative_volume_usd": vol, "derivative_volume": vol, "open_interest_usd": 1}]}


class Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}
        self.text = json.dumps(body) if body is not None else ""

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def ok(data, credits=1):
    return {"data": data, "status": {"error_code": 0, "error_message": "", "credit_count": credits}}


class FakeCmcSession:
    """requests.Session stand-in. `markets` maps exchange slug/id -> list of market pairs."""

    def __init__(self, exchanges, markets_by_id, failing_ids=(), script=None):
        self.exchanges, self.markets_by_id, self.failing_ids = exchanges, markets_by_id, set(failing_ids)
        self.script = list(script or [])  # optional queue of Resp to return first (rate-limit tests)
        self.calls, self.headers = [], {}

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        if self.script:
            return self.script.pop(0)
        if url.endswith(EP_EXCHANGES):
            return Resp(200, ok({"exchanges": self.exchanges}))
        if url.endswith(EP_EXCHANGE_PAIRS):
            p = params or {}
            eid = p.get("exchange_id")
            if eid is None:
                slug = p.get("exchange_slug")
                eid = next((e["exchange_id"] for e in self.exchanges if e["exchange_slug"] == slug), None)
                if eid is None:
                    eid = {"mexc": 9999}.get(slug)
            if eid in self.failing_ids:
                return Resp(500, {"status": {"error_code": 500}})
            return Resp(200, ok({"market_pairs": self.markets_by_id.get(eid, [])}, credits=2))
        return Resp(404, {})
