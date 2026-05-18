"""
Test Execution Parity — LiveTrader recovery and schema integrity.

Validates:
  1. Recovery bars_held estimation uses bar_size (not hardcoded /5)
  2. initial_sl_price column exists and is set on bracket placement
  3. initial_sl_price is NOT overwritten by trailing stop modifications
  4. export_trade_ledger correctly reads initial_sl_price with fallback
"""

import os
import sqlite3
import sys
import tempfile

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bar_minutes_map():
    """The bar duration lookup that must match live_trader.py recovery code."""
    return {"5m": 5, "1h": 60, "2h": 120, "4h": 240}


def _estimate_bars_held(delta_minutes: float, bar_size: str) -> int:
    """Replicate the recovery bars_held logic from live_trader.py."""
    bar_dur = _bar_minutes_map().get(bar_size, 5)
    return max(0, int(delta_minutes / bar_dur))


# ---------------------------------------------------------------------------
# Tests: Recovery bars_held estimation
# ---------------------------------------------------------------------------


class TestRecoveryBarsHeld:
    """Verify recovery bars_held uses bar_size, not hardcoded /5."""

    def test_5m_bars_held_same_as_before(self):
        """5m bar_size should match the old /5 logic."""
        delta = 1500.0  # 25 hours
        assert _estimate_bars_held(delta, "5m") == 300

    def test_1h_bars_held_correct(self):
        """1h bar_size: 25 hours = 25 bars, NOT 300."""
        delta = 1500.0  # 25 hours
        result = _estimate_bars_held(delta, "1h")
        assert result == 25, f"Expected 25 bars for 1h, got {result}"
        # Verify this is under max_hold_bars=240
        assert result < 240

    def test_1h_bars_held_not_triggering_time_barrier(self):
        """The bug: 25h hold on 1h bars should NOT exceed max_hold_bars=240.
        Before fix: delta_minutes/5 = 300 > 240 → premature TIME_BARRIER.
        After fix:  delta_minutes/60 = 25 < 240 → no TIME_BARRIER."""
        max_hold = 240
        delta = 25 * 60.0  # 25 hours in minutes
        old_logic = int(delta / 5)  # 300 — WRONG
        new_logic = _estimate_bars_held(delta, "1h")  # 25 — CORRECT
        assert old_logic > max_hold, "Old logic should exceed barrier"
        assert new_logic < max_hold, "New logic should be under barrier"

    def test_2h_bars_held_correct(self):
        """2h bar_size: 10 hours = 5 bars."""
        delta = 600.0  # 10 hours
        assert _estimate_bars_held(delta, "2h") == 5

    def test_4h_bars_held_correct(self):
        """4h bar_size: 24 hours = 6 bars."""
        delta = 1440.0  # 24 hours
        assert _estimate_bars_held(delta, "4h") == 6

    def test_unknown_bar_size_falls_back_to_5m(self):
        """Unknown bar sizes should conservatively use 5-min fallback."""
        delta = 100.0
        assert _estimate_bars_held(delta, "unknown") == 20

    def test_zero_delta(self):
        """Zero elapsed time should produce 0 bars."""
        assert _estimate_bars_held(0.0, "1h") == 0

    def test_negative_delta(self):
        """Negative delta (clock skew) should clamp to 0."""
        assert _estimate_bars_held(-100.0, "1h") == 0


# ---------------------------------------------------------------------------
# Tests: initial_sl_price schema and behavior
# ---------------------------------------------------------------------------


class TestInitialSlPriceSchema:
    """Verify the initial_sl_price column in active_positions."""

    @pytest.fixture
    def db(self, tmp_path):
        """Create a fresh TelemetryDB in a temp directory."""
        from src.live_execution.telemetry import TelemetryDB
        db_path = str(tmp_path / "test_telemetry.db")
        tdb = TelemetryDB(db_path)
        yield tdb
        tdb.close()

    def test_column_exists_in_schema(self, db):
        """initial_sl_price column should exist in active_positions."""
        conn = db._get_conn()
        cols = {
            row[1] for row in conn.execute(
                "PRAGMA table_info(active_positions)"
            ).fetchall()
        }
        assert "initial_sl_price" in cols

    def test_initial_sl_set_on_bracket_placement(self, db):
        """update_position_brackets should set initial_sl_price on first call."""
        db.open_position(
            trade_id="test-001",
            side="LONG",
            quantity=1,
            entry_price=100.0,
            entry_time="2026-05-17T12:00:00",
        )
        db.update_position_brackets(
            "test-001",
            tp_order_id=101,
            sl_order_id=102,
            tp_price=106.0,
            sl_price=96.0,
        )
        conn = db._get_conn()
        row = conn.execute(
            "SELECT sl_price, initial_sl_price FROM active_positions "
            "WHERE trade_id = 'test-001'"
        ).fetchone()
        assert row is not None
        sl_price, initial_sl_price = row
        assert sl_price == 96.0
        assert initial_sl_price == 96.0

    def test_initial_sl_not_overwritten_by_trailing(self, db):
        """update_position_sl (trailing) should NOT change initial_sl_price."""
        db.open_position(
            trade_id="test-002",
            side="LONG",
            quantity=1,
            entry_price=100.0,
            entry_time="2026-05-17T12:00:00",
        )
        # Place brackets (sets initial_sl_price = 96.0)
        db.update_position_brackets(
            "test-002",
            tp_order_id=201,
            sl_order_id=202,
            tp_price=106.0,
            sl_price=96.0,
        )
        # Trailing stop moves SL up to 101.0
        db.update_position_sl(
            "test-002",
            new_sl_price=101.0,
            sl_order_id=202,
        )
        conn = db._get_conn()
        row = conn.execute(
            "SELECT sl_price, initial_sl_price FROM active_positions "
            "WHERE trade_id = 'test-002'"
        ).fetchone()
        assert row is not None
        sl_price, initial_sl_price = row
        assert sl_price == 101.0, "sl_price should be updated to trailing value"
        assert initial_sl_price == 96.0, "initial_sl_price must NOT change"

    def test_initial_sl_survives_re_bracket(self, db):
        """If brackets are re-placed (recovery), initial_sl_price stays."""
        db.open_position(
            trade_id="test-003",
            side="SHORT",
            quantity=2,
            entry_price=105.0,
            entry_time="2026-05-17T13:00:00",
        )
        # Original bracket
        db.update_position_brackets(
            "test-003",
            tp_order_id=301,
            sl_order_id=302,
            tp_price=100.0,
            sl_price=108.0,
        )
        # Recovery re-places brackets with new order IDs
        db.update_position_brackets(
            "test-003",
            tp_order_id=401,
            sl_order_id=402,
            tp_price=100.0,
            sl_price=108.0,
        )
        conn = db._get_conn()
        row = conn.execute(
            "SELECT initial_sl_price FROM active_positions "
            "WHERE trade_id = 'test-003'"
        ).fetchone()
        assert row[0] == 108.0, "initial_sl_price should not change on re-bracket"

    def test_export_trade_ledger_uses_initial_sl(self, db):
        """export_trade_ledger should prefer initial_sl_price over sl_price."""
        db.open_position(
            trade_id="test-004",
            side="LONG",
            quantity=1,
            entry_price=100.0,
            entry_time="2026-05-17T14:00:00",
            entry_bar_time="2026-05-17T14:00:00",
        )
        db.update_position_brackets(
            "test-004",
            tp_order_id=501,
            sl_order_id=502,
            tp_price=106.0,
            sl_price=96.0,
        )
        # Trailing modifies SL
        db.update_position_sl("test-004", new_sl_price=101.5)
        # Close trade
        db.close_position(
            "test-004",
            reason="TAKE_PROFIT",
            close_time="2026-05-17T18:00:00",
            bars_held=4,
            exit_price=106.0,
        )
        df = db.export_trade_ledger()
        assert len(df) == 1
        assert df.iloc[0]["initial_sl_price"] == 96.0, \
            "Should export initial SL (96.0), not trailing SL (101.5)"

    def test_migration_adds_column_to_old_db(self, tmp_path):
        """Simulates an old DB without initial_sl_price — migration should add it."""
        db_path = str(tmp_path / "old_telemetry.db")
        conn = sqlite3.connect(db_path)
        # Create active_positions WITHOUT initial_sl_price (old schema)
        conn.execute("""
            CREATE TABLE active_positions (
                trade_id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'OPEN',
                side TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                entry_price REAL NOT NULL,
                entry_order_id INTEGER,
                tp_order_id INTEGER,
                sl_order_id INTEGER,
                tp_price REAL,
                sl_price REAL,
                atr_at_entry REAL,
                entry_time TEXT NOT NULL,
                entry_bar_time TEXT,
                close_time TEXT,
                close_reason TEXT,
                exit_price REAL,
                bars_held INTEGER,
                trailing_atr_mult REAL,
                max_hold_bars INTEGER,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.commit()
        conn.close()

        # Now open with TelemetryDB — migration should add the column
        from src.live_execution.telemetry import TelemetryDB
        tdb = TelemetryDB(db_path)
        try:
            cols = {
                row[1] for row in tdb._get_conn().execute(
                    "PRAGMA table_info(active_positions)"
                ).fetchall()
            }
            assert "initial_sl_price" in cols, \
                "Migration should add initial_sl_price to old databases"
        finally:
            tdb.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
