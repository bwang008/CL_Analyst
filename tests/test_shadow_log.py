"""
Tests for the shadow_log table and log_shadow_state() in TelemetryDB.

Verifies:
- Table creation
- Row insertion with full data
- NaN/inf sanitization
- Features JSON roundtrip
- Duplicate timestamp rejection (INSERT OR IGNORE)
"""

from __future__ import annotations

import json
import math
import tempfile
from pathlib import Path

import pytest

from src.live_execution.telemetry import TelemetryDB


@pytest.fixture
def db(tmp_path: Path) -> TelemetryDB:
    """Create a fresh TelemetryDB in a temp directory."""
    db_path = str(tmp_path / "test_telemetry.db")
    return TelemetryDB(db_path)


class TestShadowLogTable:
    """Verify the shadow_log table exists and behaves correctly."""

    def test_shadow_log_table_created(self, db: TelemetryDB) -> None:
        """shadow_log table should exist after initialization."""
        conn = db._get_conn()
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='shadow_log'"
        ).fetchall()
        assert len(tables) == 1, "shadow_log table not found"

    def test_shadow_log_index_created(self, db: TelemetryDB) -> None:
        """Index on timestamp should exist."""
        conn = db._get_conn()
        indexes = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_shadow_ts'"
        ).fetchall()
        assert len(indexes) == 1, "idx_shadow_ts index not found"


class TestLogShadowState:
    """Verify log_shadow_state() insertion and data integrity."""

    def test_inserts_row(self, db: TelemetryDB) -> None:
        """A single call should insert one row."""
        db.log_shadow_state(
            timestamp="2024-01-15T10:00:00",
            open_=75.50,
            high=76.20,
            low=75.10,
            close=75.80,
            volume=1500.0,
            features_dict={"ATR_14": 0.45, "Time_Sin": 0.123},
            prob_buy=0.72,
            prob_sell=None,
            strategy_name="TestStrategy",
        )
        assert db.shadow_log_count() == 1

    def test_row_values_correct(self, db: TelemetryDB) -> None:
        """Inserted values should be retrievable."""
        db.log_shadow_state(
            timestamp="2024-01-15T10:05:00",
            open_=75.50,
            high=76.20,
            low=75.10,
            close=75.80,
            volume=1500.0,
            features_dict={"ATR_14": 0.45},
            prob_buy=0.72,
            prob_sell=None,
            strategy_name="TestStrategy",
        )
        conn = db._get_conn()
        row = conn.execute(
            "SELECT * FROM shadow_log WHERE timestamp = ?",
            ("2024-01-15T10:05:00",),
        ).fetchone()
        assert row is not None
        # row indices: 0=id, 1=timestamp, 2=open, 3=high, 4=low, 5=close
        # 6=volume, 7=features_json, 8=prob_buy, 9=prob_sell, 10=strategy_name
        assert row[2] == 75.50  # open
        assert row[5] == 75.80  # close
        assert row[8] == pytest.approx(0.72)  # prob_buy
        assert row[9] is None  # prob_sell

    def test_handles_nan_in_features(self, db: TelemetryDB) -> None:
        """NaN values in features_dict should be converted to null in JSON."""
        db.log_shadow_state(
            timestamp="2024-01-15T10:10:00",
            open_=75.50,
            high=76.20,
            low=75.10,
            close=75.80,
            volume=1500.0,
            features_dict={"ATR_14": float("nan"), "valid": 1.5},
            prob_buy=0.65,
        )
        conn = db._get_conn()
        row = conn.execute(
            "SELECT features_json FROM shadow_log WHERE timestamp = ?",
            ("2024-01-15T10:10:00",),
        ).fetchone()
        features = json.loads(row[0])
        assert features["ATR_14"] is None, "NaN should become null"
        assert features["valid"] == 1.5

    def test_handles_inf_in_probability(self, db: TelemetryDB) -> None:
        """Infinite probability values should be converted to NULL."""
        db.log_shadow_state(
            timestamp="2024-01-15T10:15:00",
            open_=75.50,
            high=76.20,
            low=75.10,
            close=75.80,
            volume=1500.0,
            prob_buy=float("inf"),
            prob_sell=float("-inf"),
        )
        conn = db._get_conn()
        row = conn.execute(
            "SELECT prob_buy, prob_sell FROM shadow_log WHERE timestamp = ?",
            ("2024-01-15T10:15:00",),
        ).fetchone()
        assert row[0] is None, "inf should become NULL"
        assert row[1] is None, "-inf should become NULL"

    def test_features_json_roundtrip(self, db: TelemetryDB) -> None:
        """Features dict → JSON → dict should preserve all valid values."""
        original = {
            "ATR_14": 0.45678912345,
            "MACRO_WIDTH_3M": 12.345,
            "Time_Sin": -0.9876,
            "Volume_Log": 7.31,
        }
        db.log_shadow_state(
            timestamp="2024-01-15T10:20:00",
            open_=75.50,
            high=76.20,
            low=75.10,
            close=75.80,
            volume=1500.0,
            features_dict=original,
            prob_buy=0.55,
        )
        conn = db._get_conn()
        row = conn.execute(
            "SELECT features_json FROM shadow_log WHERE timestamp = ?",
            ("2024-01-15T10:20:00",),
        ).fetchone()
        recovered = json.loads(row[0])
        for key, val in original.items():
            assert recovered[key] == pytest.approx(val), f"Mismatch on {key}"

    def test_duplicate_timestamp_ignored(self, db: TelemetryDB) -> None:
        """Duplicate timestamps should be silently ignored (INSERT OR IGNORE)."""
        db.log_shadow_state(
            timestamp="2024-01-15T10:25:00",
            open_=75.50,
            high=76.20,
            low=75.10,
            close=75.80,
            volume=1500.0,
            prob_buy=0.60,
        )
        db.log_shadow_state(
            timestamp="2024-01-15T10:25:00",
            open_=99.99,  # different data, same timestamp
            high=100.0,
            low=99.0,
            close=99.50,
            volume=2000.0,
            prob_buy=0.90,
        )
        assert db.shadow_log_count() == 1
        # Original data should be preserved
        conn = db._get_conn()
        row = conn.execute(
            "SELECT open, prob_buy FROM shadow_log WHERE timestamp = ?",
            ("2024-01-15T10:25:00",),
        ).fetchone()
        assert row[0] == 75.50, "Original row should be preserved"
        assert row[1] == pytest.approx(0.60)

    def test_none_features_dict(self, db: TelemetryDB) -> None:
        """None features_dict should store NULL in features_json."""
        db.log_shadow_state(
            timestamp="2024-01-15T10:30:00",
            open_=75.50,
            high=76.20,
            low=75.10,
            close=75.80,
            volume=1500.0,
            features_dict=None,
            prob_buy=0.50,
        )
        conn = db._get_conn()
        row = conn.execute(
            "SELECT features_json FROM shadow_log WHERE timestamp = ?",
            ("2024-01-15T10:30:00",),
        ).fetchone()
        assert row[0] is None

    def test_multiple_rows(self, db: TelemetryDB) -> None:
        """Multiple rows with different timestamps should all be stored."""
        for i in range(10):
            db.log_shadow_state(
                timestamp=f"2024-01-15T{10 + i}:00:00",
                open_=75.0 + i,
                high=76.0 + i,
                low=74.0 + i,
                close=75.5 + i,
                volume=1000.0 + i * 100,
                prob_buy=0.50 + i * 0.02,
            )
        assert db.shadow_log_count() == 10


class TestSanitizeFloat:
    """Verify the _sanitize_float static method."""

    def test_none(self) -> None:
        assert TelemetryDB._sanitize_float(None) is None

    def test_normal_float(self) -> None:
        assert TelemetryDB._sanitize_float(3.14) == 3.14

    def test_nan(self) -> None:
        assert TelemetryDB._sanitize_float(float("nan")) is None

    def test_inf(self) -> None:
        assert TelemetryDB._sanitize_float(float("inf")) is None

    def test_neg_inf(self) -> None:
        assert TelemetryDB._sanitize_float(float("-inf")) is None

    def test_zero(self) -> None:
        assert TelemetryDB._sanitize_float(0.0) == 0.0

    def test_string_passthrough(self) -> None:
        assert TelemetryDB._sanitize_float("hello") == "hello"

    def test_int_coerced(self) -> None:
        result = TelemetryDB._sanitize_float(42)
        assert result == 42.0
