"""
Tests for the telemetry SQLite backend.
"""

import os
import tempfile

import pytest

from src.live_execution.telemetry import TelemetryDB


@pytest.fixture
def tmp_db(tmp_path):
    """Create a TelemetryDB in a temp directory."""
    db_path = str(tmp_path / "test_telemetry.db")
    db = TelemetryDB(db_path)
    yield db
    db.close()


class TestTelemetryDB:
    """Unit tests for TelemetryDB."""

    def test_creates_db_file(self, tmp_path):
        """DB file should be created on instantiation."""
        db_path = str(tmp_path / "test.db")
        db = TelemetryDB(db_path)
        assert os.path.exists(db_path)
        db.close()

    def test_creates_tables_idempotent(self, tmp_path):
        """Re-creating the DB on the same path should not error."""
        db_path = str(tmp_path / "test.db")
        db1 = TelemetryDB(db_path)
        db1.close()
        db2 = TelemetryDB(db_path)
        assert db2.bar_count() == 0
        db2.close()

    def test_log_bar(self, tmp_db):
        """Logging a bar should increment the bar count."""
        assert tmp_db.bar_count() == 0
        tmp_db.log_bar(
            timestamp="2026-02-23T10:00:00",
            open_=70.50, high=70.80, low=70.30, close=70.60, volume=1234.0,
        )
        assert tmp_db.bar_count() == 1

    def test_log_multiple_bars(self, tmp_db):
        """Multiple bars should all be recorded."""
        for i in range(5):
            tmp_db.log_bar(
                timestamp=f"2026-02-23T10:{i*5:02d}:00",
                open_=70.0 + i, high=71.0 + i, low=69.0 + i,
                close=70.5 + i, volume=1000.0 + i,
            )
        assert tmp_db.bar_count() == 5

    def test_log_signal_hold(self, tmp_db):
        """A hold signal should be recorded."""
        tmp_db.log_signal(
            timestamp="2026-02-23T10:05:00",
            signal="Hold",
            confidence_pct=25.0,
            action_taken="HOLD",
            current_price=70.50,
            atr_value=0.85,
        )
        assert tmp_db.signal_count() == 1
        assert tmp_db.trade_count() == 0

    def test_log_signal_execute(self, tmp_db):
        """An executed buy signal should appear in both signal and trade counts."""
        tmp_db.log_signal(
            timestamp="2026-02-23T10:05:00",
            signal="Buy",
            confidence_pct=62.5,
            action_taken="EXECUTE",
            current_price=70.50,
            atr_value=0.85,
            tp_price=72.20,
            sl_price=69.65,
            order_id=12345,
            direction="BUY",
        )
        assert tmp_db.signal_count() == 1
        assert tmp_db.trade_count() == 1

    def test_update_fill(self, tmp_db):
        """Fill price should be updatable after order execution."""
        tmp_db.log_signal(
            timestamp="2026-02-23T10:05:00",
            signal="Buy",
            confidence_pct=62.5,
            action_taken="EXECUTE",
            order_id=99,
            direction="BUY",
        )
        tmp_db.update_fill(order_id=99, fill_price=70.52)
        signals = tmp_db.recent_signals(1)
        assert len(signals) == 1
        assert signals[0]["fill_price"] == 70.52

    def test_recent_signals(self, tmp_db):
        """recent_signals should return entries in reverse-chronological order."""
        for i in range(3):
            tmp_db.log_signal(
                timestamp=f"2026-02-23T10:{i*5:02d}:00",
                signal="Hold",
                confidence_pct=float(i * 10),
                action_taken="HOLD",
            )
        signals = tmp_db.recent_signals(2)
        assert len(signals) == 2
        # Most recent first
        assert signals[0]["confidence_pct"] == 20.0

    def test_recent_bars(self, tmp_db):
        """recent_bars should return entries in reverse-chronological order."""
        for i in range(3):
            tmp_db.log_bar(
                timestamp=f"2026-02-23T10:{i*5:02d}:00",
                open_=70.0 + i, high=71.0 + i, low=69.0 + i,
                close=70.5 + i, volume=1000.0 + i,
            )
        bars = tmp_db.recent_bars(2)
        assert len(bars) == 2
        # Most recent first
        assert bars[0]["close"] == 72.5
