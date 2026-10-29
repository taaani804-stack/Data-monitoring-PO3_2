import logging

import pytest
import requests

from models import ConfigError, Signal
from telegram_bot import (TelegramCredentials, TelegramSender, format_price, format_signal_message,
                          load_credentials)
from tests.helpers import T0, TF15

SECRET = "123456:SECRET-TOKEN-VALUE"


def test_missing_credentials_raise_safe_error():
    with pytest.raises(ConfigError) as e:
        load_credentials({})
    msg = str(e.value)
    assert "TELEGRAM_BOT_TOKEN" in msg and "TELEGRAM_CHAT_ID" in msg


def test_only_one_missing_is_named():
    with pytest.raises(ConfigError) as e:
        load_credentials({"TELEGRAM_BOT_TOKEN": SECRET})
    assert "TELEGRAM_CHAT_ID" in str(e.value) and SECRET not in str(e.value)


def test_credentials_repr_never_leaks():
    creds = load_credentials({"TELEGRAM_BOT_TOKEN": SECRET, "TELEGRAM_CHAT_ID": "999"})
    assert SECRET not in repr(creds) and SECRET not in str(creds) and "999" not in repr(creds)


def test_format_price_no_scientific_notation():
    assert format_price(65432.123456) == "65432.12"
    assert format_price(0.00001234) == "0.00001234"
    assert format_price(1.5) == "1.5"
    assert "e" not in format_price(0.000000012345).lower()


def _sig(direction="SELL"):
    return Signal(direction, 60, T0 + 60 * TF15, 100.5, 105.8, 95.942, 1.06)


def test_message_contains_all_required_fields():
    text = format_signal_message(_sig("SELL"), "BTC_USDT", "1h", 3_600_000)
    for needle in ["PO3 SHORT SIGNAL", "SELL / SHORT", "BTC_USDT", "1H", "100.5", "105.8", "95.942", "Risk/Reward", "0.86",
                   "Latest closed candle (UTC)", "Signal generated (UTC)", "CONFIRMED ENTRY"]:
        assert needle in text, needle
    with pytest.raises(ValueError):
        format_signal_message(_sig("BUY"), "ETH_USDT", "1h", 3_600_000)


class Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body = status, body or {}

    def json(self):
        return self._body


class Session:
    def __init__(self, results):
        self.results, self.posts = list(results), []

    def post(self, url, json=None, timeout=None):
        self.posts.append((url, json))
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _sender(results):
    s = Session(results)
    return TelegramSender(TelegramCredentials(SECRET, "42"), session=s, sleep=lambda _x: None), s


def test_send_success_payload():
    snd, s = _sender([Resp(200)])
    assert snd.send_text("hi") is True
    url, payload = s.posts[0]
    assert url.endswith("/sendMessage") and payload["chat_id"] == "42" and payload["parse_mode"] == "HTML"


def test_send_retries_then_succeeds_on_429():
    snd, s = _sender([Resp(429, {"parameters": {"retry_after": 1}}), Resp(200)])
    assert snd.send_text("hi") is True and len(s.posts) == 2


def test_send_returns_false_on_client_error_without_retry():
    snd, s = _sender([Resp(400, {"description": "chat not found"})])
    assert snd.send_text("hi") is False and len(s.posts) == 1


def test_token_never_appears_in_logs(caplog):
    # requests exceptions normally embed the URL (with token) - we must log only the class name.
    snd, _ = _sender([requests.ConnectionError(f"failed for https://api.telegram.org/bot{SECRET}/sendMessage")] * 4)
    with caplog.at_level(logging.DEBUG):
        assert snd.send_text("hi") is False
    assert SECRET not in caplog.text
    snd2, _ = _sender([Resp(401, {"description": f"bad token {SECRET}"})])
    with caplog.at_level(logging.DEBUG):
        snd2.send_text("hi")
    assert SECRET not in caplog.text


def test_telegram_test_rejects_dry_run_combination(capsys):
    # Must not claim success when PrintSender would be selected.
    assert __import__("scanner").main(["--telegram-test", "--dry-run"]) == 2
    assert "cannot be combined" in capsys.readouterr().err


def test_telegram_test_rejects_inspect_combination(capsys):
    assert __import__("scanner").main(["--telegram-test", "--inspect", "AAAUSDT"]) == 2
    assert "cannot be combined" in capsys.readouterr().err
