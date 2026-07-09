"""
Test Execution Parity — LiveTrader recovery and schema integrity.

Validates:
  1. Recovery bars_held is COUNTED from received brain bars (gap-immune
     _bars_since), not derived from wall-clock division (ticket
     recovery-barsheld-wallclock_07092026_1239 — the old wall-clock math
     counted weekend/halt gaps as phantom bars)
  2. initial_sl_price column exists and is set on bracket placement
  3. initial_sl_price is NOT overwritten by trailing stop modifications
  4. export_trade_ledger correctly reads initial_sl_price with fallback
"""

import os
import sqlite3
import sys
import tempfile

import pandas as pd
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


# ---------------------------------------------------------------------------
# Tests: Recovery bars_held counting (real LiveTrader._bars_since)
# ---------------------------------------------------------------------------


def _bars_since_trader(bar_size: str, df_1h=None, df_5m=None):
    from src.live_execution.live_trader import LiveTrader

    lt = object.__new__(LiveTrader)
    lt._bar_size = bar_size
    lt.rolling_df_1h = df_1h
    lt.rolling_df_5m = df_5m
    return lt


def _frame(idx) -> pd.DataFrame:
    return pd.DataFrame({"Close": [1.0] * len(idx)}, index=idx)


class TestRecoveryBarsHeld:
    """Recovery bars_held counts actual bars via LiveTrader._bars_since.

    Replaces the retired wall-clock replica (_estimate_bars_held): that
    helper self-replicated the delta_minutes/bar_dur math this ticket
    removed, so it pinned the bug instead of the behavior.
    """

    def test_1h_contiguous_25h_is_25_bars(self):
        idx = pd.date_range("2026-07-06 00:00", periods=26, freq="h")
        lt = _bars_since_trader("1h", df_1h=_frame(idx))
        assert lt._bars_since(idx[0]) == 25
        assert lt._bars_since(idx[0]) < 240  # under max_hold_bars=240

    def test_1h_weekend_gap_counts_no_phantom_bars(self):
        """Friday entry recovered after the weekend: only bars actually
        received count — the ~49h gap contributes ZERO bars, so a 24-bar
        max hold is NOT spuriously exceeded at Sunday open."""
        fri = pd.date_range("2026-07-03 10:00", "2026-07-03 16:00", freq="h")
        sun = pd.date_range("2026-07-05 18:00", "2026-07-05 20:00", freq="h")
        lt = _bars_since_trader("1h", df_1h=_frame(fri.append(sun)))
        entry = pd.Timestamp("2026-07-03 14:00:00")
        bars = lt._bars_since(entry)
        assert bars == 5  # Fri 15,16 + Sun 18,19,20
        assert bars <= 24  # wall-clock math said ~54 → spurious TIME_BARRIER

    def test_5m_uses_5m_frame(self):
        idx = pd.date_range("2026-07-06 10:00", periods=301, freq="5min")
        lt = _bars_since_trader("5m", df_5m=_frame(idx))
        assert lt._bars_since(idx[0]) == 300

    def test_resampled_bar_sizes_refuse_to_count(self):
        """2h/4h brains are resampled from 1h rows — raw counting would
        over-count 2-4x, so the helper must return None (reviewer C1)."""
        idx = pd.date_range("2026-07-06 00:00", periods=10, freq="h")
        for size in ("2h", "4h", "unknown"):
            lt = _bars_since_trader(size, df_1h=_frame(idx))
            assert lt._bars_since(idx[0]) is None, size

    def test_entry_at_latest_bar_is_zero(self):
        idx = pd.date_range("2026-07-06 00:00", periods=5, freq="h")
        lt = _bars_since_trader("1h", df_1h=_frame(idx))
        assert lt._bars_since(idx[-1]) == 0

    def test_future_entry_clamps_to_zero(self):
        """Clock skew (entry after last bar) yields 0, never negative."""
        idx = pd.date_range("2026-07-06 00:00", periods=5, freq="h")
        lt = _bars_since_trader("1h", df_1h=_frame(idx))
        assert lt._bars_since(idx[-1] + pd.Timedelta(hours=3)) == 0


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
