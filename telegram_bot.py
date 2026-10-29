"""Telegram delivery. Credentials come ONLY from environment variables and are never logged."""
from __future__ import annotations

import html
import logging
import math
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence, Callable, Mapping, Optional

import requests

from models import ConfigError, ContractInfo, Signal
from po3_strategy import STRATEGY_NAME
from short_signals import decimals_for, extra_take_profits, risk_reward, setup_lines

log = logging.getLogger("telegram")

API_URL = "https://api.telegram.org"
ENV_TOKEN = "TELEGRAM_BOT_TOKEN"
ENV_CHAT = "TELEGRAM_CHAT_ID"

__all__ = ["ConfigError", "TelegramCredentials", "load_credentials", "TelegramSender", "format_signal_message", "format_price", "display_pair"]


@dataclass(frozen=True)
class TelegramCredentials:
    token: str
    chat_id: str

    def __repr__(self) -> str:  # never expose secrets through repr / tracebacks
        return "TelegramCredentials(token=***, chat_id=***)"

    __str__ = __repr__


def load_credentials(env: Mapping[str, str] = os.environ) -> TelegramCredentials:
    """Read credentials from the environment; raise a SAFE error if any is missing."""
    token = (env.get(ENV_TOKEN) or "").strip()
    chat_id = (env.get(ENV_CHAT) or "").strip()
    missing = [name for name, val in ((ENV_TOKEN, token), (ENV_CHAT, chat_id)) if not val]
    if missing:
        raise ConfigError(
            "Missing required environment variable(s): " + ", ".join(missing) + ". "
            "Set them (GitHub: Settings > Secrets and variables > Actions) or run with --dry-run."
        )
    return TelegramCredentials(token=token, chat_id=chat_id)


def format_price(value: float) -> str:
    """Readable price without scientific notation, precision scaled to magnitude."""
    if value is None or not math.isfinite(value):
        return "n/a"
    a = abs(value)
    if a >= 1000:
        dec = 2
    elif a >= 1:
        dec = 4
    elif a >= 0.01:
        dec = 6
    elif a >= 0.0001:
        dec = 8
    else:
        dec = 10
    text = f"{value:.{dec}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _fmt_utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def display_pair(symbol: str) -> str:
    """BTC_USDT -> BTCUSDT (display only; the exchange symbol stays BTC_USDT internally)."""
    return symbol.replace("_", "")


def price_formatter(contract: Optional[ContractInfo]):
    """Fixed decimals from the contract's tick size when known, else the magnitude heuristic."""
    dec = decimals_for(contract)
    if dec is None:
        return format_price
    return lambda v: f"{v:.{dec}f}" if v is not None and math.isfinite(v) else "n/a"


MAX_MESSAGE_CHARS = 4000  # Telegram hard limit is 4096


def _usd_short(v: Optional[float]) -> str:
    if v is None or not math.isfinite(v) or v <= 0:
        return "unavailable"
    for div, unit in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if v >= div:
            return f"${v / div:.2f}{unit}"
    return f"${v:,.0f}"


def format_signal_message(
    signal: Signal,
    symbol: str,
    timeframe: str,
    tf_ms: int,
    signal_time_ms: Optional[int] = None,
    contract: Optional[ContractInfo] = None,
    extra_tp_rr: Sequence[float] = (),
) -> str:
    """HTML SHORT alert. Raises ValueError for anything that is not a SELL (SHORT-only scanner).

    Global rank / volume / source come from the CoinMarketCap ranking and are shown as "unavailable" when the
    contract carries no verified ranking data - never invented.
    """
    if signal.direction != "SELL":
        raise ValueError("Only SELL/SHORT signals may be formatted for Telegram")
    fmt = price_formatter(contract)
    esc = html.escape
    now_ms = signal_time_ms if signal_time_ms is not None else int(time.time() * 1000)
    entry, sl, tp = signal.entry_price, signal.stop_loss, signal.take_profit
    rr = risk_reward(signal)
    tf = timeframe.upper()
    lines_ = setup_lines(signal, fmt)
    base = contract.base if contract else symbol.split("_")[0]
    name = f"{contract.name} ({base})" if contract and contract.name and contract.name != base else base
    rank = f"#{contract.global_rank}" if contract and contract.global_rank else "unavailable"
    gvol = _usd_short(contract.global_volume_usd) if contract else "unavailable"
    source = (contract.global_source if contract and contract.global_source else "unavailable")
    rts = (contract.ranking_ts[:16].replace("T", " ") + " UTC") if contract and contract.ranking_ts else "unavailable"
    if contract and contract.ranking_note:
        rts += f" ({contract.ranking_note})"
    quality = ["closed candles only"]
    if contract:
        quality.append(f"MEXC 24h turnover {_usd_short(contract.turnover_24h)}")
        quality.append(f"spread {contract.spread_pct:.3f}%" if contract.spread_pct is not None else "spread n/a")
    bar = "━━━━━━━━━━━━━━━━━━━━"
    extra = extra_take_profits(signal, extra_tp_rr, contract)
    tp_lines = [f"<b>Take Profit 1:</b> <code>{fmt(tp)}</code> ({(tp - entry) / entry * 100:+.2f}%)"]
    for n in (2, 3):
        if n - 2 < len(extra):
            m, price = extra[n - 2]
            tp_lines.append(f"<b>Take Profit {n}:</b> <code>{fmt(price)}</code> ({m:g}R, user-configured extension)")
        else:
            tp_lines.append(f"<b>Take Profit {n}:</b> n/a (original PO3 defines one TP)")
    lines = [
        bar,
        f"🔴 <b>PO3 SHORT SIGNAL</b> · <b>{esc(tf)}</b>",
        bar,
        "",
        f"<b>Cryptocurrency:</b> {esc(name)}",
        f"<b>MEXC futures contract:</b> {esc(symbol)} (USDT-margined perpetual)",
        f"<b>Global futures-volume rank:</b> {esc(rank)}",
        f"<b>Global 24h futures volume:</b> {esc(gvol)}",
        f"<b>Global volume source:</b> {esc(source)}",
        f"<b>Ranking timestamp:</b> {esc(rts)}",
        "",
        "<b>Direction:</b> 🔻 <b>SELL / SHORT</b>",
        f"<b>Timeframe:</b> ⏱ <b>{esc(tf)}</b>",
        f"<b>Signal generated (UTC):</b> {_fmt_utc(now_ms).replace(' UTC', '')}",
        f"<b>Latest closed candle (UTC):</b> {_fmt_utc(signal.candle_open_ms).replace(' UTC', '')} → {_fmt_utc(signal.candle_open_ms + tf_ms).replace(' UTC', '')}",
        "",
        f"<b>Entry:</b> <code>{fmt(entry)}</code>",
        f"<b>Stop Loss:</b> <code>{fmt(sl)}</code> ({(sl - entry) / entry * 100:+.2f}%)",
        *tp_lines,
        f"<b>Risk/Reward:</b> 1:{rr:.2f}",
        "",
        "<b>✅ CONFIRMED ENTRY</b> (closed candle)",
        f"<b>Bearish market structure:</b> {esc(lines_['structure'])}",
        f"<b>Liquidity sweep / manipulation:</b> {esc(lines_['sweep'])}",
        f"<b>Entry confirmation:</b> {esc(lines_['confirmation'])}",
        f"<b>Data quality:</b> {esc(' · '.join(quality))}",
        bar,
        "<i>Alert only — no order placed. Not financial advice; no outcome is guaranteed.</i>",
    ]
    text = "\n".join(lines)
    if len(text) > MAX_MESSAGE_CHARS:  # shorten only the free-text sweep line, never the price block
        over = len(text) - MAX_MESSAGE_CHARS
        old = lines_["sweep"]
        new = old[: max(20, len(old) - over - 1)] + "…"
        text = text.replace(esc(old), esc(new))
    return text


class TelegramSender:
    def __init__(
        self,
        credentials: TelegramCredentials,
        session: Optional[requests.Session] = None,
        timeout: float = 15.0,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._creds = credentials
        self._session = session or requests.Session()
        self._timeout = timeout
        self._max_retries = max_retries
        self._sleep = sleep

    def _scrub(self, text: str) -> str:
        """Remove the token (it is part of the request URL) from any error text."""
        return text.replace(self._creds.token, "***")

    def send_text(self, text: str) -> bool:
        """Return True only when Telegram confirmed delivery."""
        url = f"{API_URL}/bot{self._creds.token}/sendMessage"
        payload = {
            "chat_id": self._creds.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        for attempt in range(self._max_retries + 1):
            try:
                resp = self._session.post(url, json=payload, timeout=self._timeout)
            except requests.RequestException as exc:
                log.warning("Telegram request failed: %s", type(exc).__name__)
                self._sleep(2 ** attempt)
                continue
            if resp.status_code == 200:
                return True
            description = ""
            retry_after = None
            try:
                body = resp.json()
                description = str(body.get("description", ""))
                retry_after = (body.get("parameters") or {}).get("retry_after")
            except ValueError:
                pass
            log.warning("Telegram HTTP %s: %s", resp.status_code, self._scrub(description)[:200])
            if resp.status_code == 429 or resp.status_code >= 500:
                self._sleep(min(float(retry_after) if retry_after else 2 ** attempt, 30.0))
                continue
            if resp.status_code == 400 and "parse" in description.lower() and payload.get("parse_mode"):
                # Avoidable formatting error: resend once as plain text (tags stripped).
                payload = {k: v for k, v in payload.items() if k != "parse_mode"}
                payload["text"] = html.unescape(re.sub(r"<[^>]+>", "", payload["text"]))
                continue
            return False  # 4xx (bad token / chat id / message) - retrying will not help
        return False
