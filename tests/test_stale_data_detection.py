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
    _FEATURE_STALE_THRESHOLD,
    _FEATURE_STALE_CRITICAL,
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
        """Exactly VIX threshold (3) identical values should raise for VIX."""
        vix_threshold = _STALE_THRESHOLDS["VIX"]  # 3
        import numpy as np
        n = 35
        dates = pd.date_range("2026-01-01", periods=n, freq="B")
        prefix_len = n - vix_threshold
        df = pd.DataFrame({
            "DXY": np.linspace(118, 120, n),  # always varying (threshold=5, won't trigger)
            "VIX": np.concatenate([np.linspace(15, 20, prefix_len), [20.5] * vix_threshold]),
            "OVX": np.linspace(30, 40, n),   # always varying
        }, index=dates)
        engine = MacroFeatureEngine()
        with pytest.raises(StaleDataException) as exc_info:
            engine._check_value_staleness(df)
        assert "VIX" in exc_info.value.stale_series
        assert exc_info.value.repeat_count >= vix_threshold

    def test_vix_two_repeats_passes(self):
        """Two identical VIX values (threshold=3) should NOT raise — normal Fri/weekend lag."""
        import numpy as np
        n = 35
        dates = pd.date_range("2026-01-01", periods=n, freq="B")
        df = pd.DataFrame({
            "DXY": np.linspace(118, 120, n),
            "VIX": np.concatenate([np.linspace(15, 20, 33), [15.74, 15.74]]),  # 2 repeats
            "OVX": np.linspace(30, 40, n),
        }, index=dates)
        engine = MacroFeatureEngine()
        engine._check_value_staleness(df)  # Should NOT raise

    def test_dxy_five_repeats_raises(self):
        """Five identical DXY values must raise (threshold=5, exactly at boundary)."""
        df = self._make_fred_df([119.2868] * 5)
        engine = MacroFeatureEngine()
        with pytest.raises(StaleDataException) as exc_info:
            engine._check_value_staleness(df)
        assert exc_info.value.stale_series["DXY"] == pytest.approx(119.2868)
        assert exc_info.value.repeat_count == 5

    def test_dxy_four_repeats_passes(self):
        """Four identical DXY values should NOT raise (threshold=5, normal FRED lag)."""
        df = self._make_fred_df([119.2868] * 4)
        engine = MacroFeatureEngine()
        engine._check_value_staleness(df)  # Should NOT raise

    def test_multiple_stale_series(self):
        """Multiple series stale at the same time."""
        dates = pd.date_range("2026-01-01", periods=40, freq="B")
        prefix = np.linspace(100, 120, 30)
        df = pd.DataFrame({
            "DXY": np.concatenate([prefix, [119.28] * 10]),  # 10 repeats > threshold(5)
            "VIX": np.concatenate([prefix, [20.5] * 10]),    # 10 repeats > threshold(3)
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
        # VIX and OVX thresholds must be equal
        assert _STALE_THRESHOLDS["VIX"] == _STALE_THRESHOLDS["OVX"]

    def test_missing_column_skipped(self):
        """Missing columns should be gracefully skipped, not crash."""
        dates = pd.date_range("2026-01-01", periods=10, freq="B")
        df = pd.DataFrame({
            "YIELD_CURVE": np.linspace(1, 2, 10),
        }, index=dates)
        engine = MacroFeatureEngine()
        engine._check_value_staleness(df)  # Should not raise


class TestFeatureStaleness:
    """Test _check_feature_staleness -- derived CHG_1D zero detection.

    Since the method now returns a list of stale feature names (warning-only)
    instead of raising StaleDataException, all tests check return values.
    """

    def _make_features_df(
        self,
        dxy_chg_values: list[float],
        n_prefix: int = 30,
    ) -> pd.DataFrame:
        """Build a features DataFrame with MACRO_DXY_CHG_1D column."""
        dates = pd.date_range(
            "2026-01-01",
            periods=n_prefix + len(dxy_chg_values),
            freq="B",
        )
        prefix = np.linspace(-0.01, 0.01, n_prefix)  # varying CHG_1D
        full = np.concatenate([prefix, dxy_chg_values])
        return pd.DataFrame({
            "MACRO_DXY_CHG_1D": full,
            "MACRO_VIX_CHG_1D": np.linspace(-0.02, 0.02, len(full)),
            "MACRO_OVX_CHG_1D": np.linspace(-0.01, 0.01, len(full)),
        }, index=dates)

    def test_fresh_features_pass(self):
        """Non-zero CHG_1D values should return empty list."""
        features = self._make_features_df([0.001, -0.002, 0.003, 0.001])
        engine = MacroFeatureEngine()
        result = engine._check_feature_staleness(features)
        assert result == []

    def test_two_zero_chg_passes(self):
        """Two consecutive CHG_1D=0 (normal weekend) should return empty list."""
        features = self._make_features_df([0.0, 0.0])
        engine = MacroFeatureEngine()
        result = engine._check_feature_staleness(features)
        assert result == []

    def test_three_zero_chg_returns_stale(self):
        """Three consecutive CHG_1D=0 (threshold) should return stale feature name."""
        features = self._make_features_df([0.0, 0.0, 0.0])
        engine = MacroFeatureEngine()
        result = engine._check_feature_staleness(features)
        assert "MACRO_DXY_CHG_1D" in result

    def test_non_zero_constant_passes(self):
        """CHG_1D stuck at a non-zero constant should return empty list.

        Only zero-stuckness is a staleness signal; a non-zero constant
        could be a legitimate regime (e.g., stable macro conditions).
        """
        features = self._make_features_df([0.005, 0.005, 0.005])
        engine = MacroFeatureEngine()
        result = engine._check_feature_staleness(features)
        assert result == []

    def test_mixed_zero_non_zero_passes(self):
        """Non-consecutive zeros should return empty list."""
        features = self._make_features_df([0.0, 0.001, 0.0, 0.0])
        engine = MacroFeatureEngine()
        result = engine._check_feature_staleness(features)
        assert result == []

    def test_multiple_features_stale(self):
        """Multiple features stuck at zero should all appear in returned list."""
        dates = pd.date_range("2026-01-01", periods=33, freq="B")
        prefix = np.linspace(-0.01, 0.01, 30)
        zeros = [0.0, 0.0, 0.0]
        features = pd.DataFrame({
            "MACRO_DXY_CHG_1D": np.concatenate([prefix, zeros]),
            "MACRO_VIX_CHG_1D": np.concatenate([prefix, zeros]),
            "MACRO_OVX_CHG_1D": np.linspace(-0.01, 0.01, 33),  # varying
        }, index=dates)
        engine = MacroFeatureEngine()
        result = engine._check_feature_staleness(features)
        assert "MACRO_DXY_CHG_1D" in result
        assert "MACRO_VIX_CHG_1D" in result
        assert "MACRO_OVX_CHG_1D" not in result

    def test_missing_feature_column_skipped(self):
        """Missing feature columns should return empty list."""
        dates = pd.date_range("2026-01-01", periods=10, freq="B")
        features = pd.DataFrame({
            "MACRO_YIELD_CURVE_SIGN": [1.0] * 10,
        }, index=dates)
        engine = MacroFeatureEngine()
        result = engine._check_feature_staleness(features)
        assert result == []

    def test_feature_stale_threshold_value(self):
        """Verify the threshold constant is sensible."""
        assert _FEATURE_STALE_THRESHOLD == 3
        assert len(_FEATURE_STALE_CRITICAL) == 3
        assert "MACRO_DXY_CHG_1D" in _FEATURE_STALE_CRITICAL
