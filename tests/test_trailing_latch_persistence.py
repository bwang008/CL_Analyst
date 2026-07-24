"""Ticket trailing-latch-reconnect-restore_07232026_1920.

Root cause (findings.md, proven from reports/fleet/fleet_20260722.log):
the `_trailing_activated` latch was never lost mid-session — both spurious
re-activations (NG order 110, 19:15:06 and 23:00:05 PT on 2026-07-22)
immediately follow PROCESS RESTARTS whose ledger recovery restores every
piece of position state EXCEPT the latch. The execDetails-replay
hypothesis is refuted: no re-activation fired across the 21:04-21:22
reconnect flap while the trigger condition was continuously true.

The restore also had no persistence to restore FROM: `active_positions`
never had a `trailing_activated` column (telemetry.py:170 belongs to
`decision_state_log`), so the readers shipped by
trailing-sl-no-cooldown_07222026_2050 (cooldown reconstruction :2634, OOB
remap :2763) silently always read None.

Sections:
  A — persistence layer: `active_positions.trailing_activated` column
      (fresh DDL legacy + fleet, migration of pre-ticket DB files
      including the fleet init path that skipped the column migration),
      stamped by `update_position_sl(trailing_activated=True)` atomically
      with the trailed SL price. RED on current HEAD.
  B — `_recover_inherited_position` restores `_trailing_activated` from
      the ledger row; absent column / NULL / 0 -> False, never an
      invented True. RED on current HEAD.
  C — FENCE (pass on current HEAD by design): the entry-fill init block
      is unreachable for replayed/duplicate fill events, both mid-session
      (_processed_entry_order_ids dedup) and after a restart (the
      _entry_order_ids submission gate routes the replay to UNRECOGNIZED
      FILL). Pins the blueprint's Part-2-item-1 hypothesis closed so a
      refactor cannot open it.
  D — regression fence: a RESTORED latch drives the SL_HIT -> TRAILING_BE
      remap in _reset_position_state (test_trailing_sl_no_cooldown
      lineage). RED on current HEAD (restore missing).

LIVE-FLEET SAFETY: every I/O boundary is a MagicMock or a tmp_path DB.
No test touches the real telemetry DB, a broker, reports/fleet, or
.agents/collab/error_queue (no stub sets _health_events_enabled).
Conventions mirror tests/test_oob_entry_state_recovery.py: LiveTrader
stubs via object.__new__ with only the seams each method reads;
pandas 1.5.3 compatible.
"""

from __future__ import annotations

import logging
import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from src.live_execution import live_trader as lt_module
from src.live_execution.interfaces.execution_interface import (
    StandardExecutionEvent,
)
from src.live_execution.strategies.configurable_strategy import (
    ConfigurableStrategy,
)
from src.live_execution.telemetry import TelemetryDB


# ===========================================================================
# Shared builders
# ===========================================================================

_TRAILED_SL = 2.937


def _open_row(**over):
    """Ledger row shape returned by TelemetryDB.get_open_position for the
    NG incident trade (trade_108). sl_price is already the TRAILED value —
    update_position_sl persisted it at activation (live_trader.py:1709)."""
    row = {
        "trade_id": "trade_108", "side": "LONG", "entry_price": 2.905,
        "quantity": 1, "tp_order_id": 109, "sl_order_id": 110,
        "tp_price": 3.059, "sl_price": _TRAILED_SL, "atr_at_entry": 0.0193,
        "entry_bar_time": None, "trailing_atr_mult": None,
        "max_hold_bars": None, "trailing_activated": 1,
    }
    row.update(over)
    return row


def _restore_stub(ledger_pos):
    """LiveTrader stub with ONLY the seams _recover_inherited_position
    reads on the IBKR-confirms-position RESTORE branch (step 3 + leg
    verification; the OOB branch is pinned elsewhere)."""
    lt = object.__new__(lt_module.LiveTrader)
    lt._execution_symbol = "NG"
    lt.telemetry = MagicMock()
    lt.telemetry.get_open_position.return_value = ledger_pos
    lt.exec_client = MagicMock()
    lt.exec_client.get_position.return_value = 1  # broker confirms -> no OOB
    lt.exec_client.get_open_trades.return_value = [
        SimpleNamespace(order_id=109), SimpleNamespace(order_id=110),
    ]
    lt._open_orders = {}
    lt.rolling_df_5m = None
    lt.rolling_df_1h = None
    lt._bar_size = "1h"
    # __init__ defaults the recovery must overwrite (or provably keep)
    lt._trailing_activated = False
    lt._tracked_sl_price = None
    lt._tracked_tp_price = None
    lt._tp_order_ids = []
    lt._sl_order_id = None
    lt._highest_high = 0.0
    lt._lowest_low = float("inf")
    lt._position_bars_held = 0
    return lt


def _sl_only_strategy():
    """Minimal ConfigurableStrategy (test_trailing_sl_no_cooldown pattern):
    long side arms cooldown on original-SL exits only."""
    s = object.__new__(ConfigurableStrategy)
    s.config = {
        "long": {"cooldown_bars": 5, "cooldown_arming": "sl_only"},
        "short": {"cooldown_bars": 5, "cooldown_arming": "sl_only"},
    }
    s._last_exit_bars_ago_long = 9999
    s._last_exit_bars_ago_short = 9999
    s._exec_strategy = MagicMock()
    return s


def _fill_event(order_id, *, action="BUY", price=2.905, qty=1, symbol="NG"):
    raw = SimpleNamespace(
        order=SimpleNamespace(
            action=action, permId=int(order_id) * 10, parentId=0,
            account="DU-TEST",
        ),
        contract=SimpleNamespace(symbol=symbol, localSymbol="NGQ26"),
    )
    return StandardExecutionEvent(
        order_id=str(order_id), symbol=symbol, status="Filled",
        filled_qty=qty, remaining_qty=0, avg_price=price, raw_event=raw,
    )


def _exec_event_stub():
    """Seams _on_standard_execution_event reads BEFORE/AROUND the entry
    branch. Mid-trade state is preset by each test."""
    lt = object.__new__(lt_module.LiveTrader)
    lt._execution_symbol = "NG"
    lt._front_month_str = "202607"
    lt._open_orders = {}
    lt._last_decision_context_by_order_id = {}
    lt._processed_entry_order_ids = set()
    lt._processed_exit_order_ids = set()
    lt._entry_order_ids = set()
    lt._tp_order_ids = []
    lt._sl_order_id = None
    lt._recently_closed_legs = None
    lt._retiring_leg_ids = []
    lt._pending_entry_order_id = None
    lt._pending_entry_bar_time = None
    lt.telemetry = MagicMock()
    lt.exec_client = MagicMock()
    lt._telegram = MagicMock()
    lt.rolling_df_5m = None
    lt.rolling_df_1h = None
    return lt


def _mid_trade(lt):
    """Preset the exec-event stub with the restored NG mid-trade state."""
    lt._active_trade_id = "trade_108"
    lt._position_side = 1
    lt._position_bars_held = 15
    lt._trailing_activated = True
    lt._entry_price = 2.905
    lt._atr_at_entry = 0.0193
    lt._tp_order_ids = [109]
    lt._sl_order_id = 110
    lt._tracked_sl_price = _TRAILED_SL
    return lt


# Pre-ticket active_positions DDL (no trailing_activated column) for
# migration tests — byte-shape of the shipped schemas before this fix.
_OLD_LEGACY_ACTIVE_POSITIONS = """
CREATE TABLE active_positions (
    trade_id            TEXT    PRIMARY KEY,
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
    created_at          TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""

_OLD_FLEET_ACTIVE_POSITIONS = """
CREATE TABLE active_positions (
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
    created_at          TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(client_id, trade_id)
);
"""


def _columns(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        return {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(active_positions)"
            ).fetchall()
        }
    finally:
        conn.close()


def _open_trade(db, trade_id="trade_108"):
    db.open_position(
        trade_id=trade_id, side="LONG", quantity=1, entry_price=2.905,
        entry_order_id=108, atr_at_entry=0.0193,
        entry_time="2026-07-22T13:00:11",
        entry_bar_time="2026-07-22T12:55:00",
        trailing_atr_mult=None, max_hold_bars=None,
    )


# ===========================================================================
# A — persistence layer (TelemetryDB)
# ===========================================================================


class TestActivePositionsTrailingColumn:

    def test_fresh_legacy_schema_has_column_default_untrailed(self, tmp_path):
        db = TelemetryDB(tmp_path / "legacy.db")
        _open_trade(db)
        row = db.get_open_position()
        assert "trailing_activated" in row, (
            "active_positions must persist the trailing latch — the column "
            "cited from telemetry.py:170 belongs to decision_state_log, "
            "not the position row"
        )
        assert row["trailing_activated"] == 0, (
            "a fresh position must start untrailed (0), never NULL/True"
        )

    def test_fresh_fleet_schema_has_column_default_untrailed(self, tmp_path):
        db = TelemetryDB(tmp_path / "fleet.db", symbol="NG", client_id=3000)
        _open_trade(db)
        row = db.get_open_position()
        assert "trailing_activated" in row
        assert row["trailing_activated"] == 0

    def test_update_position_sl_stamps_latch_with_trailed_price(self, tmp_path):
        """The latch and the trailed SL price commit in the SAME ledger
        write — the exact call _check_trailing_stop already makes."""
        db = TelemetryDB(tmp_path / "legacy.db")
        _open_trade(db)
        db.update_position_sl(
            "trade_108", new_sl_price=_TRAILED_SL, sl_order_id=110,
            trailing_activated=True,
        )
        row = db.get_open_position()
        assert row["trailing_activated"] == 1
        assert row["sl_price"] == pytest.approx(_TRAILED_SL)

    def test_update_position_sl_without_flag_leaves_latch_untouched(
        self, tmp_path
    ):
        """A later SL update that does not mention the latch (heal paths)
        must not clear an armed latch."""
        db = TelemetryDB(tmp_path / "legacy.db")
        _open_trade(db)
        db.update_position_sl(
            "trade_108", new_sl_price=_TRAILED_SL, sl_order_id=110,
            trailing_activated=True,
        )
        db.update_position_sl("trade_108", new_sl_price=2.940)
        row = db.get_open_position()
        assert row["trailing_activated"] == 1, (
            "an update_position_sl call without trailing_activated must "
            "leave the persisted latch untouched"
        )
        assert row["sl_price"] == pytest.approx(2.940)

    def test_legacy_db_file_is_migrated(self, tmp_path):
        """Opening a pre-ticket legacy DB file adds the column; existing
        rows read as untrailed (0) — never an invented True."""
        p = tmp_path / "old_legacy.db"
        conn = sqlite3.connect(str(p))
        conn.executescript(_OLD_LEGACY_ACTIVE_POSITIONS)
        conn.execute(
            "INSERT INTO active_positions "
            "(trade_id, side, quantity, entry_price, entry_time) "
            "VALUES ('trade_7', 'SHORT', 1, 68.9, '2026-07-20T00:00:00')"
        )
        conn.commit()
        conn.close()

        db = TelemetryDB(p)
        assert "trailing_activated" in _columns(p)
        row = db.get_open_position()
        assert row["trade_id"] == "trade_7"
        assert not row["trailing_activated"], (
            "pre-migration rows must read as untrailed"
        )

    def test_fleet_db_file_is_migrated(self, tmp_path):
        """The LIVE deployment case: a user_version=2 fleet DB created
        before this ticket. The fleet init path must run the
        active_positions column migration (it skipped it pre-ticket)."""
        p = tmp_path / "old_fleet.db"
        conn = sqlite3.connect(str(p))
        conn.executescript(
            _OLD_FLEET_ACTIVE_POSITIONS
            + "CREATE TABLE market_bars (id INTEGER PRIMARY KEY, "
            "symbol TEXT, client_id INTEGER, timestamp TEXT);"
        )
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
        conn.close()

        db = TelemetryDB(p, symbol="NG", client_id=3000)
        assert "trailing_activated" in _columns(p), (
            "fleet-mode _initialize must migrate active_positions columns "
            "— the live shared DB predates the column"
        )
        _open_trade(db)
        db.update_position_sl(
            "trade_108", new_sl_price=_TRAILED_SL, trailing_activated=True,
        )
        assert db.get_open_position()["trailing_activated"] == 1


# ===========================================================================
# B — recovery restores the latch
# ===========================================================================


class TestRecoveryRestoresTrailingLatch:

    def test_restores_armed_latch_from_ledger_row(self):
        """The incident fix: trade_108's row says trailing_activated=1 —
        the restarted process must NOT re-fire the activation (19:15:06 /
        23:00:05 no-op modifies of order 110, fleet_20260722.log)."""
        lt = _restore_stub(_open_row(trailing_activated=1))
        lt._recover_inherited_position()
        assert lt._trailing_activated is True, (
            "recovery restored every field except the latch — the next 5M "
            "bar close re-fires TRAILING STOP: activated and a fill in the "
            "gap books SL_HIT instead of TRAILING_BE"
        )

    def test_restored_tracked_sl_is_the_trailed_price(self):
        """FENCE: _verify_and_heal_protective_legs already restores
        _tracked_sl_price from the row's sl_price (the trailed value)."""
        lt = _restore_stub(_open_row(trailing_activated=1))
        lt._recover_inherited_position()
        assert lt._tracked_sl_price == pytest.approx(_TRAILED_SL)
        assert lt._sl_order_id == 110
        assert lt._active_trade_id == "trade_108"

    def test_untrailed_row_leaves_latch_false(self):
        lt = _restore_stub(_open_row(trailing_activated=0))
        lt._recover_inherited_position()
        assert lt._trailing_activated is False

    def test_null_value_leaves_latch_false(self):
        """Migrated rows can carry NULL — never invent a True."""
        lt = _restore_stub(_open_row(trailing_activated=None))
        lt._recover_inherited_position()
        assert lt._trailing_activated is False

    def test_absent_column_leaves_latch_false(self):
        """Legacy row dicts without the key (pre-migration DB read via an
        old codepath) must not crash and must stay untrailed."""
        row = _open_row()
        del row["trailing_activated"]
        lt = _restore_stub(row)
        lt._recover_inherited_position()
        assert lt._trailing_activated is False


# ===========================================================================
# C — entry-fill idempotency fences (pass on current HEAD by design)
# ===========================================================================


class TestEntryFillIdempotencyFences:

    def test_duplicate_entry_fill_mid_session_does_not_reset_state(self):
        """FENCE: a replayed Filled event for an already-processed entry
        order id returns at the _processed_entry_order_ids dedup — the
        position-init block (latch/bars_held reset) is unreachable."""
        lt = _mid_trade(_exec_event_stub())
        lt._processed_entry_order_ids = {"108"}
        lt._entry_order_ids = {"108"}

        lt._on_standard_execution_event(_fill_event(108))

        assert lt._trailing_activated is True
        assert lt._position_bars_held == 15
        assert lt._active_trade_id == "trade_108"
        lt.telemetry.open_position.assert_not_called()
        lt.telemetry.close_position.assert_not_called()

    def test_replayed_entry_fill_after_restart_is_ignored(self, caplog):
        """FENCE: after a restart the fresh process never SUBMITTED order
        108, so the replayed fill fails the _entry_order_ids gate and is
        logged as UNRECOGNIZED FILL with position state unchanged — the
        blueprint's feared mid-trade re-init path does not exist."""
        lt = _mid_trade(_exec_event_stub())
        # fresh-process session sets: nothing submitted, nothing processed
        lt._processed_entry_order_ids = set()
        lt._entry_order_ids = set()

        with caplog.at_level(logging.ERROR):
            lt._on_standard_execution_event(_fill_event(108))

        assert lt._trailing_activated is True
        assert lt._position_bars_held == 15
        assert lt._active_trade_id == "trade_108"
        lt.telemetry.open_position.assert_not_called()
        lt.telemetry.close_position.assert_not_called()
        assert any(
            "UNRECOGNIZED FILL" in r.getMessage() for r in caplog.records
        ), "the replayed fill must be loudly ignored, not silently eaten"

    def test_genuine_new_entry_fill_still_initializes(self):
        """A REAL new entry (submitted this session) must still run the
        full init — the dedup must not suppress legitimate entries."""
        lt = _exec_event_stub()
        lt._entry_order_ids = {"120"}
        lt._active_trade_id = None
        lt._position_side = 0
        # stale garbage the init must overwrite
        lt._trailing_activated = True
        lt._position_bars_held = 7
        lt._last_decision_context_by_order_id = {
            120: {
                "atr_at_entry": 0.02, "trailing_atr_mult": None,
                "max_hold_bars": None, "entry_action": "BUY",
                "signal_id": "sig-1", "decision_id": "dec-1",
                "decision_timestamp_utc": "2026-07-23T00:00:00",
            },
        }
        idx = pd.DatetimeIndex([
            pd.Timestamp("2026-07-23 05:50:00"),
            pd.Timestamp("2026-07-23 05:55:00"),
        ])
        lt.rolling_df_5m = pd.DataFrame({
            "Open": [2.97, 2.98], "High": [2.98, 2.99],
            "Low": [2.96, 2.97], "Close": [2.98, 2.98],
            "Volume": [10.0, 12.0],
        }, index=idx)
        lt._place_bracket_children_on_fill = MagicMock()

        lt._on_standard_execution_event(_fill_event(120, price=2.98))

        assert lt._active_trade_id == "trade_120"
        assert lt._position_side == 1
        assert lt._trailing_activated is False, (
            "a genuinely new entry must re-arm the latch to False"
        )
        assert lt._position_bars_held == 0
        assert lt._entry_price == pytest.approx(2.98)
        assert "120" in lt._processed_entry_order_ids
        lt.telemetry.open_position.assert_called_once()
        lt._place_bracket_children_on_fill.assert_called_once()


# ===========================================================================
# D — restored latch drives the TRAILING_BE remap
# ===========================================================================


class TestRestoredLatchDrivesTrailingBERemap:

    def test_sl_fill_after_recovery_books_trailing_be_no_cooldown(self):
        """End-to-end regression fence: recover a trailed LONG from the
        ledger, then the trailed stop fills -> the strategy notification
        must be TRAILING_BE (not SL_HIT), and under sl_only the cooldown
        must NOT arm. Exactly the booking that goes wrong in the
        restart-to-first-bar gap today."""
        s = _sl_only_strategy()
        lt = _restore_stub(_open_row(trailing_activated=1))
        lt._recover_inherited_position()
        assert lt._trailing_activated is True  # precondition (Section B)

        lt.strategy = s
        lt._position_side = 1
        lt._position_bars_held = 16
        lt._position_entry_bar_time = None
        lt._entry_price = 2.905
        lt._atr_at_entry = 0.0193
        lt._trade_trailing_atr_mult = None
        lt._trade_max_hold_bars = None
        lt._partial_fill_signatures = set()

        lt._reset_position_state("SL_HIT")

        s._exec_strategy.on_exit.assert_called_once_with(1, "TRAILING_BE", 16)
        assert s._last_exit_bars_ago_long == 9999, (
            "a restored-latch trailed stop is a profit-lock exit — it must "
            "NOT arm the sl_only re-entry cooldown"
        )
        assert lt._trailing_activated is False  # reset for the next trade
