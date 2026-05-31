"""
Tests for StaleDataException and value-staleness detection.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.features.macro_features import (
    MacroFeatureEngine,
    StaleDataException,
    _STALE_THRESHOLDS,
)


class TestStaleDataException:
    """Verify StaleDataException attributes and formatting."""

    def test_exception_attributes(self):
        exc = StaleDataException(
            stale_series={"DXY": 119.2868, "VIX": 20.5},
            repeat_count=4,
        )
        assert exc.stale_series == {"DXY": 119.2868, "VIX": 20.5}
        assert exc.repeat_count == 4
        assert "DXY" in str(exc)
        assert "VIX" in str(exc)
        assert "4 consecutive" in str(exc)

    def test_exception_is_runtime_error(self):
        exc = StaleDataException(stale_series={}, repeat_count=0, message="test")
        assert isinstance(exc, RuntimeError)


class TestValueStaleness:
    """Test _check_value_staleness detection logic."""

    def _make_fred_df(self, dxy_values: list[float], n_prefix: int = 30) -> pd.DataFrame:
        """Build a minimal FRED-shaped DataFrame with a DXY column."""
        dates = pd.date_range("2026-01-01", periods=n_prefix + len(dxy_values), freq="B")
        # prefix with varying values, then append the test values
        prefix = np.linspace(100.0, 120.0, n_prefix)
        full_dxy = np.concatenate([prefix, dxy_values])
        df = pd.DataFrame({
            "DXY": full_dxy,
            "VIX": np.linspace(15.0, 25.0, len(full_dxy)),  # always varying
            "OVX": np.linspace(30.0, 40.0, len(full_dxy)),   # always varying
        }, index=dates)
        return df

    def test_fresh_data_passes(self):
        """No exception when DXY has distinct values at the tail."""
        df = self._make_fred_df([119.00, 119.05, 119.10, 119.15])
        engine = MacroFeatureEngine()
        engine._check_value_staleness(df)  # Should not raise

    def test_single_repeat_passes(self):
        """One repeat (below VIX/OVX threshold of 2) should not raise."""
        # VIX threshold is 2, so 1 repeat should not fire
        df = self._make_fred_df([20.5, 20.5])  # only 1 repeat in tail
        # Manually set 1 repeat: first is different
        import numpy as np
        dates = pd.date_range("2026-01-01", periods=32, freq="B")
        prefix = np.linspace(15.0, 25.0, 30)
        dxy_vals = np.concatenate([prefix, [20.5, 20.6]])  # last 2 are DIFFERENT
        df2 = pd.DataFrame({
            "DXY": np.linspace(118, 120, 32),  # always varying (threshold=6)
            "VIX": np.concatenate([prefix, [20.5, 20.6]]),  # distinct at tail
            "OVX": np.linspace(30, 40, 32),   # always varying
        }, index=dates)
        engine = MacroFeatureEngine()
        engine._check_value_staleness(df2)  # Should not raise

    def test_exact_threshold_raises(self):
        """Exactly VIX threshold (2) identical values should raise for VIX."""
        vix_threshold = _STALE_THRESHOLDS["VIX"]  # 2
        import numpy as np
        n = 35
        dates = pd.date_range("2026-01-01", periods=n, freq="B")
        prefix_len = n - vix_threshold
        df = pd.DataFrame({
            "DXY": np.linspace(118, 120, n),  # always varying (threshold=6, won't trigger)
            "VIX": np.concatenate([np.linspace(15, 20, prefix_len), [20.5] * vix_threshold]),
            "OVX": np.linspace(30, 40, n),   # always varying
        }, index=dates)
        engine = MacroFeatureEngine()
        with pytest.raises(StaleDataException) as exc_info:
            engine._check_value_staleness(df)
        assert "VIX" in exc_info.value.stale_series
        assert exc_info.value.repeat_count >= vix_threshold

    def test_dxy_six_repeats_raises(self):
        """Six identical DXY values (realistic FRED lag scenario) must raise."""
        df = self._make_fred_df([119.2868, 119.2868, 119.2868, 119.2868, 119.2868, 119.2868])
        engine = MacroFeatureEngine()
        with pytest.raises(StaleDataException) as exc_info:
            engine._check_value_staleness(df)
        assert exc_info.value.stale_series["DXY"] == pytest.approx(119.2868)
        assert exc_info.value.repeat_count == 6

    def test_dxy_five_repeats_passes(self):
        """Five identical DXY values is within the FRED lag tolerance (threshold=6)."""
        df = self._make_fred_df([119.2868, 119.2868, 119.2868, 119.2868, 119.2868])
        engine = MacroFeatureEngine()
        engine._check_value_staleness(df)  # Should NOT raise

    def test_multiple_stale_series(self):
        """Multiple series stale at the same time."""
        dates = pd.date_range("2026-01-01", periods=40, freq="B")
        prefix = np.linspace(100, 120, 30)
        df = pd.DataFrame({
            "DXY": np.concatenate([prefix, [119.28] * 10]),  # 10 repeats > threshold(6)
            "VIX": np.concatenate([prefix, [20.5] * 10]),    # 10 repeats > threshold(2)
            "OVX": np.linspace(30, 40, 40),                  # this one is fine
        }, index=dates)
        engine = MacroFeatureEngine()
        with pytest.raises(StaleDataException) as exc_info:
            engine._check_value_staleness(df)
        assert "DXY" in exc_info.value.stale_series
        assert "VIX" in exc_info.value.stale_series
        assert "OVX" not in exc_info.value.stale_series

    def test_nan_handling(self):
        """NaN values in the tail should be ignored (dropna before check)."""
        values = [119.00, 119.05, np.nan, np.nan]
        df = self._make_fred_df(values)
        engine = MacroFeatureEngine()
        engine._check_value_staleness(df)  # Should not raise

    def test_critical_series_list(self):
        """Verify the thresholds dict contains expected series."""
        assert "DXY" in _STALE_THRESHOLDS
        assert "VIX" in _STALE_THRESHOLDS
        assert "OVX" in _STALE_THRESHOLDS
        # DXY should have a higher threshold than VIX/OVX due to FRED lag
        assert _STALE_THRESHOLDS["DXY"] > _STALE_THRESHOLDS["VIX"]

    def test_missing_column_skipped(self):
        """Missing columns should be gracefully skipped, not crash."""
        dates = pd.date_range("2026-01-01", periods=10, freq="B")
        df = pd.DataFrame({
            "YIELD_CURVE": np.linspace(1, 2, 10),
        }, index=dates)
        engine = MacroFeatureEngine()
        engine._check_value_staleness(df)  # Should not raise
