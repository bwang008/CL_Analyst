"""
Tests for the NaN hard-block guard in the live feature pipeline.

Verifies that build_live_features() returns None (hard block) when
features contain NaN after ffill, instead of silently filling with
bfill or zero. This is the core data-integrity safeguard that prevents
the model from inferring on fabricated values.
"""

import numpy as np
import pandas as pd
import pytest

from src.live_execution.live_trader import build_live_features


# Minimal feature set that exercises the key code paths (Time, log_ret,
# volatility, momentum, macro, ATR, Volume_Log).  We use set_06 features
# because they require the smallest window (10,080 bars max) and are
# fastest to generate in tests.
_MINIMAL_FEATURES = [
    "Time_Sin", "Time_Cos", "log_ret",
    "VOL_PARK_864", "VOL_ROC_864", "VOL_VOLVOL_864", "VOL_RS_864", "VOL_YZ_864",
    "LIQ_AMIHUD_864", "LIQ_CORWIN_864",
    "STRUC_EFFICIENCY_864", "STRUC_HURST_100", "STRUC_ENTROPY_100",
    "STRUC_BODY_RATIO", "STRUC_WICK_UP_RATIO", "STRUC_WICK_LOW_RATIO", "STRUC_COLOR",
    "TREND_DONCHIAN_POS_864", "TREND_LR_SLOPE_864", "TREND_LR_R2_864",
    "VOLFLOW_OBV_SLOPE_864", "VOLFLOW_DIVERGENCE_864", "VOLFLOW_VWAP_DIST_864",
    "VOL_PARK_2016", "VOL_ROC_2016", "VOL_VOLVOL_2016", "VOL_RS_2016", "VOL_YZ_2016",
    "LIQ_AMIHUD_2016", "LIQ_CORWIN_2016",
    "STRUC_EFFICIENCY_2016",
    "TREND_DONCHIAN_POS_2016", "TREND_LR_SLOPE_2016", "TREND_LR_R2_2016",
    "VOLFLOW_OBV_SLOPE_2016", "VOLFLOW_DIVERGENCE_2016", "VOLFLOW_VWAP_DIST_2016",
    "VOL_PARK_4032", "VOL_ROC_4032", "VOL_VOLVOL_4032", "VOL_RS_4032", "VOL_YZ_4032",
    "LIQ_AMIHUD_4032", "LIQ_CORWIN_4032",
    "STRUC_EFFICIENCY_4032",
    "TREND_DONCHIAN_POS_4032", "TREND_LR_SLOPE_4032", "TREND_LR_R2_4032",
    "VOLFLOW_OBV_SLOPE_4032", "VOLFLOW_DIVERGENCE_4032", "VOLFLOW_VWAP_DIST_4032",
    "VOL_PARK_10080", "VOL_ROC_10080", "VOL_VOLVOL_10080", "VOL_RS_10080", "VOL_YZ_10080",
    "LIQ_AMIHUD_10080", "LIQ_CORWIN_10080",
    "STRUC_EFFICIENCY_10080",
    "TREND_DONCHIAN_POS_10080", "TREND_LR_SLOPE_10080", "TREND_LR_R2_10080",
    "VOLFLOW_OBV_SLOPE_10080", "VOLFLOW_DIVERGENCE_10080", "VOLFLOW_VWAP_DIST_10080",
    "MOM_RSI_14", "MOM_BB_Width", "MOM_BB_PctB",
    "MOM_ADX_14", "MOM_DMP_14", "MOM_DMN_14",
    "MOM_PPO", "MOM_PPO_Signal", "MOM_PPO_Hist",
    "MACRO_WIDTH_1M", "MACRO_POS_1M", "MACRO_WIDTH_3M", "MACRO_POS_3M",
    "ATR_14", "Volume_Log",
]


def _generate_ohlcv(n_bars: int = 11000, start_price: float = 70.0) -> pd.DataFrame:
    """Generate synthetic OHLCV data for testing."""
    np.random.seed(42)
    dates = pd.date_range(start="2024-01-01", periods=n_bars, freq="5min")

    returns = np.random.normal(0, 0.001, n_bars)
    close = start_price + np.cumsum(returns)
    high = close + np.abs(np.random.normal(0, 0.05, n_bars))
    low = close - np.abs(np.random.normal(0, 0.05, n_bars))
    open_ = np.roll(close, 1)
    open_[0] = start_price
    volume = np.random.randint(100, 10000, n_bars).astype(float)

    df = pd.DataFrame({
        "Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume,
    }, index=dates)
    df.index.name = "DateTime"
    return df


class TestNaNHardBlock:
    """Tests for the NaN hard-block guard (no bfill, no fillna(0))."""

    # ------------------------------------------------------------------
    # Baseline: sufficient data produces valid features
    # ------------------------------------------------------------------

    def test_sufficient_data_returns_features(self):
        """With 27K bars (above the 26K recommended warmup), all features
        including compound ones like VOL_VOLVOL_10080 should be computable."""
        df = _generate_ohlcv(n_bars=27000)
        result = build_live_features(df, _MINIMAL_FEATURES)
        assert result is not None, (
            "build_live_features returned None with sufficient data — "
            "a feature may have unexpected NaN"
        )
        nan_cols = result.columns[result.isna().iloc[0]].tolist()
        assert len(nan_cols) == 0, f"NaN in features with 27K bars: {nan_cols}"

    # ------------------------------------------------------------------
    # Hard block: insufficient warmup → must return None
    # ------------------------------------------------------------------

    def test_insufficient_warmup_returns_none(self):
        """With fewer bars than the largest rolling window, features
        with long lookbacks will produce NaN. The pipeline must return
        None (hard block) rather than filling with fabricated values."""
        # 500 bars is clearly under the 10,080 min-bar threshold
        df = _generate_ohlcv(n_bars=500)
        result = build_live_features(df, _MINIMAL_FEATURES)
        assert result is None, (
            "build_live_features should return None when data is insufficient "
            "for feature warmup, but got a DataFrame instead"
        )

    def test_marginal_warmup_blocks_on_nan(self):
        """With bars slightly above the min-bar threshold but below
        compound feature requirements (e.g., VOL_VOLVOL_10080 needing
        2x10080=20160 bars), the pipeline should detect NaN in the
        compound features and return None."""
        # 10,200 bars: passes the len(df) < alpha_windows[-1] check
        # (10,080) but VOL_VOLVOL_10080 needs ~20K bars for its
        # rolling-of-rolling computation.
        df = _generate_ohlcv(n_bars=10200)
        result = build_live_features(df, _MINIMAL_FEATURES)
        # With strict NaN guard, this should return None because
        # VOL_VOLVOL_10080 will still be NaN
        assert result is None, (
            "build_live_features should return None when compound features "
            "(VOL_VOLVOL_10080) lack sufficient warmup history, but got "
            "a DataFrame instead — bfill/fillna(0) may be leaking through"
        )

    # ------------------------------------------------------------------
    # Inf handling: inf replaced, does not leak through
    # ------------------------------------------------------------------

    def test_inf_values_do_not_leak(self):
        """Inf values in the input should be replaced with NaN and
        either forward-filled from prior valid data or blocked."""
        df = _generate_ohlcv(n_bars=27000)
        # Inject inf into a few Close values near the end (but not last row
        # so ffill has a prior valid value to pull from)
        df.iloc[-3, df.columns.get_loc("Close")] = np.inf
        df.iloc[-5, df.columns.get_loc("Close")] = -np.inf
        result = build_live_features(df, _MINIMAL_FEATURES)
        # Should either produce clean features (if ffill covered it)
        # or return None (if the inf propagated to NaN in the final row)
        if result is not None:
            inf_mask = np.isinf(result.values)
            assert not inf_mask.any(), "Inf values leaked into the feature row"
            nan_cols = result.columns[result.isna().iloc[0]].tolist()
            assert len(nan_cols) == 0, f"NaN leaked after inf replacement: {nan_cols}"

    # ------------------------------------------------------------------
    # ffill works: mid-series NaN gaps are forward-filled
    # ------------------------------------------------------------------

    def test_ffill_covers_mid_series_gaps(self):
        """NaN in the middle of the series (not at the start) should be
        forward-filled from the prior valid bar. This is the defensible
        ffill behavior we retained."""
        df = _generate_ohlcv(n_bars=27000)
        # Inject a few NaN values in the middle (well after warmup)
        df.iloc[9000, df.columns.get_loc("Volume")] = np.nan
        df.iloc[9001, df.columns.get_loc("Volume")] = np.nan
        result = build_live_features(df, _MINIMAL_FEATURES)
        assert result is not None, (
            "Mid-series NaN gaps should be forward-filled, not trigger a block"
        )
        nan_cols = result.columns[result.isna().iloc[0]].tolist()
        assert len(nan_cols) == 0, f"NaN after mid-series gap ffill: {nan_cols}"

    # ------------------------------------------------------------------
    # No zero-fill: verify 0 is not fabricated
    # ------------------------------------------------------------------

    def test_no_zero_fill_on_nan_features(self):
        """Features that are NaN due to insufficient warmup must NOT be
        filled with 0. The pipeline should return None instead.

        This is the critical regression test — the old behavior was:
          bfill -> fillna(0) -> proceed to inference on fabricated zeros.
        The new behavior must be: return None."""
        # Generate data that's just barely enough to compute SOME features
        # but not all (compound features need more data)
        df = _generate_ohlcv(n_bars=10200)
        result = build_live_features(df, _MINIMAL_FEATURES)
        # The pipeline must block — not fill with zeros
        assert result is None, (
            "Pipeline should block on NaN features, not zero-fill them"
        )


class TestNaNGuardEdgeCases:
    """Edge cases for the NaN hard-block guard."""

    def test_all_nan_column_blocks(self):
        """A column that is entirely NaN (e.g., from a missing data source)
        cannot be forward-filled and must trigger the hard block."""
        df = _generate_ohlcv(n_bars=27000)
        # Wipe out an entire column — ffill can't help if there's no
        # prior valid value.  Volume -> NaN means Volume_Log -> NaN.
        df["Volume"] = np.nan
        result = build_live_features(df, _MINIMAL_FEATURES)
        assert result is None, (
            "An entirely-NaN column should trigger the hard block, "
            "but build_live_features returned a DataFrame"
        )

    def test_missing_feature_column_returns_none(self):
        """If the model expects a feature column that doesn't exist in
        the data at all, the pipeline should return None."""
        df = _generate_ohlcv(n_bars=27000)
        bogus_features = _MINIMAL_FEATURES + ["NONEXISTENT_FEATURE_XYZ"]
        result = build_live_features(df, bogus_features)
        assert result is None, (
            "Missing feature columns should cause build_live_features to "
            "return None"
        )
