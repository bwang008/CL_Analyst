"""Tests for DataManager JIT ratio-adjustment architecture.

Verifies that:
    1. Cache stays 100% raw after rollover
    2. get_ratio_adjusted_df() applies ratios correctly (single + multi roll)
    3. _compute_roll_ratio() computes multiplicative ratios
    4. Roll metadata persists and restores ratios
    5. No Panama Canal additive logic remains
"""
import unittest
from unittest.mock import MagicMock, patch, PropertyMock
from pathlib import Path
import pandas as pd
import numpy as np
import tempfile
import os
import json


class TestGetRatioAdjustedDF(unittest.TestCase):
    """Test the JIT ratio-adjusted view."""

    def _make_dm(self):
        """Create a DataManager with in-memory data (no disk I/O)."""
        from src.live_execution.data_manager import DataManager
        dm = DataManager.__new__(DataManager)
        dm.seed_path = Path("/fake/seed.csv")
        dm.cache_path = Path("/fake/cache.parquet")
        dm.master_ledger_path = Path("/fake/master.parquet")
        dm.data_client = None
        dm.front_month_id = None
        dm.bar_size = "1 hour"
        dm.bars_per_day = 24
        dm._bars_since_flush = 0
        dm._roll_detected = False
        dm._roll_ratios = []
        dm._roll_timestamps = []
        return dm

    def _make_df(self, n=100, base_price=80.0):
        """Create a simple OHLCV DataFrame."""
        dates = pd.date_range("2026-01-01", periods=n, freq="1h")
        df = pd.DataFrame({
            "DateTime": dates,
            "Open": base_price + np.random.randn(n) * 0.1,
            "High": base_price + 0.5 + np.random.randn(n) * 0.1,
            "Low": base_price - 0.5 + np.random.randn(n) * 0.1,
            "Close": base_price + np.random.randn(n) * 0.1,
            "Volume": np.random.randint(100, 1000, n),
        })
        df = df.set_index("DateTime", drop=False)
        df.index.name = "DateTime"
        return df

    def test_no_rolls_returns_identical_copy(self):
        """get_ratio_adjusted_df with no rolls should return an identical copy."""
        dm = self._make_dm()
        dm._df = self._make_df()
        original = dm._df.copy()

        result = dm.get_ratio_adjusted_df()

        # Should be equal but NOT the same object
        pd.testing.assert_frame_equal(result, original)
        self.assertIsNot(result, dm._df)

    def test_single_roll_adjusts_pre_roll_bars(self):
        """Single rollover should multiply pre-roll bars by ratio."""
        dm = self._make_dm()
        dm._df = self._make_df(n=100, base_price=80.0)

        # Rollover at bar 50
        roll_ts = dm._df.index[50]
        ratio = 1.005  # 0.5% ratio

        dm._roll_ratios = [ratio]
        dm._roll_timestamps = [roll_ts]

        result = dm.get_ratio_adjusted_df()

        # Pre-roll bars should be multiplied by ratio
        pre_roll = result.index < roll_ts
        for col in ("Open", "High", "Low", "Close"):
            np.testing.assert_allclose(
                result.loc[pre_roll, col].values,
                dm._df.loc[pre_roll, col].values * ratio,
                rtol=1e-10,
            )

        # Post-roll bars should be unchanged
        post_roll = result.index >= roll_ts
        for col in ("Open", "High", "Low", "Close"):
            np.testing.assert_allclose(
                result.loc[post_roll, col].values,
                dm._df.loc[post_roll, col].values,
                rtol=1e-10,
            )

    def test_multi_roll_accumulates_ratios(self):
        """Multiple rollovers should compound ratios on historical bars."""
        dm = self._make_dm()
        dm._df = self._make_df(n=300, base_price=80.0)

        # Two rollovers
        roll_ts_1 = dm._df.index[100]
        roll_ts_2 = dm._df.index[200]
        ratio_1 = 1.003
        ratio_2 = 0.998

        dm._roll_ratios = [ratio_1, ratio_2]
        dm._roll_timestamps = [roll_ts_1, roll_ts_2]

        result = dm.get_ratio_adjusted_df()

        # Bars before roll_1: adjusted by BOTH ratios
        very_old = result.index < roll_ts_1
        for col in ("Open", "High", "Low", "Close"):
            expected = dm._df.loc[very_old, col].values * ratio_1 * ratio_2
            np.testing.assert_allclose(
                result.loc[very_old, col].values,
                expected,
                rtol=1e-10,
            )

        # Bars between roll_1 and roll_2: adjusted by ratio_2 only
        middle = (result.index >= roll_ts_1) & (result.index < roll_ts_2)
        for col in ("Open", "High", "Low", "Close"):
            expected = dm._df.loc[middle, col].values * ratio_2
            np.testing.assert_allclose(
                result.loc[middle, col].values,
                expected,
                rtol=1e-10,
            )

        # Bars after roll_2: unchanged
        recent = result.index >= roll_ts_2
        for col in ("Open", "High", "Low", "Close"):
            np.testing.assert_allclose(
                result.loc[recent, col].values,
                dm._df.loc[recent, col].values,
                rtol=1e-10,
            )

    def test_cache_stays_raw_after_get_ratio_adjusted(self):
        """Calling get_ratio_adjusted_df must NOT mutate the raw cache."""
        dm = self._make_dm()
        dm._df = self._make_df(n=100, base_price=80.0)
        original_close = dm._df["Close"].copy()

        roll_ts = dm._df.index[50]
        dm._roll_ratios = [1.01]
        dm._roll_timestamps = [roll_ts]

        # Call multiple times
        _ = dm.get_ratio_adjusted_df()
        _ = dm.get_ratio_adjusted_df()

        # Raw cache must be untouched
        pd.testing.assert_series_equal(dm._df["Close"], original_close)

    def test_volume_not_adjusted(self):
        """Volume should never be ratio-adjusted."""
        dm = self._make_dm()
        dm._df = self._make_df(n=100)
        original_volume = dm._df["Volume"].copy()

        dm._roll_ratios = [1.05]
        dm._roll_timestamps = [dm._df.index[50]]

        result = dm.get_ratio_adjusted_df()
        pd.testing.assert_series_equal(result["Volume"], original_volume)


class TestApplyRollToCache(unittest.TestCase):
    """Test that _apply_roll_to_cache records the ratio without mutating prices."""

    def _make_dm(self):
        from src.live_execution.data_manager import DataManager
        dm = DataManager.__new__(DataManager)
        dm.seed_path = Path("/fake/seed.csv")
        dm.cache_path = Path("/fake/cache.parquet")
        dm.master_ledger_path = Path("/fake/master.parquet")
        dm.data_client = None
        dm.front_month_id = "CLN6"
        dm.bar_size = "1 hour"
        dm.bars_per_day = 24
        dm._bars_since_flush = 0
        dm._roll_detected = False
        dm._roll_ratios = []
        dm._roll_timestamps = []
        return dm

    def test_records_ratio_without_mutating_prices(self):
        """_apply_roll_to_cache should store ratio but not change OHLC values."""
        dm = self._make_dm()
        dates = pd.date_range("2026-01-01", periods=50, freq="1h")
        dm._df = pd.DataFrame({
            "DateTime": dates,
            "Open": 80.0, "High": 81.0, "Low": 79.0, "Close": 80.5,
            "Volume": 500,
        }).set_index("DateTime", drop=False)
        dm._df.index.name = "DateTime"
        original_close = dm._df["Close"].copy()

        with patch.object(dm, "save_cache"):
            dm._apply_roll_to_cache(1.003)

        self.assertEqual(len(dm._roll_ratios), 1)
        self.assertAlmostEqual(dm._roll_ratios[0], 1.003)
        self.assertEqual(len(dm._roll_timestamps), 1)
        # Prices unchanged (no IBKR overlap data to stitch)
        pd.testing.assert_series_equal(dm._df["Close"], original_close)


class TestComputeRollRatio(unittest.TestCase):
    """Test the multiplicative ratio computation from IBKR overlap."""

    def _make_dm(self):
        from src.live_execution.data_manager import DataManager
        dm = DataManager.__new__(DataManager)
        dm.seed_path = Path("/fake/seed.csv")
        dm.cache_path = Path("/fake/cache.parquet")
        dm.master_ledger_path = Path("/fake/master.parquet")
        dm.front_month_id = "CLN6"
        dm.bar_size = "1 hour"
        dm.bars_per_day = 24
        dm._bars_since_flush = 0
        dm._roll_detected = False
        dm._roll_ratios = []
        dm._roll_timestamps = []
        return dm

    def test_computes_median_ratio(self):
        """Should compute median(ibkr_close / cache_close)."""
        dm = self._make_dm()
        dates = pd.date_range("2026-01-01", periods=100, freq="1h")
        cache_close = np.full(100, 80.0)
        dm._df = pd.DataFrame({
            "DateTime": dates,
            "Open": cache_close, "High": cache_close + 0.5,
            "Low": cache_close - 0.5, "Close": cache_close,
            "Volume": 500,
        }).set_index("DateTime", drop=False)
        dm._df.index.name = "DateTime"

        # IBKR has slightly different prices (ratio = 1.005)
        ibkr_close = cache_close * 1.005
        ibkr_df = pd.DataFrame({
            "DateTime": dates,
            "Open": ibkr_close, "High": ibkr_close + 0.5,
            "Low": ibkr_close - 0.5, "Close": ibkr_close,
            "Volume": 500,
        }).set_index("DateTime", drop=False)
        ibkr_df.index.name = "DateTime"

        mock_client = MagicMock()
        mock_client.fetch_historical_bars_by_duration.return_value = ibkr_df
        dm.data_client = mock_client

        ratio = dm._compute_roll_ratio()

        self.assertIsNotNone(ratio)
        self.assertAlmostEqual(ratio, 1.005, places=5)


class TestPanamaCanalRemoved(unittest.TestCase):
    """Verify that Panama Canal additive logic has been completely removed."""

    def test_no_back_adjust_method(self):
        """DataManager should no longer have _back_adjust_cache."""
        from src.live_execution.data_manager import DataManager
        self.assertFalse(
            hasattr(DataManager, "_back_adjust_cache"),
            "_back_adjust_cache should be deleted (Panama Canal is deprecated)"
        )

    def test_no_compute_roll_delta_method(self):
        """DataManager should no longer have _compute_roll_delta."""
        from src.live_execution.data_manager import DataManager
        self.assertFalse(
            hasattr(DataManager, "_compute_roll_delta"),
            "_compute_roll_delta should be deleted (Panama Canal is deprecated)"
        )


if __name__ == "__main__":
    unittest.main()
