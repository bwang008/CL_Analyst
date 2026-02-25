"""
Telemetry & Trade Ledger for Live Execution.

Lightweight SQLite backend that records:
- market_bars: every closed 5-minute bar (builds our own historical dataset)
- trade_ledger: every signal generated and every order executed

Author: CL Analyst
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional


_DEFAULT_DB_PATH = "data/live_telemetry.db"

_CREATE_MARKET_BARS = """
CREATE TABLE IF NOT EXISTS market_bars (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT    NOT NULL,
    open        REAL    NOT NULL,
    high        REAL    NOT NULL,
    low         REAL    NOT NULL,
    close       REAL    NOT NULL,
    volume      REAL    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""

_CREATE_TRADE_LEDGER = """
CREATE TABLE IF NOT EXISTS trade_ledger (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT    NOT NULL,
    signal          TEXT    NOT NULL,          -- 'Buy' or 'Hold'
    confidence_pct  REAL,                     -- model probability (0-100)
    action_taken    TEXT    NOT NULL,          -- 'EXECUTE', 'SKIP_POSITION', 'SKIP_THRESHOLD', 'HOLD'
    order_id        INTEGER,                  -- IBKR order ID (NULL if no order)
    fill_price      REAL,                     -- actual fill price (NULL until filled)
    direction       TEXT,                     -- 'BUY' or NULL
    tp_price        REAL,                     -- take-profit target
    sl_price        REAL,                     -- stop-loss target
    atr_value       REAL,                     -- ATR at time of signal
    current_price   REAL,                     -- close price at signal time
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""

_CREATE_RAW_FRONT_MONTH_BARS = """
CREATE TABLE IF NOT EXISTS raw_front_month_bars (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT    NOT NULL,
    open            REAL    NOT NULL,
    high            REAL    NOT NULL,
    low             REAL    NOT NULL,
    close           REAL    NOT NULL,
    volume          REAL    NOT NULL,
    contract_month  TEXT    NOT NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""

_CREATE_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_bars_ts ON market_bars(timestamp);
CREATE INDEX IF NOT EXISTS idx_ledger_ts ON trade_ledger(timestamp);
CREATE INDEX IF NOT EXISTS idx_raw_bars_ts ON raw_front_month_bars(timestamp);
"""


class TelemetryDB:
    """Lightweight SQLite telemetry backend for live execution."""

    def __init__(self, db_path: str = _DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._initialize()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _initialize(self) -> None:
        """Create tables and indexes if they don't exist."""
        conn = self._get_conn()
        conn.executescript(
            _CREATE_MARKET_BARS
            + _CREATE_TRADE_LEDGER
            + _CREATE_RAW_FRONT_MONTH_BARS
            + _CREATE_INDEXES
        )
        conn.commit()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.execute("PRAGMA journal_mode=WAL;")
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Market bars
    # ------------------------------------------------------------------

    def log_bar(
        self,
        timestamp: str | datetime,
        open_: float,
        high: float,
        low: float,
        close: float,
        volume: float,
    ) -> None:
        """Record a closed 5-minute bar."""
        ts = timestamp.isoformat() if isinstance(timestamp, datetime) else str(timestamp)
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO market_bars (timestamp, open, high, low, close, volume) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ts, open_, high, low, close, volume),
        )
        conn.commit()

    def bar_count(self) -> int:
        """Return total number of recorded bars."""
        cur = self._get_conn().execute("SELECT COUNT(*) FROM market_bars")
        return cur.fetchone()[0]

    # ------------------------------------------------------------------
    # Trade ledger — signal logging
    # ------------------------------------------------------------------

    def log_signal(
        self,
        timestamp: str | datetime,
        signal: str,
        confidence_pct: float,
        action_taken: str,
        *,
        current_price: Optional[float] = None,
        atr_value: Optional[float] = None,
        tp_price: Optional[float] = None,
        sl_price: Optional[float] = None,
        order_id: Optional[int] = None,
        fill_price: Optional[float] = None,
        direction: Optional[str] = None,
    ) -> None:
        """Record a generated signal and any resulting action."""
        ts = timestamp.isoformat() if isinstance(timestamp, datetime) else str(timestamp)
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO trade_ledger "
            "(timestamp, signal, confidence_pct, action_taken, order_id, "
            " fill_price, direction, tp_price, sl_price, atr_value, current_price) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ts, signal, confidence_pct, action_taken,
                order_id, fill_price, direction,
                tp_price, sl_price, atr_value, current_price,
            ),
        )
        conn.commit()

    def update_fill(self, order_id: int, fill_price: float) -> None:
        """Update the fill price for an existing order in the ledger."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE trade_ledger SET fill_price = ? WHERE order_id = ?",
            (fill_price, order_id),
        )
        conn.commit()

    def signal_count(self) -> int:
        """Return total number of recorded signals."""
        cur = self._get_conn().execute("SELECT COUNT(*) FROM trade_ledger")
        return cur.fetchone()[0]

    def trade_count(self) -> int:
        """Return number of executed trades (action_taken = 'EXECUTE')."""
        cur = self._get_conn().execute(
            "SELECT COUNT(*) FROM trade_ledger WHERE action_taken = 'EXECUTE'"
        )
        return cur.fetchone()[0]

    # ------------------------------------------------------------------
    # Queries (for dashboarding / analysis)
    # ------------------------------------------------------------------

    def recent_signals(self, n: int = 20) -> list[dict]:
        """Return the N most recent signal entries."""
        conn = self._get_conn()
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM trade_ledger ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
        conn.row_factory = None
        return [dict(r) for r in rows]

    def recent_bars(self, n: int = 20) -> list[dict]:
        """Return the N most recent bar entries."""
        conn = self._get_conn()
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM market_bars ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
        conn.row_factory = None
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Raw front-month bars (training ledger)
    # ------------------------------------------------------------------

    def log_raw_bar(
        self,
        timestamp: str | datetime,
        open_: float,
        high: float,
        low: float,
        close: float,
        volume: float,
        contract_month: str,
    ) -> None:
        """Record a raw front-month 5-minute bar for future training."""
        ts = timestamp.isoformat() if isinstance(timestamp, datetime) else str(timestamp)
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO raw_front_month_bars "
            "(timestamp, open, high, low, close, volume, contract_month) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ts, open_, high, low, close, volume, contract_month),
        )
        conn.commit()

    def raw_bar_count(self) -> int:
        """Return total number of recorded raw front-month bars."""
        cur = self._get_conn().execute(
            "SELECT COUNT(*) FROM raw_front_month_bars"
        )
        return cur.fetchone()[0]

    def recent_raw_bars(self, n: int = 20) -> list[dict]:
        """Return the N most recent raw front-month bar entries."""
        conn = self._get_conn()
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM raw_front_month_bars ORDER BY id DESC LIMIT ?",
            (n,),
        ).fetchall()
        conn.row_factory = None
        return [dict(r) for r in rows]
