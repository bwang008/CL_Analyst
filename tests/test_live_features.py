"""
Tests for the live feature pipeline.

Verifies that build_live_features() produces exactly the 80 columns
expected by the current models.
"""

import numpy as np
import pandas as pd
import pytest

from src.live_execution.live_trader import build_live_features


# The 80 feature names the S_Ultimate model expects (from the saved .pkl)
_EXPECTED_FEATURES = [
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


def _generate_ohlcv(n_bars: int = 27000, start_price: float = 70.0) -> pd.DataFrame:
    """Generate synthetic OHLCV data for testing.

    Default 27K bars covers the 26K recommended warmup for compound
    features like VOL_VOLVOL_10080 (rolling-of-rolling, needs ~20K bars).
    """
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


class TestBuildLiveFeatures:
    """Tests for the live feature pipeline."""

    @pytest.fixture(scope="class")
    def ohlcv_data(self):
        """Generate 27,000 bars of synthetic OHLCV (computed once per class)."""
        return _generate_ohlcv()

    @pytest.fixture(scope="class")
    def feature_row(self, ohlcv_data):
        """Run the full feature pipeline once per class."""
        return build_live_features(ohlcv_data, _EXPECTED_FEATURES)

    def test_returns_dataframe(self, feature_row):
        """Features should be a single-row DataFrame."""
        assert feature_row is not None
        assert isinstance(feature_row, pd.DataFrame)
        assert len(feature_row) == 1

    def test_correct_column_count(self, feature_row):
        """Feature row should have exactly 80 columns."""
        assert feature_row.shape[1] == 80

    def test_correct_column_names(self, feature_row):
        """Columns must match the model's expected feature names exactly."""
        assert list(feature_row.columns) == _EXPECTED_FEATURES

    def test_no_nan_in_features(self, feature_row):
        """No NaN values in the final feature row."""
        nan_cols = feature_row.columns[feature_row.isna().iloc[0]].tolist()
        assert len(nan_cols) == 0, f"NaN in: {nan_cols}"

    def test_no_inf_in_features(self, feature_row):
        """No inf values in the final feature row."""
        inf_mask = np.isinf(feature_row.values)
        assert not inf_mask.any(), "Inf detected in feature row"

    def test_insufficient_data_returns_none(self):
        """build_live_features should return None with too few bars."""
        short_df = _generate_ohlcv(n_bars=500)
        result = build_live_features(short_df, _EXPECTED_FEATURES)
        assert result is None

    def test_volume_log_positive(self, feature_row):
        """Volume_Log should be positive (log1p of positive volume)."""
        assert feature_row["Volume_Log"].iloc[0] > 0

    def test_time_sin_cos_range(self, feature_row):
        """Time_Sin and Time_Cos should be in [-1, 1]."""
        assert -1 <= feature_row["Time_Sin"].iloc[0] <= 1
        assert -1 <= feature_row["Time_Cos"].iloc[0] <= 1


# --------------------------------------------------------------------------
# Set_07 Extended Feature List
# --------------------------------------------------------------------------

# Features produced by the extended pipeline (set_07 models)
# Includes all set_06 features plus new clusters and 288-bar window
_EXPECTED_FEATURES_SET_07 = [
    "Time_Sin", "Time_Cos", "Time_DayOfWeek_Sin", "Time_DayOfWeek_Cos",
    "log_ret",
    "VOL_PARK_288", "VOL_ROC_288", "VOL_VOLVOL_288", "VOL_RS_288", "VOL_YZ_288",
    "LIQ_AMIHUD_288", "LIQ_CORWIN_288", "STRUC_EFFICIENCY_288", "STRUC_HURST_100",
    "STRUC_ENTROPY_100", "STRUC_BODY_RATIO", "STRUC_WICK_UP_RATIO", "STRUC_WICK_LOW_RATIO",
    "STRUC_COLOR", "TREND_DONCHIAN_POS_288", "TREND_LR_SLOPE_288", "TREND_LR_R2_288",
    "VOLFLOW_OBV_SLOPE_288", "VOLFLOW_DIVERGENCE_288", "VOLFLOW_VWAP_DIST_288",
    "VOLFLOW_CMF_288", "MOM_STOCH_K_288", "MOM_STOCH_D_288", "EXHAUST_CUM_RET_288",
    "EXHAUST_CUM_ATR_288", "EXHAUST_DIST_HIGH_288",
    "VOL_PARK_864", "VOL_ROC_864", "VOL_VOLVOL_864", "VOL_RS_864", "VOL_YZ_864",
    "LIQ_AMIHUD_864", "LIQ_CORWIN_864", "STRUC_EFFICIENCY_864", "TREND_DONCHIAN_POS_864",
    "TREND_LR_SLOPE_864", "TREND_LR_R2_864", "VOLFLOW_OBV_SLOPE_864", "VOLFLOW_DIVERGENCE_864",
    "VOLFLOW_VWAP_DIST_864", "VOLFLOW_CMF_864", "MOM_STOCH_K_864", "MOM_STOCH_D_864",
    "EXHAUST_CUM_RET_864", "EXHAUST_CUM_ATR_864", "EXHAUST_DIST_HIGH_864",
    "VOL_PARK_2016", "VOL_ROC_2016", "VOL_VOLVOL_2016", "VOL_RS_2016", "VOL_YZ_2016",
    "LIQ_AMIHUD_2016", "LIQ_CORWIN_2016", "STRUC_EFFICIENCY_2016", "TREND_DONCHIAN_POS_2016",
    "TREND_LR_SLOPE_2016", "TREND_LR_R2_2016", "VOLFLOW_OBV_SLOPE_2016", "VOLFLOW_DIVERGENCE_2016",
    "VOLFLOW_VWAP_DIST_2016", "VOLFLOW_CMF_2016", "MOM_STOCH_K_2016", "MOM_STOCH_D_2016",
    "EXHAUST_CUM_RET_2016", "EXHAUST_CUM_ATR_2016", "EXHAUST_DIST_HIGH_2016",
    "VOL_PARK_4032", "VOL_ROC_4032", "VOL_VOLVOL_4032", "VOL_RS_4032", "VOL_YZ_4032",
    "LIQ_AMIHUD_4032", "LIQ_CORWIN_4032", "STRUC_EFFICIENCY_4032", "TREND_DONCHIAN_POS_4032",
    "TREND_LR_SLOPE_4032", "TREND_LR_R2_4032", "VOLFLOW_OBV_SLOPE_4032", "VOLFLOW_DIVERGENCE_4032",
    "VOLFLOW_VWAP_DIST_4032", "VOLFLOW_CMF_4032", "MOM_STOCH_K_4032", "MOM_STOCH_D_4032",
    "EXHAUST_CUM_RET_4032", "EXHAUST_CUM_ATR_4032", "EXHAUST_DIST_HIGH_4032",
    "VOL_PARK_10080", "VOL_ROC_10080", "VOL_VOLVOL_10080", "VOL_RS_10080", "VOL_YZ_10080",
    "LIQ_AMIHUD_10080", "LIQ_CORWIN_10080", "STRUC_EFFICIENCY_10080", "TREND_DONCHIAN_POS_10080",
    "TREND_LR_SLOPE_10080", "TREND_LR_R2_10080", "VOLFLOW_OBV_SLOPE_10080", "VOLFLOW_DIVERGENCE_10080",
    "VOLFLOW_VWAP_DIST_10080", "VOLFLOW_CMF_10080", "MOM_STOCH_K_10080", "MOM_STOCH_D_10080",
    "EXHAUST_CUM_RET_10080", "EXHAUST_CUM_ATR_10080", "EXHAUST_DIST_HIGH_10080",
    "DIST_SKEW_12", "DIST_KURT_12", "DIST_ZSCORE_12",
    "DIST_SKEW_24", "DIST_KURT_24", "DIST_ZSCORE_24",
    "DIST_SKEW_72", "DIST_KURT_72", "DIST_ZSCORE_72",
    "DIST_SKEW_120", "DIST_KURT_120", "DIST_ZSCORE_120",
    "CROSS_VOL_RATIO_1D_35D", "CROSS_VOL_RATIO_3D_14D",
    "CROSS_TREND_DIFF_1D_35D", "CROSS_TREND_DIFF_3D_14D",
    "CROSS_VWAP_DIFF_1D_35D",
    "MOM_RSI_14", "MOM_BB_Width", "MOM_BB_PctB",
    "MOM_ADX_14", "MOM_DMP_14", "MOM_DMN_14",
    "MOM_PPO", "MOM_PPO_Signal", "MOM_PPO_Hist",
    "MACRO_WIDTH_1M", "MACRO_POS_1M", "MACRO_WIDTH_3M", "MACRO_POS_3M",
    "ATR_14", "Volume_Log",
]


class TestBuildLiveFeaturesSet07:
    """Tests for the live feature pipeline with extended set_07."""

    @pytest.fixture(scope="class")
    def ohlcv_data(self):
        """Generate 27,000 bars of synthetic OHLCV (computed once per class)."""
        return _generate_ohlcv()

    @pytest.fixture(scope="class")
    def feature_row(self, ohlcv_data):
        """Run the full extended feature pipeline once per class."""
        return build_live_features(ohlcv_data, _EXPECTED_FEATURES_SET_07)

    def test_returns_dataframe(self, feature_row):
        assert feature_row is not None
        assert isinstance(feature_row, pd.DataFrame)
        assert len(feature_row) == 1

    def test_correct_column_count(self, feature_row):
        """Feature row should have the expected number of set_07 columns."""
        assert feature_row.shape[1] == len(_EXPECTED_FEATURES_SET_07)

    def test_correct_column_names(self, feature_row):
        """Columns must match the set_07 expected feature names exactly."""
        assert list(feature_row.columns) == _EXPECTED_FEATURES_SET_07

    def test_no_nan_in_features(self, feature_row):
        nan_cols = feature_row.columns[feature_row.isna().iloc[0]].tolist()
        assert len(nan_cols) == 0, f"NaN in: {nan_cols}"

    def test_no_inf_in_features(self, feature_row):
        inf_mask = np.isinf(feature_row.values)
        assert not inf_mask.any(), "Inf detected in feature row"

    def test_day_of_week_range(self, feature_row):
        """Time_DayOfWeek_Sin/Cos should be in [-1, 1]."""
        assert -1 <= feature_row["Time_DayOfWeek_Sin"].iloc[0] <= 1
        assert -1 <= feature_row["Time_DayOfWeek_Cos"].iloc[0] <= 1

    def test_stochastic_in_range(self, feature_row):
        """Stochastic K values should be in [0, 1]."""
        for w in [288, 864]:
            k = feature_row[f"MOM_STOCH_K_{w}"].iloc[0]
            assert 0 <= k <= 1, f"MOM_STOCH_K_{w} = {k} out of [0, 1]"

    def test_cmf_in_range(self, feature_row):
        """CMF values should be in [-1, 1]."""
        for w in [288, 864]:
            cmf = feature_row[f"VOLFLOW_CMF_{w}"].iloc[0]
            assert -1 <= cmf <= 1, f"VOLFLOW_CMF_{w} = {cmf} out of [-1, 1]"

