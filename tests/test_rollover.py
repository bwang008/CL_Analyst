"""Tests for DataManager rollover detection and master training ledger."""
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from src.live_execution.data_manager import DataManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_dir(tmp_path):
    """Provide temp paths for cache, seed, ledger, and metadata."""
    return tmp_path


@pytest.fixture
def seed_csv(tmp_dir):
    """Create a minimal seed CSV in the expected format."""
    seed_path = tmp_dir / "seed.csv"
    rows = []
    base = pd.Timestamp("2024-01-01 09:00")
    for i in range(200):
        dt = base + pd.Timedelta(minutes=5 * i)
        rows.append(
            f"{dt.strftime('%d/%m/%Y')};{dt.strftime('%H:%M')};"
            f"{60.0 + i * 0.01:.3f};{60.5 + i * 0.01:.3f};"
            f"{59.5 + i * 0.01:.3f};{60.2 + i * 0.01:.3f};100"
        )
    seed_path.write_text("\n".join(rows), encoding="utf-8")
    return str(seed_path)


@pytest.fixture
def dm(tmp_dir, seed_csv):
    """Create a DataManager with temp paths and no IBKR."""
    return DataManager(
        seed_path=seed_csv,
        cache_path=str(tmp_dir / "cache.parquet"),
        master_ledger_path=str(tmp_dir / "master.parquet"),
        ibkr_manager=None,
        front_month_id="CLJ6",
    )


# ---------------------------------------------------------------------------
# Rollover metadata I/O
# ---------------------------------------------------------------------------

class TestRollMetadata:
    def test_save_and_load(self, dm, tmp_dir):
        """Saving metadata creates file; loading returns same data."""
        meta_file = tmp_dir / ".roll_metadata.json"
        with patch(
            "src.live_execution.data_manager._ROLL_METADATA_PATH",
            str(meta_file),
        ):
            dm._save_roll_metadata()
            assert meta_file.exists()

            meta = dm._load_roll_metadata()
            assert meta["last_front_month"] == "CLJ6"
            assert "updated_at" in meta

    def test_load_missing_file(self, dm, tmp_dir):
        """Loading when no metadata file exists returns empty dict."""
        with patch(
            "src.live_execution.data_manager._ROLL_METADATA_PATH",
            str(tmp_dir / "nonexistent.json"),
        ):
            meta = dm._load_roll_metadata()
            assert meta == {}

    def test_load_corrupt_file(self, dm, tmp_dir):
        """Loading a corrupt JSON file returns empty dict gracefully."""
        bad_file = tmp_dir / "corrupt.json"
        bad_file.write_text("NOT JSON", encoding="utf-8")
        with patch(
            "src.live_execution.data_manager._ROLL_METADATA_PATH",
            str(bad_file),
        ):
            meta = dm._load_roll_metadata()
            assert meta == {}


# ---------------------------------------------------------------------------
# Rollover detection
# ---------------------------------------------------------------------------

class TestRolloverDetection:
    def test_first_run_no_rollover(self, dm, tmp_dir):
        """First run (no metadata file) should not be detected as rollover."""
        with patch(
            "src.live_execution.data_manager._ROLL_METADATA_PATH",
            str(tmp_dir / "nonexistent.json"),
        ):
            assert dm._detect_rollover() is False

    def test_same_contract_no_rollover(self, dm, tmp_dir):
        """Same front-month as last run = no rollover."""
        meta_file = tmp_dir / "meta.json"
        meta_file.write_text(
            json.dumps({"last_front_month": "CLJ6"}),
            encoding="utf-8",
        )
        with patch(
            "src.live_execution.data_manager._ROLL_METADATA_PATH",
            str(meta_file),
        ):
            assert dm._detect_rollover() is False

    def test_different_contract_triggers_rollover(self, dm, tmp_dir):
        """Different front-month since last run = rollover detected."""
        meta_file = tmp_dir / "meta.json"
        meta_file.write_text(
            json.dumps({"last_front_month": "CLH6"}),
            encoding="utf-8",
        )
        with patch(
            "src.live_execution.data_manager._ROLL_METADATA_PATH",
            str(meta_file),
        ):
            assert dm._detect_rollover() is True


# ---------------------------------------------------------------------------
# Roll delta computation (Panama Canal method)
# ---------------------------------------------------------------------------

class TestComputeRollDelta:
    def test_empty_cache_returns_none(self, dm):
        """Computing delta with empty cache returns None."""
        dm._df = None
        assert dm._compute_roll_delta() is None

    def test_correct_delta_computed(self, dm, seed_csv, monkeypatch):
        """Roll delta is the median difference between IBKR and cache Close."""
        dm._df = dm._seed_from_csv()
        overlap_idx = dm._df.index[-20:]

        # IBKR data shifted by exactly $1.50
        mock_ibkr_df = dm._df.loc[overlap_idx].copy()
        mock_ibkr_df["Open"] += 1.50
        mock_ibkr_df["High"] += 1.50
        mock_ibkr_df["Low"] += 1.50
        mock_ibkr_df["Close"] += 1.50

        mock_manager = MagicMock()
        mock_manager.qualify_contract.return_value = MagicMock()
        mock_manager._request_historical_data.return_value = ["bar"]
        dm.ibkr_manager = mock_manager

        import src.live_execution.ibkr_client as ibkr_mod
        monkeypatch.setattr(ibkr_mod, "build_cl_contract", lambda **kw: MagicMock())
        monkeypatch.setattr(ibkr_mod, "ib_bars_to_dataframe", lambda bars: mock_ibkr_df)

        delta = dm._compute_roll_delta()
        assert delta is not None
        assert abs(delta - 1.50) < 0.01, f"Expected delta ~1.50, got {delta}"

    def test_no_ibkr_data_returns_none(self, dm, seed_csv, monkeypatch):
        """If IBKR returns no bars, delta is None."""
        dm._df = dm._seed_from_csv()

        mock_manager = MagicMock()
        mock_manager.qualify_contract.return_value = MagicMock()
        mock_manager._request_historical_data.return_value = []
        dm.ibkr_manager = mock_manager

        import src.live_execution.ibkr_client as ibkr_mod
        monkeypatch.setattr(ibkr_mod, "build_cl_contract", lambda **kw: MagicMock())

        assert dm._compute_roll_delta() is None

    def test_no_overlap_returns_none(self, dm, seed_csv, monkeypatch):
        """No overlapping timestamps = None (cannot compute delta)."""
        dm._df = dm._seed_from_csv()

        future_base = pd.Timestamp("2025-06-01 09:00")
        mock_ibkr_df = pd.DataFrame(
            {
                "DateTime": [future_base + pd.Timedelta(minutes=5 * i) for i in range(20)],
                "Open": np.random.uniform(60, 65, 20),
                "High": np.random.uniform(65, 70, 20),
                "Low": np.random.uniform(55, 60, 20),
                "Close": np.random.uniform(60, 65, 20),
                "Volume": [100.0] * 20,
            }
        )
        mock_ibkr_df = mock_ibkr_df.set_index(
            pd.DatetimeIndex(mock_ibkr_df["DateTime"]), drop=False
        )
        mock_ibkr_df.index.name = "DateTime"

        mock_manager = MagicMock()
        mock_manager.qualify_contract.return_value = MagicMock()
        mock_manager._request_historical_data.return_value = ["bar"]
        dm.ibkr_manager = mock_manager

        import src.live_execution.ibkr_client as ibkr_mod
        monkeypatch.setattr(ibkr_mod, "build_cl_contract", lambda **kw: MagicMock())
        monkeypatch.setattr(ibkr_mod, "ib_bars_to_dataframe", lambda bars: mock_ibkr_df)

        assert dm._compute_roll_delta() is None


# ---------------------------------------------------------------------------
# Back-adjustment (Panama Canal shift)
# ---------------------------------------------------------------------------

class TestBackAdjustCache:
    def test_ohlc_shifted_by_delta(self, dm, seed_csv):
        """OHLC columns should be shifted by delta after back-adjustment."""
        dm._df = dm._seed_from_csv()
        original_close = dm._df["Close"].copy()
        original_open = dm._df["Open"].copy()
        delta = 2.50

        dm._back_adjust_cache(delta)

        # All OHLC shifted
        assert abs(dm._df["Close"].iloc[0] - (original_close.iloc[0] + delta)) < 0.001
        assert abs(dm._df["Open"].iloc[0] - (original_open.iloc[0] + delta)) < 0.001

    def test_volume_untouched(self, dm, seed_csv):
        """Volume column should NOT be modified by back-adjustment."""
        dm._df = dm._seed_from_csv()
        original_volume = dm._df["Volume"].copy()

        dm._back_adjust_cache(1.50)

        pd.testing.assert_series_equal(
            dm._df["Volume"], original_volume, check_names=False,
        )

    def test_bar_count_preserved(self, dm, seed_csv):
        """Back-adjustment should not add or remove bars."""
        dm._df = dm._seed_from_csv()
        n_before = len(dm._df)

        dm._back_adjust_cache(1.50)

        assert len(dm._df) == n_before

    def test_zero_delta_noop(self, dm, seed_csv):
        """Delta of 0 should leave data unchanged."""
        dm._df = dm._seed_from_csv()
        original_close = dm._df["Close"].copy()

        dm._back_adjust_cache(0.0)

        pd.testing.assert_series_equal(
            dm._df["Close"], original_close, check_names=False,
        )

    def test_ibkr_overlap_overwrites_shifted_bars(self, dm, seed_csv):
        """When IBKR overlap data exists, those bars use IBKR values exactly."""
        dm._df = dm._seed_from_csv()
        overlap_idx = dm._df.index[-10:]

        # Set up IBKR overlap with specific prices
        ibkr_df = dm._df.loc[overlap_idx].copy()
        ibkr_df["Close"] = 99.99
        ibkr_df["Open"] = 99.00

        dm._ibkr_overlap_df = ibkr_df

        dm._back_adjust_cache(1.50)

        # The overlap bars should have IBKR values, not shifted values
        assert abs(dm._df.loc[overlap_idx[-1], "Close"] - 99.99) < 0.001
        assert abs(dm._df.loc[overlap_idx[-1], "Open"] - 99.00) < 0.001

    def test_stores_roll_delta(self, dm, seed_csv):
        """Back-adjustment should store the delta for metadata/ledger use."""
        dm._df = dm._seed_from_csv()

        dm._back_adjust_cache(2.75)

        assert dm._roll_delta == 2.75


# ---------------------------------------------------------------------------
# Roll metadata history
# ---------------------------------------------------------------------------

class TestRollMetadataHistory:
    def test_roll_history_accumulated(self, dm, tmp_dir):
        """Roll history entries accumulate across multiple rolls."""
        meta_file = tmp_dir / ".roll_metadata.json"
        with patch(
            "src.live_execution.data_manager._ROLL_METADATA_PATH",
            str(meta_file),
        ):
            # First roll
            dm.front_month_id = "CLK6"
            dm._roll_detected = True
            dm._roll_delta = 1.50
            dm._save_roll_metadata()

            meta = dm._load_roll_metadata()
            assert len(meta["roll_history"]) == 1
            assert meta["roll_history"][0]["delta"] == 1.50
            assert meta["cumulative_delta"] == 1.50

            # Second roll
            dm.front_month_id = "CLM6"
            dm._roll_delta = 0.80
            dm._save_roll_metadata()

            meta = dm._load_roll_metadata()
            assert len(meta["roll_history"]) == 2
            assert abs(meta["cumulative_delta"] - 2.30) < 0.01

    def test_no_history_without_roll(self, dm, tmp_dir):
        """No roll history entry added when no roll was detected."""
        meta_file = tmp_dir / ".roll_metadata.json"
        with patch(
            "src.live_execution.data_manager._ROLL_METADATA_PATH",
            str(meta_file),
        ):
            dm._roll_detected = False
            dm._roll_delta = 0.0
            dm._save_roll_metadata()

            meta = dm._load_roll_metadata()
            assert meta.get("roll_history", []) == []
            assert meta.get("cumulative_delta", 0.0) == 0.0


# ---------------------------------------------------------------------------
# Full rebuild fallback
# ---------------------------------------------------------------------------

class TestFullRebuildFallback:
    def test_full_rebuild_deletes_and_reseeds(self, dm, seed_csv):
        """Full rebuild (fallback) removes stale cache and re-seeds from CSV."""
        dm._df = dm._seed_from_csv()
        dm.save_cache()
        assert dm.cache_path.exists()

        original_len = len(dm._df)
        # Corrupt cache by adding fake data
        dm._df = pd.concat([dm._df, dm._df])
        assert len(dm._df) != original_len

        dm._full_rebuild_cache()
        assert len(dm._df) == original_len


# ---------------------------------------------------------------------------
# Master training ledger
# ---------------------------------------------------------------------------

class TestTrainingLedger:
    def test_load_full_seed(self, dm, seed_csv):
        """_load_full_seed loads entire dataset (no lookback limit)."""
        full = dm._load_full_seed()
        assert len(full) == 200
        assert "Open" in full.columns
        assert "Close" in full.columns

    def test_save_and_load_ledger(self, dm, seed_csv):
        """Ledger round-trips to Parquet correctly."""
        df = dm._load_full_seed()
        dm._save_ledger(df)
        assert dm.master_ledger_path.exists()

        loaded = pd.read_parquet(dm.master_ledger_path)
        assert len(loaded) == 200
        assert "Close" in loaded.columns

    def test_get_seed_end_timestamp(self, dm, seed_csv):
        """Seed end timestamp matches the last row of the seed CSV."""
        full = dm._load_full_seed()
        expected_end = full.index.max()
        actual_end = dm._get_seed_end_timestamp(full)
        assert actual_end == expected_end
