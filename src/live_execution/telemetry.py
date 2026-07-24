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
from datetime import datetime, timedelta
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
    initial_sl_price    REAL,              -- original SL before trailing modifications
    atr_at_entry        REAL,
    entry_time          TEXT    NOT NULL,
    entry_bar_time      TEXT,
    close_time          TEXT,
    close_reason        TEXT,
    exit_price          REAL,
    bars_held           INTEGER,
    trailing_atr_mult   REAL,
    max_hold_bars       INTEGER,
    trailing_activated  INTEGER DEFAULT 0,  -- latch: restart recovery restores it (trailing-latch-reconnect-restore_07232026_1920)
    created_at          TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""

_CREATE_ACTIVE_POSITIONS_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_active_pos_status ON active_positions(status);
"""

# ---------------------------------------------------------------------------
# FLEET (shared-DB) schema v2 — one DB file, many bots.
#
# Differences from the legacy single-bot schema, each load-bearing:
# - market_bars / raw_front_month_bars / shadow_log lose their
#   timestamp-only UNIQUE: with two symbols in one file, INSERT OR IGNORE
#   against UNIQUE(timestamp) silently DROPS the second symbol's bar.
#   Rekeyed to (symbol, timestamp[, contract_month]) / (client_id, timestamp).
# - active_positions loses trade_id as PRIMARY KEY: trade_ids are built from
#   per-connection IBKR order ids ("trade_5"), so two bots can collide and
#   INSERT OR REPLACE would clobber the other bot's position row. Surrogate
#   id + UNIQUE(client_id, trade_id) instead.
# - identity columns are NOT NULL where uniqueness depends on them.
#
# A DB file is either legacy (PRAGMA user_version 0) or fleet (2) — opening
# one in the wrong mode RAISES (no silent cross-mode reads that would
# return empty results or clobber rows).
# ---------------------------------------------------------------------------

_FLEET_SCHEMA_VERSION = 2

_CREATE_MARKET_BARS_V2 = """
CREATE TABLE IF NOT EXISTS market_bars (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT    NOT NULL,
    client_id   INTEGER NOT NULL,
    timestamp   TEXT    NOT NULL,
    open        REAL    NOT NULL,
    high        REAL    NOT NULL,
    low         REAL    NOT NULL,
    close       REAL    NOT NULL,
    volume      REAL    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(symbol, timestamp)
);
"""

_CREATE_RAW_FRONT_MONTH_BARS_V2 = """
CREATE TABLE IF NOT EXISTS raw_front_month_bars (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT    NOT NULL,
    client_id       INTEGER NOT NULL,
    timestamp       TEXT    NOT NULL,
    open            REAL    NOT NULL,
    high            REAL    NOT NULL,
    low             REAL    NOT NULL,
    close           REAL    NOT NULL,
    volume          REAL    NOT NULL,
    contract_month  TEXT    NOT NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(symbol, timestamp, contract_month)
);
"""

_CREATE_SHADOW_LOG_V2 = """
CREATE TABLE IF NOT EXISTS shadow_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT    NOT NULL,
    client_id       INTEGER NOT NULL,
    timestamp       TEXT    NOT NULL,
    open            REAL    NOT NULL,
    high            REAL    NOT NULL,
    low             REAL    NOT NULL,
    close           REAL    NOT NULL,
    volume          REAL    NOT NULL,
    features_json   TEXT,
    prob_buy        REAL,
    prob_sell       REAL,
    strategy_name   TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(client_id, timestamp)
);
"""

_CREATE_ACTIVE_POSITIONS_V2 = """
CREATE TABLE IF NOT EXISTS active_positions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol              TEXT    NOT NULL,
    client_id           INTEGER NOT NULL,
    trade_id            TEXT    NOT NULL,
    status              TEXT    NOT NULL DEFAULT 'OPEN',
    side                TEXT    NOT NULL,
    quantity            INTEGER NOT NULL,
    entry_price         REAL    NOT NULL,
    entry_order_id      INTEGER,
    tp_order_id         INTEGER,
    sl_order_id         INTEGER,
    tp_price            REAL,
    sl_price            REAL,
    initial_sl_price    REAL,
    atr_at_entry        REAL,
    entry_time          TEXT    NOT NULL,
    entry_bar_time      TEXT,
    close_time          TEXT,
    close_reason        TEXT,
    exit_price          REAL,
    bars_held           INTEGER,
    trailing_atr_mult   REAL,
    max_hold_bars       INTEGER,
    trailing_activated  INTEGER DEFAULT 0,  -- latch: restart recovery restores it (trailing-latch-reconnect-restore_07232026_1920)
    created_at          TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(client_id, trade_id)
);
"""


class TelemetryDB:
    """Lightweight SQLite telemetry backend for live execution.

    Two modes, chosen at construction and locked to the DB file:

    - **Legacy (unbound)** — ``TelemetryDB(path)``: the original one-bot-
      one-file behavior, byte-identical SQL. Refuses to open a fleet file.
    - **Fleet (bound)** — ``TelemetryDB(path, symbol=..., client_id=...)``:
      many bots share ONE file (SQLite WAL); every write is stamped with
      the bot's identity and every read is scoped to it (bar tables by
      symbol — bars are market data shared across same-symbol bots;
      strategy tables by client_id). Refuses to open a legacy file, whose
      timestamp-only UNIQUE constraints would silently drop cross-symbol
      rows.
    """

    def __init__(self, db_path: str = _DEFAULT_DB_PATH, *,
                 symbol: Optional[str] = None,
                 client_id: Optional[int] = None) -> None:
        if (symbol is None) != (client_id is None):
            raise ValueError(
                "TelemetryDB identity must be complete: pass BOTH symbol "
                f"and client_id (fleet mode) or NEITHER (legacy mode); got "
                f"symbol={symbol!r}, client_id={client_id!r}."
            )
        self.symbol = symbol
        self.client_id = client_id
        self._bound = symbol is not None
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._initialize()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _initialize(self) -> None:
        """Create tables and indexes if they don't exist.

        Enforces the mode <-> file pairing: a legacy file (user_version 0)
        opened in fleet mode, or a fleet file (user_version 2) opened in
        legacy mode, RAISES instead of silently misbehaving (empty scoped
        reads / cross-symbol row drops / cross-bot clobbers).
        """
        conn = self._get_conn()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        has_tables = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='market_bars'"
        ).fetchone() is not None

        if self._bound:
            if has_tables and version < _FLEET_SCHEMA_VERSION:
                raise ValueError(
                    f"{self.db_path} is a LEGACY single-bot telemetry DB — "
                    f"its timestamp-only UNIQUE constraints would silently "
                    f"drop cross-symbol rows in fleet mode. Point the fleet "
                    f"at a fresh shared DB file (old per-cid files stay on "
                    f"disk for history), or open this file without "
                    f"symbol/client_id for legacy access."
                )
            conn.executescript(
                _CREATE_MARKET_BARS_V2
                + _CREATE_TRADE_LEDGER
                + _CREATE_RAW_FRONT_MONTH_BARS_V2
                + _CREATE_TRADEBOOK_EVENTS
                + _CREATE_SHADOW_LOG_V2
                + _CREATE_DECISION_STATE_LOG
                + _CREATE_ACTIVE_POSITIONS_V2
                + _CREATE_INDEXES
                + _CREATE_TRADEBOOK_INDEXES
                + _CREATE_SHADOW_LOG_INDEXES
                + _CREATE_DECISION_STATE_LOG_INDEXES
                + _CREATE_ACTIVE_POSITIONS_INDEXES
            )
            self._migrate_fleet_identity_columns(conn)
            # Fleet files created before trailing-latch-reconnect-
            # restore_07232026_1920 predate active_positions.trailing_activated
            # — this path never ran the column migration (CREATE IF NOT
            # EXISTS keeps the old shape). Idempotent: PRAGMA-guarded.
            self._migrate_active_positions_columns(conn)
            conn.execute(f"PRAGMA user_version = {_FLEET_SCHEMA_VERSION}")
            conn.commit()
            return

        if version >= _FLEET_SCHEMA_VERSION:
            raise ValueError(
                f"{self.db_path} is a FLEET shared telemetry DB — open it "
                f"with an explicit identity: TelemetryDB(path, symbol=..., "
                f"client_id=...). Unscoped legacy access would silently "
                f"return empty results."
            )
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

    def _migrate_fleet_identity_columns(self, conn: sqlite3.Connection) -> None:
        """Add identity columns to the fleet tables created from legacy DDL
        (trade_ledger / decision_state_log / tradebook_events keep their
        legacy shape; identity is stamped by every bound write)."""
        for table, columns in (
            ("trade_ledger", ("symbol TEXT", "client_id INTEGER")),
            ("decision_state_log", ("symbol TEXT", "client_id INTEGER")),
            ("tradebook_events", ("client_id INTEGER",)),  # symbol exists
        ):
            existing = {
                row[1]
                for row in conn.execute(
                    f"PRAGMA table_info({table})"
                ).fetchall()
            }
            for col_def in columns:
                if col_def.split()[0] not in existing:
                    conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {col_def}"
                    )

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
        if "initial_sl_price" not in cols:
            conn.execute("ALTER TABLE active_positions ADD COLUMN initial_sl_price REAL")
        if "trailing_activated" not in cols:
            # trailing-latch-reconnect-restore_07232026_1920: pre-ticket
            # rows migrate to 0 (untrailed) — recovery must never invent a
            # True the ledger cannot prove.
            conn.execute(
                "ALTER TABLE active_positions "
                "ADD COLUMN trailing_activated INTEGER DEFAULT 0"
            )

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
            # Multiple fleet processes share one file: without a busy
            # timeout, two commits colliding raise "database is locked".
            self._conn.execute("PRAGMA busy_timeout = 10000;")
            try:
                self._conn.execute("PRAGMA journal_mode=WAL;")
            except sqlite3.OperationalError as e:
                import logging
                logging.getLogger("TelemetryDB").warning(
                    "Failed to set journal_mode=WAL (%s). Falling back to default journal mode. "
                    "This is common on WSL mounts (/mnt/c) or network shares.",
                    e
                )
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
        if self._bound:
            # (symbol, timestamp) unique: same-symbol bots dedup to one row
            # (bars are market data); different symbols never collide.
            conn.execute(
                "INSERT OR IGNORE INTO market_bars "
                "(symbol, client_id, timestamp, open, high, low, close, volume) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (self.symbol, self.client_id, ts, open_, high, low, close, volume),
            )
        else:
            conn.execute(
                "INSERT OR IGNORE INTO market_bars (timestamp, open, high, low, close, volume) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (ts, open_, high, low, close, volume),
            )
        conn.commit()

    def bar_count(self) -> int:
        """Return total number of recorded bars (this symbol's, in fleet mode)."""
        if self._bound:
            cur = self._get_conn().execute(
                "SELECT COUNT(*) FROM market_bars WHERE symbol = ?",
                (self.symbol,),
            )
        else:
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
        id_cols, id_marks, id_vals = self._identity_insert_fragments()
        conn.execute(
            "INSERT INTO trade_ledger "
            f"({id_cols}timestamp, signal, confidence_pct, action_taken, order_id, "
            " fill_price, direction, tp_price, sl_price, atr_value, current_price, "
            " signal_id, decision_id, decision_timestamp_utc, exit_reason) "
            f"VALUES ({id_marks}?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            id_vals + (
                ts, signal, confidence_pct, action_taken,
                order_id, fill_price, direction,
                tp_price, sl_price, atr_value, current_price,
                signal_id, decision_id, decision_timestamp_utc,
                exit_reason,
            ),
        )
        conn.commit()

    def _identity_insert_fragments(self) -> tuple:
        """SQL fragments stamping (symbol, client_id) on bound-mode inserts."""
        if self._bound:
            return ("symbol, client_id, ", "?, ?, ",
                    (self.symbol, self.client_id))
        return ("", "", ())

    def _client_scope(self, prefix: str = " AND ") -> tuple:
        """WHERE fragment scoping bound-mode reads to this bot's rows.

        IBKR order ids are per-connection sequences — unscoped, bot A's
        "order 5" UPDATE would hit bot B's row too.
        """
        if self._bound:
            return (f"{prefix}client_id = ?", (self.client_id,))
        return ("", ())

    def update_fill(self, order_id: int, fill_price: float) -> None:
        """Update the fill price for an existing order in the ledger."""
        conn = self._get_conn()
        scope_sql, scope_vals = self._client_scope()
        conn.execute(
            f"UPDATE trade_ledger SET fill_price = ? WHERE order_id = ?{scope_sql}",
            (fill_price, order_id) + scope_vals,
        )
        conn.commit()

    def signal_count(self) -> int:
        """Return total number of recorded signals."""
        scope_sql, scope_vals = self._client_scope(prefix=" WHERE ")
        cur = self._get_conn().execute(
            f"SELECT COUNT(*) FROM trade_ledger{scope_sql}", scope_vals
        )
        return cur.fetchone()[0]

    def trade_count(self) -> int:
        """Return number of executed trades (action_taken = 'EXECUTE')."""
        scope_sql, scope_vals = self._client_scope()
        cur = self._get_conn().execute(
            "SELECT COUNT(*) FROM trade_ledger "
            f"WHERE action_taken = 'EXECUTE'{scope_sql}",
            scope_vals,
        )
        return cur.fetchone()[0]

    def realized_pnl_total(self) -> float:
        """Cumulative realized PnL summed from persisted per-fill rows.

        Aggregates the tradebook's ``realized_pnl`` column — populated from
        each ``CommissionReport.realizedPNL`` (event_id-deduped via INSERT OR
        IGNORE, so no double-count) — scoped to this bot's client_id. Unlike
        IBKR's ``PortfolioItem.realizedPNL`` (a daily figure that resets and
        drops to $0 when the position leaves the portfolio feed), this is a
        restart-surviving lifetime total: it lives in the DB, spans every
        session, and never blinks to zero when flat. NULL realized values
        (opening fills) are ignored by SUM.
        """
        scope_sql, scope_vals = self._client_scope(prefix=" WHERE ")
        cur = self._get_conn().execute(
            f"SELECT COALESCE(SUM(realized_pnl), 0.0) FROM tradebook_events{scope_sql}",
            scope_vals,
        )
        return float(cur.fetchone()[0] or 0.0)

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
        where_sql, where_vals = self._client_scope(prefix=" WHERE ")
        and_sql, and_vals = self._client_scope()

        total_signals = conn.execute(
            f"SELECT COUNT(*) FROM trade_ledger{where_sql}", where_vals
        ).fetchone()[0]

        executed_trades = conn.execute(
            "SELECT COUNT(*) FROM trade_ledger "
            f"WHERE action_taken = 'EXECUTE'{and_sql}",
            and_vals,
        ).fetchone()[0]

        row = conn.execute(
            f"SELECT MIN(timestamp), MAX(timestamp) FROM trade_ledger{where_sql}",
            where_vals,
        ).fetchone()
        first_signal = row[0] if row else None
        last_signal = row[1] if row else None

        total_bars = self.bar_count()

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
        scope_sql, scope_vals = self._client_scope(prefix=" WHERE ")
        rows = conn.execute(
            f"SELECT * FROM trade_ledger{scope_sql} ORDER BY id DESC LIMIT ?",
            scope_vals + (n,),
        ).fetchall()
        conn.row_factory = None
        return [dict(r) for r in rows]

    def recent_bars(self, n: int = 20) -> list[dict]:
        """Return the N most recent bar entries."""
        conn = self._get_conn()
        conn.row_factory = sqlite3.Row
        if self._bound:
            rows = conn.execute(
                "SELECT * FROM market_bars WHERE symbol = ? "
                "ORDER BY id DESC LIMIT ?",
                (self.symbol, n),
            ).fetchall()
        else:
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
        id_cols, id_marks, id_vals = self._identity_insert_fragments()
        conn.execute(
            "INSERT OR IGNORE INTO raw_front_month_bars "
            f"({id_cols}timestamp, open, high, low, close, volume, contract_month) "
            f"VALUES ({id_marks}?, ?, ?, ?, ?, ?, ?)",
            id_vals + (ts, open_, high, low, close, volume, contract_month),
        )
        conn.commit()

    def raw_bar_count(self) -> int:
        """Return total number of recorded raw front-month bars."""
        if self._bound:
            cur = self._get_conn().execute(
                "SELECT COUNT(*) FROM raw_front_month_bars WHERE symbol = ?",
                (self.symbol,),
            )
        else:
            cur = self._get_conn().execute(
                "SELECT COUNT(*) FROM raw_front_month_bars"
            )
        return cur.fetchone()[0]

    def recent_raw_bars(self, n: int = 20) -> list[dict]:
        """Return the N most recent raw front-month bar entries."""
        conn = self._get_conn()
        conn.row_factory = sqlite3.Row
        if self._bound:
            rows = conn.execute(
                "SELECT * FROM raw_front_month_bars WHERE symbol = ? "
                "ORDER BY id DESC LIMIT ?",
                (self.symbol, n),
            ).fetchall()
        else:
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
        id_cols, id_marks, id_vals = self._identity_insert_fragments()
        conn.execute(
            "INSERT OR IGNORE INTO shadow_log "
            f"({id_cols}timestamp, open, high, low, close, volume, "
            " features_json, prob_buy, prob_sell, strategy_name) "
            f"VALUES ({id_marks}?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            id_vals + (
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
        scope_sql, scope_vals = self._client_scope(prefix=" WHERE ")
        cur = self._get_conn().execute(
            f"SELECT COUNT(*) FROM shadow_log{scope_sql}", scope_vals
        )
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
        id_cols, id_marks, id_vals = self._identity_insert_fragments()
        conn.execute(
            "INSERT INTO decision_state_log ("
            f" {id_cols}trade_id, event_type, event_timestamp_utc, entry_price,"
            " position_side, atr_at_entry, bracket_atr, tp_price, sl_price,"
            " trailing_atr_mult, trailing_sl_atr_offset, trailing_activated,"
            " highest_high, lowest_low, bars_held, state_json"
            f") VALUES ({id_marks}?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            id_vals + (
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
        if self._bound and symbol is None:
            # Caller-provided symbol (e.g. execution local symbol) wins;
            # otherwise stamp the bot's brain symbol.
            symbol = self.symbol
        # Legacy tables have no client_id column — only stamp when bound.
        if self._bound:
            cid_col, cid_mark, cid_vals = "client_id, ", "?, ", (self.client_id,)
        else:
            cid_col, cid_mark, cid_vals = "", "", ()
        cur = conn.execute(
            "INSERT OR IGNORE INTO tradebook_events ("
            f" {cid_col}"
            " event_id, event_type, event_timestamp_utc, decision_timestamp_utc,"
            " signal_id, decision_id, order_id, perm_id, parent_order_id,"
            " broker_execution_id, account, environment, symbol, local_symbol,"
            " contract_month, side, action, order_type, time_in_force, status,"
            " order_qty, fill_qty, cum_fill_qty, remaining_qty, avg_fill_price,"
            " last_fill_price, limit_price, stop_price, commission, fees,"
            " slippage_estimate, realized_pnl, unrealized_pnl, run_id, session_id,"
            " hostname, process_id"
            f") VALUES ({cid_mark}?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
            " ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            cid_vals + (
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
        scope_sql, scope_vals = self._client_scope(prefix=" WHERE ")
        rows = conn.execute(
            f"SELECT * FROM tradebook_events{scope_sql} ORDER BY id DESC LIMIT ?",
            scope_vals + (n,),
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
        if self._bound:
            where_parts.append("client_id = ?")
            params.append(self.client_id)
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
        id_cols, id_marks, id_vals = self._identity_insert_fragments()
        # Bound mode replaces on UNIQUE(client_id, trade_id) — trade_ids are
        # built from per-connection IBKR order ids and DO collide across
        # bots; unscoped OR REPLACE would clobber another bot's row.
        conn.execute(
            "INSERT OR REPLACE INTO active_positions "
            f"({id_cols}trade_id, status, side, quantity, entry_price, entry_order_id, "
            " atr_at_entry, entry_time, entry_bar_time, "
            " trailing_atr_mult, max_hold_bars) "
            f"VALUES ({id_marks}?, 'OPEN', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            id_vals + (
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
        """Update TP/SL order IDs and prices after bracket children are placed.

        Also stores the initial SL price (before trailing stop modifications)
        if initial_sl_price has not been set yet.
        """
        conn = self._get_conn()
        scope_sql, scope_vals = self._client_scope()
        conn.execute(
            "UPDATE active_positions "
            "SET tp_order_id = ?, sl_order_id = ?, tp_price = ?, sl_price = ?, "
            "    initial_sl_price = COALESCE(initial_sl_price, ?) "
            f"WHERE trade_id = ? AND status = 'OPEN'{scope_sql}",
            (tp_order_id, sl_order_id, tp_price, sl_price, sl_price, trade_id)
            + scope_vals,
        )
        conn.commit()

    def update_position_sl(
        self,
        trade_id: str,
        *,
        new_sl_price: float,
        sl_order_id: Optional[int] = None,
        trailing_activated: Optional[bool] = None,
    ) -> None:
        """Update SL price after trailing stop modification.

        ``trailing_activated=True`` additionally stamps the row's latch
        column in the SAME statement as the trailed price
        (trailing-latch-reconnect-restore_07232026_1920) so restart
        recovery can restore ``_trailing_activated``. ``None`` (the
        default) leaves the column untouched — heal paths that re-place an
        un-trailed SL must not clear an armed latch.
        """
        conn = self._get_conn()
        scope_sql, scope_vals = self._client_scope()
        set_fragments = ["sl_price = ?"]
        set_vals: list = [new_sl_price]
        if sl_order_id is not None:
            set_fragments.append("sl_order_id = ?")
            set_vals.append(sl_order_id)
        if trailing_activated is not None:
            set_fragments.append("trailing_activated = ?")
            set_vals.append(1 if trailing_activated else 0)
        conn.execute(
            f"UPDATE active_positions SET {', '.join(set_fragments)} "
            f"WHERE trade_id = ? AND status = 'OPEN'{scope_sql}",
            tuple(set_vals) + (trade_id,) + scope_vals,
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
        scope_sql, scope_vals = self._client_scope()
        conn.execute(
            "UPDATE active_positions "
            "SET status = 'CLOSED', close_reason = ?, close_time = ?, "
            "    bars_held = ?, exit_price = ? "
            f"WHERE trade_id = ? AND status = 'OPEN'{scope_sql}",
            (reason, close_time, bars_held,
             self._sanitize_float(exit_price), trade_id) + scope_vals,
        )
        conn.commit()

    def repair_closed_position(
        self,
        trade_id: str,
        *,
        exit_price: float,
        reason: str,
        allow_overwrite_reasons,
    ) -> int:
        """Repair a CLOSED row's exit_price/close_reason from a proven fill.

        A-9 (hourly housekeeping, A-1(b)): the UPDATE is scoped to
        trade_id AND status='CLOSED' AND (exit_price IS NULL OR
        close_reason IN ``allow_overwrite_reasons``) AND this instance's
        client binding on the shared fleet DB — trade_ids are built from
        per-connection IBKR order ids, so a fleet-mate's identical
        trade_id must never be touched (54dc110 collision class). Rows
        outside the whitelist with a real price (TP_HIT/SL_HIT/...) are
        structurally unreachable. Returns the UPDATE rowcount so the
        caller can distinguish "repaired" from "already truthful".
        """
        reasons = tuple(allow_overwrite_reasons)
        conn = self._get_conn()
        scope_sql, scope_vals = self._client_scope()
        marks = ", ".join("?" for _ in reasons)
        cur = conn.execute(
            "UPDATE active_positions "
            "SET exit_price = ?, close_reason = ? "
            "WHERE trade_id = ? AND status = 'CLOSED' "
            f"AND (exit_price IS NULL OR close_reason IN ({marks}))"
            f"{scope_sql}",
            (self._sanitize_float(exit_price), reason, trade_id)
            + reasons + scope_vals,
        )
        conn.commit()
        return cur.rowcount

    def get_recent_closed_positions(self, hours: int = 48) -> list[dict]:
        """Return this client's CLOSED rows with close_time in the window.

        A-9 read helper for the hourly housekeeping sweep's orphan and
        ledger-repair matching (rows carry tp_order_id/sl_order_id/
        close_reason/exit_price). Client-scoped: order ids are
        per-connection sequences, so cross-bot rows would false-match.
        """
        cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        conn = self._get_conn()
        conn.row_factory = sqlite3.Row
        scope_sql, scope_vals = self._client_scope()
        rows = conn.execute(
            "SELECT * FROM active_positions "
            f"WHERE status = 'CLOSED' AND close_time >= ?{scope_sql} "
            "ORDER BY close_time DESC",
            (cutoff,) + scope_vals,
        ).fetchall()
        conn.row_factory = None
        return [dict(r) for r in rows]

    def get_open_position(self) -> Optional[dict]:
        """Return the currently open position, or None if flat.

        At most one position should be OPEN at any time.
        If multiple are found, returns the most recently created.
        """
        conn = self._get_conn()
        conn.row_factory = sqlite3.Row
        # In fleet mode "at most one OPEN position" holds PER BOT — the
        # client_id scope is what stops a bot adopting a fleet-mate's
        # position during reconnect recovery.
        scope_sql, scope_vals = self._client_scope()
        row = conn.execute(
            "SELECT * FROM active_positions "
            f"WHERE status = 'OPEN'{scope_sql} "
            "ORDER BY created_at DESC LIMIT 1",
            scope_vals,
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
        scope_sql, scope_vals = self._client_scope()
        rows = conn.execute(
            "SELECT * FROM active_positions "
            f"WHERE status = 'CLOSED'{scope_sql} "
            "ORDER BY entry_time ASC",
            scope_vals,
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
                "initial_sl_price": d.get("initial_sl_price") or d.get("sl_price"),
                "exit_time": pd.Timestamp(d["close_time"]) if d.get("close_time") else None,
                "exit_price": d.get("exit_price"),
                "exit_reason": d.get("close_reason"),
                "atr_at_entry": d.get("atr_at_entry"),
                "duration_bars": d.get("bars_held"),
                "lots": d.get("quantity", 1),
                "trade_id": d["trade_id"],
            })
        return pd.DataFrame(records)
