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
# Cache validation (direct unit tests via monkeypatch)
# ---------------------------------------------------------------------------

class TestCacheValidation:
    def test_empty_cache_returns_false(self, dm):
        """Validating an empty cache returns False (stale)."""
        dm._df = None
        assert dm._validate_cache_after_roll() is False

    def test_matching_prices_returns_true(self, dm, seed_csv, monkeypatch):
        """Cache is valid when prices match IBKR data."""
        dm._df = dm._seed_from_csv()
        overlap_idx = dm._df.index[-20:]
        mock_ibkr_df = dm._df.loc[overlap_idx].copy()

        mock_manager = MagicMock()
        mock_manager.qualify_contract.return_value = MagicMock()
        mock_manager._request_historical_data.return_value = ["bar"]
        dm.ibkr_manager = mock_manager

        # Patch the imports used inside _validate_cache_after_roll
        import src.live_execution.ibkr_client as ibkr_mod
        monkeypatch.setattr(ibkr_mod, "build_cl_contract", lambda **kw: MagicMock())
        monkeypatch.setattr(ibkr_mod, "ib_bars_to_dataframe", lambda bars: mock_ibkr_df)

        assert dm._validate_cache_after_roll()

    def test_shifted_prices_returns_false(self, dm, seed_csv, monkeypatch):
        """Cache is stale when prices differ from IBKR data."""
        dm._df = dm._seed_from_csv()
        overlap_idx = dm._df.index[-20:]
        mock_ibkr_df = dm._df.loc[overlap_idx].copy()
        mock_ibkr_df["Close"] += 0.50  # $0.50 shift

        mock_manager = MagicMock()
        mock_manager.qualify_contract.return_value = MagicMock()
        mock_manager._request_historical_data.return_value = ["bar"]
        dm.ibkr_manager = mock_manager

        import src.live_execution.ibkr_client as ibkr_mod
        monkeypatch.setattr(ibkr_mod, "build_cl_contract", lambda **kw: MagicMock())
        monkeypatch.setattr(ibkr_mod, "ib_bars_to_dataframe", lambda bars: mock_ibkr_df)

        assert not dm._validate_cache_after_roll()

    def test_no_overlap_returns_false(self, dm, seed_csv, monkeypatch):
        """No overlapping timestamps = assume stale."""
        dm._df = dm._seed_from_csv()

        # IBKR data with completely different timestamps
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

        assert not dm._validate_cache_after_roll()

    def test_no_ibkr_data_returns_false(self, dm, seed_csv, monkeypatch):
        """If IBKR returns no bars, assume stale."""
        dm._df = dm._seed_from_csv()

        mock_manager = MagicMock()
        mock_manager.qualify_contract.return_value = MagicMock()
        mock_manager._request_historical_data.return_value = []  # No bars
        dm.ibkr_manager = mock_manager

        import src.live_execution.ibkr_client as ibkr_mod
        monkeypatch.setattr(ibkr_mod, "build_cl_contract", lambda **kw: MagicMock())

        assert dm._validate_cache_after_roll() is False


# ---------------------------------------------------------------------------
# Cache rebuild
# ---------------------------------------------------------------------------

class TestCacheRebuild:
    def test_rebuild_deletes_and_reseeds(self, dm, seed_csv):
        """Rebuild removes stale cache and re-seeds from CSV."""
        dm._df = dm._seed_from_csv()
        dm.save_cache()
        assert dm.cache_path.exists()

        original_len = len(dm._df)
        # Corrupt cache by adding fake data
        dm._df = pd.concat([dm._df, dm._df])
        assert len(dm._df) != original_len

        dm._rebuild_cache()
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
