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
import time
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


# ---------------------------------------------------------------------------
# Expected-cancel-bounce downgrade (log-cosmetics-cancel-bounce_07222026_2330)
# ---------------------------------------------------------------------------
# Under broker-side OCA (ocaType=2) every protective-leg fill triggers the
# child's belt-and-suspenders sibling cancel, which races the broker's own
# server-side cancel and bounces with Error 10148 ("cannot be cancelled,
# state: Cancelled") — logged at ERROR by ib_insync on EVERY SL/TP fill.
# The cancel itself must stay (order routing untouched); only the KNOWN
# bounce is reclassified: ids we deliberately cancelled are registered at
# the two ib.cancelOrder chokepoints, and a 10147/10148 for a registered,
# fresh id is downgraded to INFO with an annotation. A bounce for an id we
# NEVER cancelled stays ERROR — that asymmetry is the alarm worth keeping.

_EXPECTED_CANCEL_BOUNCE_TTL_SECONDS = 300.0
_expected_cancel_bounces: dict[str, float] = {}


def register_expected_cancel_bounce(order_id) -> None:
    """Record that a deliberate cancel was just issued for ``order_id`` —
    an IBKR 10147/10148 bounce for it within the TTL is expected noise."""
    now = time.time()
    # Prune stale entries so the registry stays bounded.
    for oid, ts in list(_expected_cancel_bounces.items()):
        if now - ts > _EXPECTED_CANCEL_BOUNCE_TTL_SECONDS:
            del _expected_cancel_bounces[oid]
    _expected_cancel_bounces[str(order_id)] = now


class ExpectedCancelBounceFilter(logging.Filter):
    """Downgrade EXPECTED cancel bounces on the ib_insync.wrapper logger.

    Matches ``Error 10147/10148, reqId <id>`` records; when <id> was
    registered via register_expected_cancel_bounce within the TTL, the
    record is downgraded to INFO and annotated. Everything else passes
    through untouched (returns True always — never suppresses).
    """

    _BOUNCE_RE = re.compile(r"Error 1014[78], reqId (-?\d+)")

    def filter(self, record: logging.LogRecord) -> bool:
        m = self._BOUNCE_RE.search(record.getMessage())
        if m is None:
            return True
        ts = _expected_cancel_bounces.get(m.group(1))
        if ts is None or time.time() - ts > _EXPECTED_CANCEL_BOUNCE_TTL_SECONDS:
            return True
        record.msg = (
            record.getMessage()
            + " (expected: deliberate cancel raced the broker's own"
            " server-side OCA cancel — order already dead)"
        )
        record.args = None
        record.levelno = logging.INFO
        record.levelname = "INFO"
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
    # Sanitize the child's OWN console/stderr stream too — it feeds the
    # runner's raw crash-capture sinks (reports/fleet_stderr/*.stderr.log),
    # which was the one path where non-ASCII escaped as literal \uXXXX
    # (log-cosmetics-cancel-bounce_07222026_2330). Tracebacks bypass
    # logging formatters, so crash fidelity is untouched.
    for handler in logging.getLogger().handlers:
        if type(handler) is logging.StreamHandler and not isinstance(
            handler.formatter, AsciiFormatter
        ):
            fmt = handler.formatter
            handler.setFormatter(AsciiFormatter(
                getattr(fmt, "_fmt", _LOG_FORMAT) if fmt else _LOG_FORMAT,
                datefmt=getattr(fmt, "datefmt", None) if fmt else None,
            ))
    # Downgrade expected cancel bounces at their origin logger (idempotent).
    wrapper_logger = logging.getLogger("ib_insync.wrapper")
    if not any(isinstance(f, ExpectedCancelBounceFilter)
               for f in wrapper_logger.filters):
        wrapper_logger.addFilter(ExpectedCancelBounceFilter())
    log.info("File logging enabled: %s", log_file)
