"""Telegram send: Markdown 400 -> plain-text retry fallback.

Regression for follow-up #5: A4 escalation alerts embed identifiers like
`trade_27`. Under parse_mode="Markdown" the unbalanced underscore made
Telegram return HTTP 400 ("can't parse entities") and the alert silently
vanished. send() must retry once as plain text so the alert still delivers.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.live_execution.utils import telegram_alert
from src.live_execution.utils.telegram_alert import TelegramAlerter


class _Resp:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


def _alerter() -> TelegramAlerter:
    # Explicit creds so .enabled is True regardless of environment.
    return TelegramAlerter(token="T", chat_id="C")


def test_markdown_400_retries_as_plaintext(monkeypatch):
    """A 400 on the Markdown send triggers exactly one plain-text retry."""
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append(dict(json))
        # First call (Markdown) 400s; second call (no parse_mode) succeeds.
        if "parse_mode" in json:
            return _Resp(400, "Bad Request: can't parse entities")
        return _Resp(200)

    monkeypatch.setattr(telegram_alert, "requests", SimpleNamespace(post=fake_post,
                        exceptions=telegram_alert.requests.exceptions))

    ok = _alerter().send("naked position on trade_27 needs a human")

    assert ok is True
    assert len(calls) == 2, "expected one retry after the 400"
    assert "parse_mode" in calls[0], "first attempt should use Markdown"
    assert "parse_mode" not in calls[1], "retry must drop parse_mode (plain text)"
    # The message body is carried through unchanged on the retry.
    assert "trade_27" in calls[1]["text"]


def test_success_does_not_retry(monkeypatch):
    """A 200 on the first attempt must not trigger a second POST."""
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append(dict(json))
        return _Resp(200)

    monkeypatch.setattr(telegram_alert, "requests", SimpleNamespace(post=fake_post,
                        exceptions=telegram_alert.requests.exceptions))

    ok = _alerter().send("clean message")

    assert ok is True
    assert len(calls) == 1, "a successful send must not retry"


def test_plaintext_400_does_not_loop(monkeypatch):
    """If parse_mode was already absent, a 400 does not retry (returns False)."""
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append(dict(json))
        return _Resp(400, "Bad Request")

    monkeypatch.setattr(telegram_alert, "requests", SimpleNamespace(post=fake_post,
                        exceptions=telegram_alert.requests.exceptions))

    ok = _alerter().send("plain", parse_mode=None)

    assert ok is False
    assert len(calls) == 1, "no parse_mode to strip -> no retry"


def test_retry_also_failing_returns_false(monkeypatch):
    """If the plain-text retry also fails, send() reports False (never raises)."""
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append(dict(json))
        return _Resp(500, "server error")

    monkeypatch.setattr(telegram_alert, "requests", SimpleNamespace(post=fake_post,
                        exceptions=telegram_alert.requests.exceptions))

    ok = _alerter().send("trade_27 escalation")

    assert ok is False
