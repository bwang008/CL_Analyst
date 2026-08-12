"""
Lightweight push-only Telegram notification client.

Sends Markdown-formatted messages to a Telegram bot chat.
Designed for fire-and-forget usage — all network errors are caught
and logged, never bubbled. A dropped Telegram connection must never
crash the live trader.

Keep message text plain ASCII: no emoji, no decorative punctuation, no
unnecessary filler. Non-ASCII in operator output has caused crashes in the
past when a sink could not render/encode it. ``send()`` runs every message
through ``ascii_safe.to_ascii`` as a safety net (emoji dropped, ``—`` -> ``-``),
but author strings in ASCII to begin with — see ``ascii_safe`` for the why.

Setup:
    1. Talk to @BotFather on Telegram → /newbot → copy the token.
    2. Send any message to your new bot.
    3. Visit https://api.telegram.org/bot<TOKEN>/getUpdates
       to find your chat_id.
    4. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in your .env.

Usage:
    from src.live_execution.utils.telegram_alert import TelegramAlerter

    tg = TelegramAlerter()            # reads from env / .env
    tg.send("LiveTrader started")     # fire-and-forget
    tg.send("SL HIT: SELL 1 MCL @ 62.45", parse_mode="Markdown")
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    requests = None  # type: ignore[assignment]

from dotenv import load_dotenv

from src.live_execution.ascii_safe import to_ascii

# Load .env from project root
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

log = logging.getLogger("TelegramAlerter")

# Telegram API constants
_TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
_TIMEOUT = 3  # seconds — fast fail, never block the trader


class TelegramAlerter:
    """Fire-and-forget Telegram message sender.

    Attributes:
        enabled: True only if both token and chat_id are configured.
    """

    def __init__(
        self,
        token: str | None = None,
        chat_id: str | None = None,
        prefix: str = "",
    ) -> None:
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")
        self.prefix = prefix
        self.enabled = bool(self.token and self.chat_id)

        if not self.enabled:
            log.info(
                "Telegram alerts DISABLED - set TELEGRAM_BOT_TOKEN and "
                "TELEGRAM_CHAT_ID in .env to enable."
            )
        else:
            log.info("Telegram alerts ENABLED (chat_id=%s)", self.chat_id)

    def send(
        self,
        message: str,
        *,
        parse_mode: str = "Markdown",
        silent: bool = False,
    ) -> bool:
        """Send a message to the configured Telegram chat.

        Args:
            message: Text to send (supports Markdown if parse_mode="Markdown").
            parse_mode: Telegram parse mode ("Markdown", "HTML", or None).
            silent: If True, send without notification sound.

        Returns:
            True if the message was sent successfully, False otherwise.
            Never raises.
        """
        if not self.enabled:
            return False

        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo("America/Los_Angeles")
        except ImportError:
            try:
                import pytz
                tz = pytz.timezone("America/Los_Angeles")
            except ImportError:
                tz = timezone.utc
                
        now_str = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S %Z")
        prefix_str = f"[{self.prefix}] " if getattr(self, "prefix", "") else ""
        # Strip emoji and transliterate punctuation to plain ASCII — no emoji
        # in operator messages, and no mojibake in any client that mishandles
        # Unicode.
        message = to_ascii(f"_{now_str}_\n\n{prefix_str}{message}")

        if requests is None:
            log.warning("requests library not installed - cannot send Telegram alert")
            return False

        url = _TELEGRAM_API_URL.format(token=self.token)
        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "disable_notification": silent,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode

        try:
            resp = requests.post(url, json=payload, timeout=_TIMEOUT)
            # A Markdown parse failure (400 "can't parse entities") happens
            # whenever the body carries an unbalanced _ or * — snake_case
            # identifiers like trade_27 or module paths. That silently dropped
            # A4 escalation alerts. Retry once as plain text so the alert still
            # delivers; literal _/* in the fallback are acceptable.
            if resp.status_code == 400 and parse_mode:
                log.debug(
                    "Telegram 400 with parse_mode=%s; retrying as plain text",
                    parse_mode,
                )
                payload.pop("parse_mode", None)
                resp = requests.post(url, json=payload, timeout=_TIMEOUT)
            if resp.status_code != 200:
                log.warning(
                    "Telegram API returned %d: %s",
                    resp.status_code,
                    resp.text[:200],
                )
                return False
            return True
        except requests.exceptions.Timeout:
            # R7 (exit-fill-observability_08112026_1749): was log.debug, and
            # setup_fleet_logging attaches the root handler at INFO
            # (fleet_log.py:163) - so every timed-out send was a LOST operator
            # alert that left no trace. The non-200 branch above is already
            # WARNING; the split was inconsistent, not deliberate.
            log.warning(
                "Telegram send timed out (%.1fs) - message NOT delivered",
                _TIMEOUT,
            )
            return False
        except Exception:
            # Catch everything: DNS failure, connection reset, SSL error, etc.
            # A Telegram outage must NEVER crash the trader (the "Never raises"
            # contract above is untouched) - but each occurrence is a lost
            # operator alert and must be visible (R7).
            log.warning("Telegram send failed - message NOT delivered", exc_info=True)
            return False
