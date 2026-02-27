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

    # ------------------------------------------------------------------
    # Raw front-month bars (training ledger)
    # ------------------------------------------------------------------

    def test_log_raw_bar(self, tmp_db):
        """Logging a raw front-month bar should increment the raw bar count."""
        assert tmp_db.raw_bar_count() == 0
        tmp_db.log_raw_bar(
            timestamp="2026-02-23T10:00:00",
            open_=70.50, high=70.80, low=70.30,
            close=70.60, volume=1234.0,
            contract_month="202604",
        )
        assert tmp_db.raw_bar_count() == 1

    def test_log_raw_bar_with_contract_month(self, tmp_db):
        """Raw bar should record the contract_month field."""
        tmp_db.log_raw_bar(
            timestamp="2026-02-23T10:05:00",
            open_=71.00, high=71.20, low=70.80,
            close=71.10, volume=500.0,
            contract_month="202604",
        )
        bars = tmp_db.recent_raw_bars(1)
        assert len(bars) == 1
        assert bars[0]["contract_month"] == "202604"

    def test_recent_raw_bars_ordering(self, tmp_db):
        """recent_raw_bars should return entries most-recent-first."""
        for i in range(3):
            tmp_db.log_raw_bar(
                timestamp=f"2026-02-23T10:{i*5:02d}:00",
                open_=70.0 + i, high=71.0 + i, low=69.0 + i,
                close=70.5 + i, volume=1000.0 + i,
                contract_month="202604",
            )
        bars = tmp_db.recent_raw_bars(2)
        assert len(bars) == 2
        # Most recent first
        assert bars[0]["close"] == 72.5

    # ------------------------------------------------------------------
    # Tradebook events (execution lifecycle)
    # ------------------------------------------------------------------

    def test_log_tradebook_event_and_reader(self, tmp_db):
        """Tradebook should store normalized event rows and be queryable."""
        inserted = tmp_db.log_tradebook_event(
            event_id="evt-1",
            event_type="EXECUTION_FILL",
            event_timestamp_utc="2026-02-23T10:05:01.123000",
            decision_timestamp_utc="2026-02-23T10:05:00.000000",
            signal_id="sig-1",
            decision_id="dec-1",
            order_id=123,
            broker_execution_id="000abc",
            symbol="CL",
            local_symbol="CLH6",
            contract_month="202603",
            fill_qty=1.0,
            last_fill_price=70.52,
            slippage_estimate=0.02,
        )
        assert inserted is True
        rows = tmp_db.read_tradebook(order_id=123, limit=10)
        assert len(rows) == 1
        row = rows[0]
        assert row["signal_id"] == "sig-1"
        assert row["decision_id"] == "dec-1"
        assert row["contract_month"] == "202603"
        assert row["local_symbol"] == "CLH6"
        assert row["event_type"] == "EXECUTION_FILL"

    def test_tradebook_event_idempotency(self, tmp_db):
        """Duplicate event_id should be ignored (append-only idempotency)."""
        first = tmp_db.log_tradebook_event(
            event_id="evt-dup",
            event_type="ORDER_STATUS",
            event_timestamp_utc="2026-02-23T10:05:00.000000",
            status="Submitted",
            order_id=77,
        )
        second = tmp_db.log_tradebook_event(
            event_id="evt-dup",
            event_type="ORDER_STATUS",
            event_timestamp_utc="2026-02-23T10:05:00.000000",
            status="Submitted",
            order_id=77,
        )
        assert first is True
        assert second is False
        rows = tmp_db.read_tradebook(order_id=77, limit=10)
        assert len(rows) == 1

    def test_commission_async_rows(self, tmp_db):
        """Commission can be logged after fill as a separate event."""
        tmp_db.log_tradebook_event(
            event_id="evt-fill",
            event_type="EXECUTION_FILL",
            event_timestamp_utc="2026-02-23T10:05:01.100000",
            order_id=88,
            broker_execution_id="exec-88",
            fill_qty=0.5,
            last_fill_price=70.40,
        )
        tmp_db.log_tradebook_event(
            event_id="evt-comm",
            event_type="COMMISSION",
            event_timestamp_utc="2026-02-23T10:05:01.900000",
            order_id=88,
            broker_execution_id="exec-88",
            commission=2.34,
            fees=2.34,
        )
        rows = tmp_db.read_tradebook(order_id=88, limit=10)
        assert len(rows) == 2
        assert rows[0]["event_type"] == "EXECUTION_FILL"
        assert rows[1]["event_type"] == "COMMISSION"

