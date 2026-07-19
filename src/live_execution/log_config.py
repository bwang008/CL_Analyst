"""
Logging configuration for the CL Analyst live trading engine.

Extracted from live_trader.py (Phase 1 modularization).
Contains log handlers, filters, and file-logging setup.
"""

from __future__ import annotations

import collections
import logging
import logging.handlers
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from src.live_execution.ascii_safe import AsciiFormatter

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_LOG_DIR = _PROJECT_ROOT / "reports"
_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
_LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

log = logging.getLogger("LiveTrader")


class _TelegramLogCapture(logging.Handler):
    """Thread-safe ring buffer that retains the last N WARNING/ERROR log records."""

    def __init__(self, maxlen: int = 8) -> None:
        super().__init__(level=logging.WARNING)
        self._records: collections.deque = collections.deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            ts_utc = datetime.fromtimestamp(record.created, timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S UTC"
            )
            with self._lock:
                self._records.append((record.levelname, msg, ts_utc))
        except Exception:
            pass

    def drain(self) -> list:
        """Return and clear all buffered records."""
        with self._lock:
            items = list(self._records)
            self._records.clear()
        return items


class CLOnlyLogFilter(logging.Filter):
    """Suppress ib_insync log messages about non-CL positions/trades."""

    _NON_CL_RE = re.compile(
        r"(?:Stock\(|symbol='(?!CL\b)\w+)",
    )
    _VERBOSE_IBKR_RE = re.compile(
        r"^(?:placeOrder:|orderStatus:|execDetails[ :]"
        r"|commissionReport:|updatePortfolio:|position:)",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if self._NON_CL_RE.search(msg):
            return False
        if self._VERBOSE_IBKR_RE.match(msg):
            return False
        return True


def _setup_file_logging(client_id: int) -> None:
    """Add a file handler so logs are persisted to disk."""
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = _LOG_DIR / f"livetrader_{client_id}.log"
    file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(
        AsciiFormatter(_LOG_FORMAT, datefmt=_LOG_DATE_FORMAT)
    )
    logging.getLogger().addHandler(file_handler)
    log.info("File logging enabled: %s", log_file)
