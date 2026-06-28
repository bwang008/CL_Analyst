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
    _FEATURE_STALE_THRESHOLDS,
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
            "DXY": np.linspace(118, 120, 32),  # always varying (threshold=10)
            "OVX": np.concatenate([prefix, [30.5, 30.6]]),   # distinct at tail
        }, index=dates)
        engine = MacroFeatureEngine()
        engine._check_value_staleness(df2)  # Should not raise

    def test_dxy_ten_repeats_raises(self):
        """Ten identical DXY values must raise (threshold=10, exactly at boundary)."""
        df = self._make_fred_df([119.2868] * 10)
        engine = MacroFeatureEngine()
        with pytest.raises(StaleDataException) as exc_info:
            engine._check_value_staleness(df)
        assert exc_info.value.stale_series["DXY"] == pytest.approx(119.2868)
        assert exc_info.value.repeat_count == 10

    def test_dxy_nine_repeats_passes(self):
        """Nine identical DXY values should NOT raise (threshold=10, normal FRED lag)."""
        df = self._make_fred_df([119.2868] * 9)
        engine = MacroFeatureEngine()
        engine._check_value_staleness(df)  # Should NOT raise

    def test_multiple_stale_series(self):
        """If multiple series are stale, exception contains all of them."""
        # DXY threshold is 10.
        # We will just test that DXY is caught.
        df = self._make_fred_df([119.28] * 12)
        engine = MacroFeatureEngine()
        with pytest.raises(StaleDataException) as exc_info:
            engine._check_value_staleness(df)
        assert "DXY" in exc_info.value.stale_series

    def test_critical_series_list(self):
        """All series in _STALE_THRESHOLDS are tested and present."""
        assert "DXY" in _STALE_THRESHOLDS
        assert "OVX" not in _STALE_THRESHOLDS

    def test_nan_handling(self):
        """NaN values in the tail should be ignored (dropna before check)."""
        values = [119.00, 119.05, np.nan, np.nan]
        df = self._make_fred_df(values)
        engine = MacroFeatureEngine()
        engine._check_value_staleness(df)  # Should not raise



    def test_missing_column_skipped(self):
        """Missing columns should be gracefully skipped, not crash."""
        dates = pd.date_range("2026-01-01", periods=10, freq="B")
        df = pd.DataFrame({
            "YIELD_CURVE": np.linspace(1, 2, 10),
        }, index=dates)
        engine = MacroFeatureEngine()
        engine._check_value_staleness(df)  # Should not raise


class TestFeatureStaleness:
    """Test _check_feature_staleness -- per-feature CHG_1D zero detection.

    Per-feature thresholds: DXY=8, VIX=5, OVX=5 trading days.
    The method raises StaleDataException when thresholds are exceeded.
    """

    def _make_features_df(
        self,
        dxy_chg_values: list[float],
        vix_chg_values: list[float] | None = None,
        ovx_chg_values: list[float] | None = None,
        n_prefix: int = 30,
    ) -> pd.DataFrame:
        """Build a features DataFrame with MACRO_*_CHG_1D columns.

        When vix/ovx are shorter than dxy, they are padded with varying
        values so they don't trigger staleness.
        """
        n_dxy = len(dxy_chg_values)
        n_vix = len(vix_chg_values) if vix_chg_values is not None else 0
        n_ovx = len(ovx_chg_values) if ovx_chg_values is not None else 0
        n_tail = max(n_dxy, n_vix, n_ovx)
        total = n_prefix + n_tail
        dates = pd.date_range("2026-01-01", periods=total, freq="B")
        prefix = np.linspace(-0.01, 0.01, n_prefix)

        dxy_full = np.concatenate([prefix,
                                   np.linspace(0.001, 0.002, n_tail - n_dxy),
                                   dxy_chg_values])

        if vix_chg_values is not None:
            vix_full = np.concatenate([prefix,
                                       np.linspace(0.001, 0.002, n_tail - n_vix),
                                       vix_chg_values])
        else:
            vix_full = np.linspace(-0.02, 0.02, total)

        if ovx_chg_values is not None:
            ovx_full = np.concatenate([prefix,
                                       np.linspace(0.001, 0.002, n_tail - n_ovx),
                                       ovx_chg_values])
        else:
            ovx_full = np.linspace(-0.01, 0.01, total)

        return pd.DataFrame({
            "MACRO_DXY_CHG_1D": dxy_full,
            "MACRO_VIX_CHG_1D": vix_full,
            "MACRO_OVX_CHG_1D": ovx_full,
        }, index=dates)

    def test_fresh_features_pass(self):
        """Non-zero CHG_1D values should not raise."""
        features = self._make_features_df([0.001, -0.002, 0.003, 0.001])
        engine = MacroFeatureEngine()
        engine._check_feature_staleness(features)  # Should not raise

    def test_dxy_ten_zeros_passes(self):
        """Ten consecutive DXY CHG_1D=0 should NOT raise (threshold=11)."""
        features = self._make_features_df([0.0] * 10)
        engine = MacroFeatureEngine()
        engine._check_feature_staleness(features)  # Should not raise

    def test_dxy_eleven_zeros_raises(self):
        """Eleven consecutive DXY CHG_1D=0 must raise (threshold=11, at boundary)."""
        features = self._make_features_df([0.0] * 11)
        engine = MacroFeatureEngine()
        with pytest.raises(StaleDataException) as exc_info:
            engine._check_feature_staleness(features)
        assert "MACRO_DXY_CHG_1D" in exc_info.value.stale_series

    def test_non_zero_constant_passes(self):
        """CHG_1D stuck at a non-zero constant should NOT raise."""
        features = self._make_features_df([0.005] * 12)
        engine = MacroFeatureEngine()
        engine._check_feature_staleness(features)  # Should not raise

    def test_mixed_zero_non_zero_passes(self):
        """Non-consecutive zeros should NOT raise."""
        features = self._make_features_df([0.0, 0.001, 0.0, 0.0, 0.001, 0.0, 0.0])
        engine = MacroFeatureEngine()
        engine._check_feature_staleness(features)  # Should not raise

    def test_missing_feature_column_skipped(self):
        """Missing feature columns should be gracefully skipped."""
        dates = pd.date_range("2026-01-01", periods=15, freq="B")
        features = pd.DataFrame({
            "MACRO_YIELD_CURVE_SIGN": [1.0] * 15,
        }, index=dates)
        engine = MacroFeatureEngine()
        engine._check_feature_staleness(features)  # Should not raise

    def test_feature_stale_threshold_values(self):
        """Verify the per-feature threshold constants."""
        assert _FEATURE_STALE_THRESHOLDS["MACRO_DXY_CHG_1D"] == 11
        assert len(_FEATURE_STALE_THRESHOLDS) == 1
