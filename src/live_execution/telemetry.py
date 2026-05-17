"""
Telemetry & Trade Ledger for Live Execution.

Lightweight SQLite backend that records:
- market_bars: every closed 5-minute bar (builds our own historical dataset)
- trade_ledger: every signal generated and every order executed

Author: CL Analyst
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from src.data_paths import get_data_path as _get_data_path

_DEFAULT_DB_PATH = str(_get_data_path("live_telemetry.db"))

_CREATE_MARKET_BARS = """
CREATE TABLE IF NOT EXISTS market_bars (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT    NOT NULL UNIQUE,
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
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(timestamp, contract_month)
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

_CREATE_SHADOW_LOG = """
CREATE TABLE IF NOT EXISTS shadow_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT    NOT NULL UNIQUE,
    open            REAL    NOT NULL,
    high            REAL    NOT NULL,
    low             REAL    NOT NULL,
    close           REAL    NOT NULL,
    volume          REAL    NOT NULL,
    features_json   TEXT,                     -- full feature vector as JSON
    prob_buy        REAL,                     -- model probability (long)
    prob_sell       REAL,                     -- model probability (short)
    strategy_name   TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""

_CREATE_SHADOW_LOG_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_shadow_ts ON shadow_log(timestamp);
"""

_CREATE_DECISION_STATE_LOG = """
CREATE TABLE IF NOT EXISTS decision_state_log (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id                TEXT    NOT NULL,
    event_type              TEXT    NOT NULL,   -- ENTRY / BRACKET_PLACED / TRAILING_ACTIVATED
    event_timestamp_utc     TEXT    NOT NULL,
    entry_price             REAL,
    position_side           INTEGER,           -- +1 long, -1 short
    atr_at_entry            REAL,
    bracket_atr             REAL,
    tp_price                REAL,
    sl_price                REAL,
    trailing_atr_mult       REAL,
    trailing_sl_atr_offset  REAL,
    trailing_activated      INTEGER DEFAULT 0,
    highest_high            REAL,
    lowest_low              REAL,
    bars_held               INTEGER,
    state_json              TEXT,              -- full state dict as JSON fallback
    created_at              TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""

_CREATE_DECISION_STATE_LOG_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_dsl_trade_id ON decision_state_log(trade_id);
CREATE INDEX IF NOT EXISTS idx_dsl_event_ts ON decision_state_log(event_timestamp_utc);
"""

_CREATE_ACTIVE_POSITIONS = """
CREATE TABLE IF NOT EXISTS active_positions (
    trade_id            TEXT    PRIMARY KEY,
    status              TEXT    NOT NULL DEFAULT 'OPEN',   -- OPEN / CLOSED
    side                TEXT    NOT NULL,                  -- LONG / SHORT
    quantity            INTEGER NOT NULL,
    entry_price         REAL    NOT NULL,
    entry_order_id      INTEGER,
    tp_order_id         INTEGER,
    sl_order_id         INTEGER,
    tp_price            REAL,
    sl_price            REAL,
    atr_at_entry        REAL,
    entry_time          TEXT    NOT NULL,
    entry_bar_time      TEXT,
    close_time          TEXT,
    close_reason        TEXT,
    exit_price          REAL,
    bars_held           INTEGER,
    trailing_atr_mult   REAL,
    max_hold_bars       INTEGER,
    created_at          TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""

_CREATE_ACTIVE_POSITIONS_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_active_pos_status ON active_positions(status);
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
            + _CREATE_SHADOW_LOG
            + _CREATE_DECISION_STATE_LOG
            + _CREATE_ACTIVE_POSITIONS
            + _CREATE_INDEXES
            + _CREATE_TRADEBOOK_INDEXES
            + _CREATE_SHADOW_LOG_INDEXES
            + _CREATE_DECISION_STATE_LOG_INDEXES
            + _CREATE_ACTIVE_POSITIONS_INDEXES
        )
        self._migrate_trade_ledger_columns(conn)
        self._migrate_unique_constraints(conn)
        self._migrate_active_positions_columns(conn)
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

    def _migrate_unique_constraints(self, conn: sqlite3.Connection) -> None:
        """Add unique indexes to market_bars and raw_front_month_bars for
        existing databases that predate the UNIQUE constraint.

        De-duplicates any existing data first (keeps the earliest row per
        timestamp), then creates a UNIQUE INDEX.
        """
        # Check if unique index already exists for market_bars
        indexes = {
            row[1] for row in conn.execute("PRAGMA index_list(market_bars)").fetchall()
        }
        if "uq_market_bars_ts" not in indexes:
            # De-duplicate: keep the row with the smallest id per timestamp
            conn.execute(
                "DELETE FROM market_bars WHERE id NOT IN "
                "(SELECT MIN(id) FROM market_bars GROUP BY timestamp)"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_market_bars_ts "
                "ON market_bars(timestamp)"
            )

        # Check if unique index already exists for raw_front_month_bars
        indexes = {
            row[1] for row in conn.execute(
                "PRAGMA index_list(raw_front_month_bars)"
            ).fetchall()
        }
        if "uq_raw_bars_ts_month" not in indexes:
            conn.execute(
                "DELETE FROM raw_front_month_bars WHERE id NOT IN "
                "(SELECT MIN(id) FROM raw_front_month_bars "
                " GROUP BY timestamp, contract_month)"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_raw_bars_ts_month "
                "ON raw_front_month_bars(timestamp, contract_month)"
            )

    def _migrate_active_positions_columns(self, conn: sqlite3.Connection) -> None:
        """Add newer nullable columns to active_positions for older DB files."""
        cols = {
            row[1] for row in conn.execute("PRAGMA table_info(active_positions)").fetchall()
        }
        if "exit_price" not in cols:
            conn.execute("ALTER TABLE active_positions ADD COLUMN exit_price REAL")

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
            "INSERT OR IGNORE INTO market_bars (timestamp, open, high, low, close, volume) "
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
            "INSERT OR IGNORE INTO raw_front_month_bars "
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
    # Shadow log (State Parity Test)
    # ------------------------------------------------------------------

    @staticmethod
    def _sanitize_float(val: object) -> object:
        """Convert NaN/inf to None and robustly cast objects for safe SQLite JSON insertion."""
        if val is None:
            return None
        # Handle numpy float types that crash json.dumps
        if isinstance(val, (np.float32, np.float64, np.floating, np.integer)):
            val = float(val)
        # Handle timestamps/dates that crash json.dumps
        if isinstance(val, (pd.Timestamp, datetime)):
            return val.isoformat()

        try:
            f = float(val)
            if math.isnan(f) or math.isinf(f):
                return None
            return f
        except (TypeError, ValueError):
            # If it cannot be parsed as a float and isn't natively supported, 
            # cast to string to prevent json.dumps() from throwing a fatal TypeError.
            if isinstance(val, (str, bool, int, list, dict)):
                return val
            return str(val)

    def log_shadow_state(
        self,
        timestamp: str | datetime,
        open_: float,
        high: float,
        low: float,
        close: float,
        volume: float,
        features_dict: dict | None = None,
        prob_buy: float | None = None,
        prob_sell: float | None = None,
        strategy_name: str | None = None,
    ) -> None:
        """Record a shadow-replay state row for parity validation."""
        ts = timestamp.isoformat() if isinstance(timestamp, datetime) else str(timestamp)

        # Serialize features dict to JSON, sanitizing NaN/inf
        features_json = None
        if features_dict is not None:
            sanitized = {
                k: self._sanitize_float(v) for k, v in features_dict.items()
            }
            features_json = json.dumps(sanitized)

        conn = self._get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO shadow_log "
            "(timestamp, open, high, low, close, volume, "
            " features_json, prob_buy, prob_sell, strategy_name) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ts, open_, high, low, close, volume,
                features_json,
                self._sanitize_float(prob_buy),
                self._sanitize_float(prob_sell),
                strategy_name,
            ),
        )
        conn.commit()

    def shadow_log_count(self) -> int:
        """Return total number of shadow log entries."""
        cur = self._get_conn().execute("SELECT COUNT(*) FROM shadow_log")
        return cur.fetchone()[0]

    # ------------------------------------------------------------------
    # Decision state log (execution parity snapshots)
    # ------------------------------------------------------------------

    def log_decision_state(
        self,
        *,
        trade_id: str,
        event_type: str,
        event_timestamp_utc: str,
        entry_price: Optional[float] = None,
        position_side: Optional[int] = None,
        atr_at_entry: Optional[float] = None,
        bracket_atr: Optional[float] = None,
        tp_price: Optional[float] = None,
        sl_price: Optional[float] = None,
        trailing_atr_mult: Optional[float] = None,
        trailing_sl_atr_offset: Optional[float] = None,
        trailing_activated: bool = False,
        highest_high: Optional[float] = None,
        lowest_low: Optional[float] = None,
        bars_held: Optional[int] = None,
        state_json: Optional[str] = None,
    ) -> None:
        """Record a decision-state snapshot for execution parity auditing."""
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO decision_state_log ("
            " trade_id, event_type, event_timestamp_utc, entry_price,"
            " position_side, atr_at_entry, bracket_atr, tp_price, sl_price,"
            " trailing_atr_mult, trailing_sl_atr_offset, trailing_activated,"
            " highest_high, lowest_low, bars_held, state_json"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                trade_id, event_type, event_timestamp_utc,
                self._sanitize_float(entry_price),
                position_side,
                self._sanitize_float(atr_at_entry),
                self._sanitize_float(bracket_atr),
                self._sanitize_float(tp_price),
                self._sanitize_float(sl_price),
                self._sanitize_float(trailing_atr_mult),
                self._sanitize_float(trailing_sl_atr_offset),
                1 if trailing_activated else 0,
                self._sanitize_float(highest_high),
                self._sanitize_float(lowest_low),
                bars_held,
                state_json,
            ),
        )
        conn.commit()

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

    # ------------------------------------------------------------------
    # Active positions (persistent position ledger)
    # ------------------------------------------------------------------

    def open_position(
        self,
        *,
        trade_id: str,
        side: str,
        quantity: int,
        entry_price: float,
        entry_order_id: Optional[int] = None,
        atr_at_entry: Optional[float] = None,
        entry_time: str,
        entry_bar_time: Optional[str] = None,
        trailing_atr_mult: Optional[float] = None,
        max_hold_bars: Optional[int] = None,
    ) -> None:
        """Record a new position opening in the ledger."""
        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO active_positions "
            "(trade_id, status, side, quantity, entry_price, entry_order_id, "
            " atr_at_entry, entry_time, entry_bar_time, "
            " trailing_atr_mult, max_hold_bars) "
            "VALUES (?, 'OPEN', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                trade_id, side, quantity, entry_price, entry_order_id,
                self._sanitize_float(atr_at_entry),
                entry_time, entry_bar_time,
                self._sanitize_float(trailing_atr_mult),
                max_hold_bars,
            ),
        )
        conn.commit()

    def update_position_brackets(
        self,
        trade_id: str,
        *,
        tp_order_id: Optional[int] = None,
        sl_order_id: Optional[int] = None,
        tp_price: Optional[float] = None,
        sl_price: Optional[float] = None,
    ) -> None:
        """Update TP/SL order IDs and prices after bracket children are placed."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE active_positions "
            "SET tp_order_id = ?, sl_order_id = ?, tp_price = ?, sl_price = ? "
            "WHERE trade_id = ? AND status = 'OPEN'",
            (tp_order_id, sl_order_id, tp_price, sl_price, trade_id),
        )
        conn.commit()

    def update_position_sl(
        self,
        trade_id: str,
        *,
        new_sl_price: float,
        sl_order_id: Optional[int] = None,
    ) -> None:
        """Update SL price after trailing stop modification."""
        conn = self._get_conn()
        if sl_order_id is not None:
            conn.execute(
                "UPDATE active_positions "
                "SET sl_price = ?, sl_order_id = ? "
                "WHERE trade_id = ? AND status = 'OPEN'",
                (new_sl_price, sl_order_id, trade_id),
            )
        else:
            conn.execute(
                "UPDATE active_positions SET sl_price = ? "
                "WHERE trade_id = ? AND status = 'OPEN'",
                (new_sl_price, trade_id),
            )
        conn.commit()

    def close_position(
        self,
        trade_id: str,
        *,
        reason: str,
        close_time: str,
        bars_held: Optional[int] = None,
        exit_price: Optional[float] = None,
    ) -> None:
        """Mark a position as closed in the ledger."""
        conn = self._get_conn()
        conn.execute(
            "UPDATE active_positions "
            "SET status = 'CLOSED', close_reason = ?, close_time = ?, "
            "    bars_held = ?, exit_price = ? "
            "WHERE trade_id = ? AND status = 'OPEN'",
            (reason, close_time, bars_held,
             self._sanitize_float(exit_price), trade_id),
        )
        conn.commit()

    def get_open_position(self) -> Optional[dict]:
        """Return the currently open position, or None if flat.

        At most one position should be OPEN at any time.
        If multiple are found, returns the most recently created.
        """
        conn = self._get_conn()
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM active_positions "
            "WHERE status = 'OPEN' "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        conn.row_factory = None
        return dict(row) if row else None

    def export_trade_ledger(self) -> pd.DataFrame:
        """Export closed positions as a DataFrame with the unified schema.

        Returns a DataFrame with columns matching BacktestResult.to_dataframe()
        so the reconciliation script can diff backtest vs live trades.
        """
        conn = self._get_conn()
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM active_positions "
            "WHERE status = 'CLOSED' "
            "ORDER BY entry_time ASC"
        ).fetchall()
        conn.row_factory = None

        if not rows:
            return pd.DataFrame()

        records = []
        for row in rows:
            d = dict(row)
            records.append({
                "entry_time": pd.Timestamp(d.get("entry_bar_time") or d["entry_time"]),
                "signal_side": d["side"],
                "entry_price": d["entry_price"],
                "initial_tp_price": d.get("tp_price"),
                "initial_sl_price": d.get("sl_price"),
                "exit_time": pd.Timestamp(d["close_time"]) if d.get("close_time") else None,
                "exit_price": d.get("exit_price"),
                "exit_reason": d.get("close_reason"),
                "atr_at_entry": d.get("atr_at_entry"),
                "duration_bars": d.get("bars_held"),
                "lots": d.get("quantity", 1),
                "trade_id": d["trade_id"],
            })
        return pd.DataFrame(records)
