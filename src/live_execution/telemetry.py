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
    signal_id       TEXT,                     -- stable signal ID for joins
    decision_id     TEXT,                     -- stable decision ID for joins
    decision_timestamp_utc TEXT,              -- decision timestamp for latency analysis
    exit_reason     TEXT,                     -- REASON_TIMEOUT / REASON_TP / REASON_SL
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

_CREATE_TRADEBOOK_EVENTS = """
CREATE TABLE IF NOT EXISTS tradebook_events (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id                TEXT    NOT NULL UNIQUE,   -- idempotency key
    event_type              TEXT    NOT NULL,          -- ORDER_SUBMITTED / ORDER_STATUS / EXECUTION_FILL / COMMISSION
    event_timestamp_utc     TEXT    NOT NULL,          -- broker/event timestamp
    decision_timestamp_utc  TEXT,                      -- decision-side timestamp for latency joins
    signal_id               TEXT,                      -- join key to trade_ledger
    decision_id             TEXT,                      -- join key to trade_ledger
    order_id                INTEGER,
    perm_id                 INTEGER,
    parent_order_id         INTEGER,
    broker_execution_id     TEXT,
    account                 TEXT,
    environment             TEXT,
    symbol                  TEXT,
    local_symbol            TEXT,                      -- e.g., CLH5
    contract_month          TEXT,                      -- e.g., 202503
    side                    TEXT,
    action                  TEXT,
    order_type              TEXT,
    time_in_force           TEXT,
    status                  TEXT,
    order_qty               REAL,
    fill_qty                REAL,
    cum_fill_qty            REAL,
    remaining_qty           REAL,
    avg_fill_price          REAL,
    last_fill_price         REAL,
    limit_price             REAL,
    stop_price              REAL,
    commission              REAL,
    fees                    REAL,
    slippage_estimate       REAL,
    realized_pnl            REAL,
    unrealized_pnl          REAL,
    run_id                  TEXT,
    session_id              TEXT,
    hostname                TEXT,
    process_id              INTEGER,
    created_at              TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""

_CREATE_TRADEBOOK_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_tradebook_ts ON tradebook_events(event_timestamp_utc);
CREATE INDEX IF NOT EXISTS idx_tradebook_order_id ON tradebook_events(order_id);
CREATE INDEX IF NOT EXISTS idx_tradebook_exec_id ON tradebook_events(broker_execution_id);
CREATE INDEX IF NOT EXISTS idx_tradebook_signal_id ON tradebook_events(signal_id);
CREATE INDEX IF NOT EXISTS idx_tradebook_decision_id ON tradebook_events(decision_id);
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
            + _CREATE_TRADEBOOK_EVENTS
            + _CREATE_INDEXES
            + _CREATE_TRADEBOOK_INDEXES
        )
        self._migrate_trade_ledger_columns(conn)
        conn.commit()

    def _migrate_trade_ledger_columns(self, conn: sqlite3.Connection) -> None:
        """Add newer nullable columns to trade_ledger for older DB files."""
        cols = {
            row[1] for row in conn.execute("PRAGMA table_info(trade_ledger)").fetchall()
        }
        if "signal_id" not in cols:
            conn.execute("ALTER TABLE trade_ledger ADD COLUMN signal_id TEXT")
        if "decision_id" not in cols:
            conn.execute("ALTER TABLE trade_ledger ADD COLUMN decision_id TEXT")
        if "decision_timestamp_utc" not in cols:
            conn.execute(
                "ALTER TABLE trade_ledger ADD COLUMN decision_timestamp_utc TEXT"
            )
        if "exit_reason" not in cols:
            conn.execute("ALTER TABLE trade_ledger ADD COLUMN exit_reason TEXT")

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
        signal_id: Optional[str] = None,
        decision_id: Optional[str] = None,
        decision_timestamp_utc: Optional[str] = None,
        exit_reason: Optional[str] = None,
    ) -> None:
        """Record a generated signal and any resulting action."""
        ts = timestamp.isoformat() if isinstance(timestamp, datetime) else str(timestamp)
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO trade_ledger "
            "(timestamp, signal, confidence_pct, action_taken, order_id, "
            " fill_price, direction, tp_price, sl_price, atr_value, current_price, "
            " signal_id, decision_id, decision_timestamp_utc, exit_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ts, signal, confidence_pct, action_taken,
                order_id, fill_price, direction,
                tp_price, sl_price, atr_value, current_price,
                signal_id, decision_id, decision_timestamp_utc,
                exit_reason,
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

    def trade_summary(self) -> dict:
        """Return aggregate trade history summary for startup report.

        Returns a dict with:
            total_signals: int — all signals logged
            executed_trades: int — trades with action_taken='EXECUTE'
            first_signal: str | None — timestamp of earliest signal
            last_signal: str | None — timestamp of most recent signal
            total_bars: int — total bars recorded
        """
        conn = self._get_conn()

        total_signals = conn.execute(
            "SELECT COUNT(*) FROM trade_ledger"
        ).fetchone()[0]

        executed_trades = conn.execute(
            "SELECT COUNT(*) FROM trade_ledger WHERE action_taken = 'EXECUTE'"
        ).fetchone()[0]

        row = conn.execute(
            "SELECT MIN(timestamp), MAX(timestamp) FROM trade_ledger"
        ).fetchone()
        first_signal = row[0] if row else None
        last_signal = row[1] if row else None

        total_bars = conn.execute(
            "SELECT COUNT(*) FROM market_bars"
        ).fetchone()[0]

        return {
            "total_signals": total_signals,
            "executed_trades": executed_trades,
            "first_signal": first_signal,
            "last_signal": last_signal,
            "total_bars": total_bars,
        }

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

    # ------------------------------------------------------------------
    # Tradebook events (execution lifecycle)
    # ------------------------------------------------------------------

    def log_tradebook_event(
        self,
        *,
        event_id: str,
        event_type: str,
        event_timestamp_utc: str | datetime,
        decision_timestamp_utc: Optional[str] = None,
        signal_id: Optional[str] = None,
        decision_id: Optional[str] = None,
        order_id: Optional[int] = None,
        perm_id: Optional[int] = None,
        parent_order_id: Optional[int] = None,
        broker_execution_id: Optional[str] = None,
        account: Optional[str] = None,
        environment: Optional[str] = None,
        symbol: Optional[str] = None,
        local_symbol: Optional[str] = None,
        contract_month: Optional[str] = None,
        side: Optional[str] = None,
        action: Optional[str] = None,
        order_type: Optional[str] = None,
        time_in_force: Optional[str] = None,
        status: Optional[str] = None,
        order_qty: Optional[float] = None,
        fill_qty: Optional[float] = None,
        cum_fill_qty: Optional[float] = None,
        remaining_qty: Optional[float] = None,
        avg_fill_price: Optional[float] = None,
        last_fill_price: Optional[float] = None,
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
        commission: Optional[float] = None,
        fees: Optional[float] = None,
        slippage_estimate: Optional[float] = None,
        realized_pnl: Optional[float] = None,
        unrealized_pnl: Optional[float] = None,
        run_id: Optional[str] = None,
        session_id: Optional[str] = None,
        hostname: Optional[str] = None,
        process_id: Optional[int] = None,
    ) -> bool:
        """Insert one append-only tradebook event row (idempotent by event_id)."""
        ts = (
            event_timestamp_utc.isoformat()
            if isinstance(event_timestamp_utc, datetime)
            else str(event_timestamp_utc)
        )
        conn = self._get_conn()
        cur = conn.execute(
            "INSERT OR IGNORE INTO tradebook_events ("
            " event_id, event_type, event_timestamp_utc, decision_timestamp_utc,"
            " signal_id, decision_id, order_id, perm_id, parent_order_id,"
            " broker_execution_id, account, environment, symbol, local_symbol,"
            " contract_month, side, action, order_type, time_in_force, status,"
            " order_qty, fill_qty, cum_fill_qty, remaining_qty, avg_fill_price,"
            " last_fill_price, limit_price, stop_price, commission, fees,"
            " slippage_estimate, realized_pnl, unrealized_pnl, run_id, session_id,"
            " hostname, process_id"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
            " ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id, event_type, ts, decision_timestamp_utc,
                signal_id, decision_id, order_id, perm_id, parent_order_id,
                broker_execution_id, account, environment, symbol, local_symbol,
                contract_month, side, action, order_type, time_in_force, status,
                order_qty, fill_qty, cum_fill_qty, remaining_qty, avg_fill_price,
                last_fill_price, limit_price, stop_price, commission, fees,
                slippage_estimate, realized_pnl, unrealized_pnl, run_id, session_id,
                hostname, process_id,
            ),
        )
        conn.commit()
        return cur.rowcount > 0

    def recent_tradebook_events(self, n: int = 50) -> list[dict]:
        """Return the N most recent tradebook events."""
        conn = self._get_conn()
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM tradebook_events ORDER BY id DESC LIMIT ?",
            (n,),
        ).fetchall()
        conn.row_factory = None
        return [dict(r) for r in rows]

    def read_tradebook(
        self,
        *,
        signal_id: Optional[str] = None,
        order_id: Optional[int] = None,
        limit: int = 1000,
    ) -> list[dict]:
        """Read normalized tradebook rows for reporting/analysis."""
        conn = self._get_conn()
        conn.row_factory = sqlite3.Row

        where_parts = []
        params: list[object] = []
        if signal_id is not None:
            where_parts.append("signal_id = ?")
            params.append(signal_id)
        if order_id is not None:
            where_parts.append("order_id = ?")
            params.append(order_id)

        query = "SELECT * FROM tradebook_events"
        if where_parts:
            query += " WHERE " + " AND ".join(where_parts)
        query += " ORDER BY event_timestamp_utc ASC, id ASC LIMIT ?"
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
        conn.row_factory = None
        return [dict(r) for r in rows]
