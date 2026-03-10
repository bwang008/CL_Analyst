"""
Tests for AlphaFactory feature generation.
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.features.alpha_factory import AlphaFactory


@pytest.fixture
def flat_line_data():
    """Price never moves; volatility should be zero."""
    n_rows = 100
    index = pd.date_range(start="2024-01-01", periods=n_rows, freq="5min")
    return pd.DataFrame(
        {
            "Open": [100.0] * n_rows,
            "High": [100.0] * n_rows,
            "Low": [100.0] * n_rows,
            "Close": [100.0] * n_rows,
            "Volume": [1000] * n_rows,
        },
        index=index,
    )


@pytest.fixture
def perfect_trend_data():
    """Price moves up by a fixed increment each bar."""
    n_rows = 100
    index = pd.date_range(start="2024-01-01", periods=n_rows, freq="5min")
    prices = np.linspace(100.0, 199.0, n_rows)
    return pd.DataFrame(
        {
            "Open": prices,
            "High": prices,
            "Low": prices,
            "Close": prices,
            "Volume": [1000] * n_rows,
        },
        index=index,
    )


@pytest.fixture
def long_trend_data():
    """Longer trend to support macro windows."""
    n_rows = 30000
    index = pd.date_range(start="2024-01-01", periods=n_rows, freq="5min")
    prices = np.linspace(100.0, 160.0, n_rows)
    return pd.DataFrame(
        {
            "Open": prices,
            "High": prices + 0.5,
            "Low": prices - 0.5,
            "Close": prices,
            "Volume": np.full(n_rows, 1000),
        },
        index=index,
    )


def test_volatility_flat_line(flat_line_data):
    factory = AlphaFactory(flat_line_data)
    df = factory.add_volatility_cluster(window=10)

    for col in ["VOL_PARK_10", "VOL_RS_10", "VOL_YZ_10"]:
        series = df[col].dropna()
        assert np.isclose(series, 0.0).all(), f"{col} should be 0 for flat data"


def test_volatility_non_negative(perfect_trend_data):
    factory = AlphaFactory(perfect_trend_data)
    df = factory.add_volatility_cluster(window=10)

    for col in ["VOL_PARK_10", "VOL_RS_10", "VOL_YZ_10"]:
        series = df[col].dropna()
        assert (series >= 0).all(), f"{col} should be non-negative"


def test_liquidity_non_negative(perfect_trend_data):
    factory = AlphaFactory(perfect_trend_data)
    df = factory.add_liquidity_cluster(window=10)

    for col in ["LIQ_AMIHUD_10", "LIQ_CORWIN_10"]:
        series = df[col].dropna()
        assert (series >= 0).all(), f"{col} should be non-negative"


def test_add_all_features_columns(flat_line_data):
    factory = AlphaFactory(flat_line_data)
    df = factory.add_all_features(windows=[10], include_macro=False)

    expected_cols = [
        "VOL_PARK_10",
        "VOL_RS_10",
        "VOL_YZ_10",
        "LIQ_AMIHUD_10",
        "LIQ_CORWIN_10",
        "STRUC_EFFICIENCY_10",
        "MOM_RSI_14",
        "MOM_BB_Width",
        "MOM_BB_PctB",
    ]

    for col in expected_cols:
        assert col in df.columns, f"Missing expected column: {col}"


def test_no_inf_or_nan_after_warmup(perfect_trend_data):
    factory = AlphaFactory(perfect_trend_data)
    df = factory.add_all_features(windows=[10], include_macro=False)

    warmup_df = df.iloc[25:]
    assert not warmup_df.isin([np.inf, -np.inf]).any().any(), "Found inf values"

    feature_cols = [
        "VOL_PARK_10",
        "VOL_RS_10",
        "VOL_YZ_10",
        "LIQ_AMIHUD_10",
        "LIQ_CORWIN_10",
        "STRUC_EFFICIENCY_10",
        "MOM_RSI_14",
        "MOM_BB_Width",
        "MOM_BB_PctB",
    ]
    assert not warmup_df[feature_cols].isna().any().any(), "Found NaNs after warmup"


def test_structure_physics(flat_line_data):
    factory = AlphaFactory(flat_line_data)
    df = factory.add_structure_cluster(window=10)

    assert "STRUC_ENTROPY_100" in df.columns
    assert "STRUC_HURST_100" in df.columns

    entropy_series = df["STRUC_ENTROPY_100"].dropna()
    assert not entropy_series.empty, "Entropy should be calculated"
    assert entropy_series.iloc[-1] < 1e-6, "Entropy should be near 0 on flat data"

    hurst_series = df["STRUC_HURST_100"].dropna()
    assert not hurst_series.empty, "Hurst should be calculated"


def test_macro_context_integration(long_trend_data):
    factory = AlphaFactory(long_trend_data)
    df = factory.add_all_features(
        windows=[10],
        include_macro=True,
        macro_windows={"1M": 840, "3M": 2160},
    )

    assert "MACRO_POS_3M" in df.columns
    pos_series = df["MACRO_POS_3M"]
    first_valid = pos_series.first_valid_index()
    assert first_valid is not None, "MACRO_POS_3M should not be all NaN"
    assert not pos_series.loc[first_valid:].isna().any(), "Forward-fill gaps detected"


def test_return_distribution_non_nan(perfect_trend_data):
    factory = AlphaFactory(perfect_trend_data)
    df = factory.add_return_distribution_cluster(window=10)

    for col in ["DIST_SKEW_10", "DIST_KURT_10", "DIST_ZSCORE_10"]:
        assert col in df.columns, f"Missing column: {col}"
        series = df[col].dropna()
        assert not series.empty, f"{col} is all NaN"
        assert not np.isinf(series).any(), f"{col} has inf values"


def test_stochastic_bounded(perfect_trend_data):
    factory = AlphaFactory(perfect_trend_data)
    df = factory.add_stochastic_cluster(window=10)

    k_series = df["MOM_STOCH_K_10"].dropna()
    assert not k_series.empty, "MOM_STOCH_K_10 is all NaN"
    assert (k_series >= 0).all() and (k_series <= 1).all(), (
        "MOM_STOCH_K_10 should be in [0, 1]"
    )

    d_series = df["MOM_STOCH_D_10"].dropna()
    assert not d_series.empty, "MOM_STOCH_D_10 is all NaN"
    assert (d_series >= 0).all() and (d_series <= 1).all(), (
        "MOM_STOCH_D_10 should be in [0, 1]"
    )


def test_cmf_bounded(long_trend_data):
    factory = AlphaFactory(long_trend_data)
    df = factory.add_volume_flow_cluster(window=10)

    assert "VOLFLOW_CMF_10" in df.columns, "Missing VOLFLOW_CMF_10"
    cmf = df["VOLFLOW_CMF_10"].dropna()
    assert not cmf.empty, "VOLFLOW_CMF_10 is all NaN"
    assert (cmf >= -1).all() and (cmf <= 1).all(), (
        "VOLFLOW_CMF should be in [-1, 1]"
    )


def test_cross_timeframe_ratios(long_trend_data):
    factory = AlphaFactory(long_trend_data)
    df = factory.add_all_features(
        windows=[288, 864, 2016, 4032, 10080],
        include_extended=True,
        include_macro=False,
    )

    for col in ["CROSS_VOL_RATIO_1D_35D", "CROSS_VOL_RATIO_3D_14D",
                "CROSS_TREND_DIFF_1D_35D", "CROSS_TREND_DIFF_3D_14D",
                "CROSS_VWAP_DIFF_1D_35D"]:
        assert col in df.columns, f"Missing cross-timeframe column: {col}"
        series = df[col].dropna()
        assert not series.empty, f"{col} is all NaN"


def test_add_all_features_extended(flat_line_data):
    """Extended features should add extra columns when include_extended=True."""
    factory = AlphaFactory(flat_line_data)
    df_basic = factory.add_all_features(windows=[10], include_macro=False, include_extended=False)
    n_basic = len(df_basic.columns)

    factory2 = AlphaFactory(flat_line_data.copy())
    df_ext = factory2.add_all_features(windows=[10], include_macro=False, include_extended=True)
    n_ext = len(df_ext.columns)

    assert n_ext > n_basic, (
        f"Extended should have more columns ({n_ext} vs {n_basic})"
    )
    # Extended should add DIST_*, MOM_STOCH_* per window
    assert "DIST_SKEW_10" in df_ext.columns
    assert "MOM_STOCH_K_10" in df_ext.columns

