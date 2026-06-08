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
    df = factory.add_return_distribution_cluster(windows=[10])

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
    assert "DIST_SKEW_12" in df_ext.columns
    assert "MOM_STOCH_K_10" in df_ext.columns


def test_exhaustion_divergence_columns_exist(long_trend_data):
    """Exhaustion divergence cluster should add EXHDIV_* columns."""
    factory = AlphaFactory(long_trend_data)
    # Need momentum first for RSI
    factory.add_momentum_cluster()
    df = factory.add_exhaustion_divergence_cluster(window=20)

    for col in [
        "EXHDIV_SLOPE_DIVERGE_20",
        "EXHDIV_PEAK_OFFSET_20",
        "EXHDIV_EFFORT_REWARD_20",
    ]:
        assert col in df.columns, f"Missing column: {col}"
        series = df[col].dropna()
        assert not series.empty, f"{col} is all NaN"
        assert not np.isinf(series).any(), f"{col} has inf values"


def test_effort_reward_non_negative(long_trend_data):
    """Effort-reward ratio should always be >= 0 (volume and range are positive)."""
    factory = AlphaFactory(long_trend_data)
    factory.add_momentum_cluster()
    df = factory.add_exhaustion_divergence_cluster(window=20)

    er = df["EXHDIV_EFFORT_REWARD_20"].dropna()
    assert not er.empty, "EXHDIV_EFFORT_REWARD_20 is all NaN"
    assert (er >= 0).all(), "EXHDIV_EFFORT_REWARD_20 contains negative values"


def test_exhaustion_divergence_no_lookahead(long_trend_data):
    """Features at row i must not change when future data (row i+1 onward) changes."""
    test_row = 500  # well past warmup

    # Run 1: original data
    factory1 = AlphaFactory(long_trend_data.copy())
    factory1.add_momentum_cluster()
    df1 = factory1.add_exhaustion_divergence_cluster(window=20)

    # Run 2: scramble all data after test_row
    modified = long_trend_data.copy()
    np.random.seed(42)
    n_future = len(modified) - test_row - 1
    modified.iloc[test_row + 1:, modified.columns.get_loc("Close")] = (
        np.random.uniform(50, 200, n_future)
    )
    modified.iloc[test_row + 1:, modified.columns.get_loc("High")] = (
        modified.iloc[test_row + 1:]["Close"] + 1.0
    )
    modified.iloc[test_row + 1:, modified.columns.get_loc("Low")] = (
        modified.iloc[test_row + 1:]["Close"] - 1.0
    )
    factory2 = AlphaFactory(modified)
    factory2.add_momentum_cluster()
    df2 = factory2.add_exhaustion_divergence_cluster(window=20)

    for col in [
        "EXHDIV_SLOPE_DIVERGE_20",
        "EXHDIV_PEAK_OFFSET_20",
        "EXHDIV_EFFORT_REWARD_20",
    ]:
        v1 = df1[col].iloc[test_row]
        v2 = df2[col].iloc[test_row]
        if pd.notna(v1) and pd.notna(v2):
            assert np.isclose(v1, v2, rtol=1e-6), (
                f"Lookahead detected in {col}: {v1} vs {v2}"
            )


def test_term_structure_shapes_columns_exist(long_trend_data):
    """Term structure features should be created for each eligible family."""
    factory = AlphaFactory(long_trend_data)
    df = factory.add_all_features(
        windows=[10, 20, 50],
        include_macro=False,
        include_extended=True,
        include_term_structure=True,
    )

    # BOUNDED family (VOL_PARK): should have DIFF, RATIO, LOG_RATIO, INVERT, ZSCORE
    for transform in ["DIFF", "RATIO", "LOG_RATIO", "INVERT", "ZSCORE"]:
        for fast in [10, 20]:
            col = f"TS_VOL_PARK_{transform}_{fast}v50"
            assert col in df.columns, f"Missing bounded TS column: {col}"

    # SIGNED family (LR_SLOPE): should have DIFF, SIGN_AGREE, REGIME_CROSS, ZSCORE
    for transform in ["DIFF", "SIGN_AGREE", "REGIME_CROSS", "ZSCORE"]:
        for fast in [10, 20]:
            col = f"TS_LR_SLOPE_{transform}_{fast}v50"
            assert col in df.columns, f"Missing signed TS column: {col}"

    # SIGNED families must NOT have RATIO or INVERT columns
    for signed_name in ["LR_SLOPE", "VWAP_DIST", "CMF"]:
        for bad_transform in ["RATIO", "INVERT"]:
            for fast in [10, 20]:
                col = f"TS_{signed_name}_{bad_transform}_{fast}v50"
                assert col not in df.columns, (
                    f"Signed family should NOT have {bad_transform}: {col}"
                )

    # Total TS columns:
    #   6 bounded × 5 transforms (DIFF, RATIO, LOG_RATIO, INVERT, ZSCORE) × 2 fast = 60
    #   3 signed  × 4 transforms (DIFF, SIGN_AGREE, REGIME_CROSS, ZSCORE) × 2 fast = 24
    #   Total = 84
    ts_cols = [c for c in df.columns if c.startswith("TS_")]
    assert len(ts_cols) == 84, (
        f"Expected 84 TS columns, got {len(ts_cols)}: {sorted(ts_cols)}"
    )


def test_term_structure_shapes_math_correctness(long_trend_data):
    """Verify Diff, Ratio, and Inversion math for bounded indicators."""
    factory = AlphaFactory(long_trend_data)
    df = factory.add_all_features(
        windows=[10, 20, 50],
        include_macro=False,
        include_extended=True,
        include_term_structure=True,
    )

    # Check DIFF = fast - slow (exactly)
    diff_col = "TS_VOL_PARK_DIFF_10v50"
    expected_diff = df["VOL_PARK_10"] - df["VOL_PARK_50"]
    pd.testing.assert_series_equal(
        df[diff_col], expected_diff, check_names=False,
    )

    # Check RATIO: positive for non-negative bounded indicators
    ratio_col = "TS_VOL_PARK_RATIO_10v50"
    ratio_vals = df[ratio_col].dropna()
    assert (ratio_vals >= 0).all(), "VOL_PARK ratio should be non-negative"

    # Check INVERT is binary {0, 1}
    invert_col = "TS_VOL_PARK_INVERT_10v50"
    invert_vals = df[invert_col].dropna()
    assert set(invert_vals.unique()).issubset({0, 1}), (
        f"INVERT should be binary, got {invert_vals.unique()}"
    )

    # Verify INVERT matches the sign of DIFF
    valid_mask = df[diff_col].notna() & df[invert_col].notna()
    diff_positive = df.loc[valid_mask, diff_col] > 0
    invert_one = df.loc[valid_mask, invert_col] == 1
    assert (diff_positive == invert_one).all(), (
        "INVERT=1 should align with DIFF>0"
    )


def test_term_structure_no_inf(long_trend_data):
    """Term structure features should have no inf values after inf replacement."""
    factory = AlphaFactory(long_trend_data)
    df = factory.add_all_features(
        windows=[10, 20, 50],
        include_macro=False,
        include_extended=True,
        include_term_structure=True,
    )

    ts_cols = [c for c in df.columns if c.startswith("TS_")]
    for col in ts_cols:
        series = df[col].dropna()
        assert not np.isinf(series).any(), f"{col} contains inf values"


def test_term_structure_signed_regime_features(long_trend_data):
    """Signed indicators should produce Sign_Agreement and Regime_Cross."""
    factory = AlphaFactory(long_trend_data)
    factory.add_trend_cluster(window=10)
    factory.add_trend_cluster(window=50)
    df = factory.add_term_structure_shapes()

    # Sign Agreement should be binary {0, 1}
    agree_col = "TS_LR_SLOPE_SIGN_AGREE_10v50"
    assert agree_col in df.columns, f"Missing: {agree_col}"
    agree_vals = df[agree_col].dropna()
    assert set(agree_vals.unique()).issubset({0, 1}), (
        f"SIGN_AGREE should be binary, got {agree_vals.unique()}"
    )

    # Regime Cross should be binary {0, 1}
    cross_col = "TS_LR_SLOPE_REGIME_CROSS_10v50"
    assert cross_col in df.columns, f"Missing: {cross_col}"
    cross_vals = df[cross_col].dropna()
    assert set(cross_vals.unique()).issubset({0, 1}), (
        f"REGIME_CROSS should be binary, got {cross_vals.unique()}"
    )

    # Sign Agreement and Regime Cross should be complementary:
    # SIGN_AGREE=1 when both same sign, REGIME_CROSS=1 when opposite signs.
    # They should never BOTH be 1 at the same time.
    valid = df[agree_col].notna() & df[cross_col].notna()
    both_one = (df.loc[valid, agree_col] == 1) & (df.loc[valid, cross_col] == 1)
    assert not both_one.any(), (
        "SIGN_AGREE and REGIME_CROSS should never both be 1"
    )

    # Verify math: Sign Agreement = (Fast>0) == (Slow>0)
    fast = df["TREND_LR_SLOPE_10"]
    slow = df["TREND_LR_SLOPE_50"]
    expected_agree = ((fast > 0) == (slow > 0)).astype(int)
    pd.testing.assert_series_equal(
        df[agree_col], expected_agree, check_names=False,
    )

    # No RATIO should exist for signed indicators
    assert f"TS_LR_SLOPE_RATIO_10v50" not in df.columns, (
        "Signed indicator should NOT have RATIO column"
    )
