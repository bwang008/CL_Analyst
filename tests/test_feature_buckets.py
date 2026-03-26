"""Tests for src/features/feature_buckets.py — Feature Bucket Architecture."""

import pytest
from src.features.feature_buckets import (
    FEATURE_BUCKETS,
    TOGGLEABLE_BUCKETS,
    BUCKET_MIN_TRIALS,
    classify_feature,
    get_active_features,
    get_bucket_summary,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_columns():
    """Realistic subset of set_12 feature columns."""
    return [
        "ATR_14", "Volume_Log", "log_ret",           # core
        "Hour_sin", "Hour_cos", "DayOfWeek_sin",      # time
        "VOL_BB_UPPER_288", "VOL_ATR_RATIO_864",      # volatility
        "MOM_RSI_14", "MOM_MACD_Signal",               # momentum
        "TREND_SLOPE_288", "TREND_R2_864",             # trend
        "LIQ_CORWIN_288", "LIQ_KYLE_864",             # microstructure
        "STRUC_BODY_RATIO", "VOLFLOW_OBV_SLOPE_288",  # structure
        "DIST_SKEW_288", "DIST_KURTOSIS_864",          # distribution
        "EXHAUST_CUM_RET_288",                         # exhaustion
        "EXHDIV_SLOPE_DIVERGE_288",                    # divergence
        "MACRO_MEAN_REV_3D",                           # macro_tech
        "VIX_change_1D", "COT_MM_net_pct_14D",        # macro_external
    ]


# ---------------------------------------------------------------------------
# Classification tests
# ---------------------------------------------------------------------------

def test_classify_core():
    assert classify_feature("ATR_14") == "core"
    assert classify_feature("Volume_Log") == "core"
    assert classify_feature("log_ret") == "core"


def test_classify_time():
    assert classify_feature("Hour_sin") == "time"
    assert classify_feature("DayOfWeek_cos") == "time"


def test_classify_indicators():
    assert classify_feature("VOL_BB_UPPER_288") == "volatility"
    assert classify_feature("MOM_RSI_14") == "momentum"
    assert classify_feature("TREND_SLOPE_288") == "trend"
    assert classify_feature("LIQ_CORWIN_288") == "microstructure"


def test_classify_divergence():
    assert classify_feature("EXHDIV_SLOPE_DIVERGE_288") == "divergence"
    assert classify_feature("EXHDIV_PEAK_OFFSET_864") == "divergence"
    assert classify_feature("EXHDIV_EFFORT_REWARD_2016") == "divergence"


def test_classify_macro():
    assert classify_feature("MACRO_MEAN_REV_3D") == "macro_tech"
    assert classify_feature("VIX_change_1D") == "macro_external"
    assert classify_feature("COT_MM_net_pct_14D") == "macro_external"


def test_classify_unknown():
    assert classify_feature("TOTALLY_NEW_FEATURE") is None


# ---------------------------------------------------------------------------
# Active features filtering
# ---------------------------------------------------------------------------

def test_core_only(sample_columns):
    """When only core is active, should return core + unclassified."""
    result = get_active_features(sample_columns, {"core"})
    assert "ATR_14" in result
    assert "Volume_Log" in result
    assert "log_ret" in result
    # Non-core should be excluded
    assert "MOM_RSI_14" not in result
    assert "VOL_BB_UPPER_288" not in result


def test_core_plus_momentum(sample_columns):
    result = get_active_features(sample_columns, {"core", "momentum"})
    assert "ATR_14" in result
    assert "MOM_RSI_14" in result
    assert "MOM_MACD_Signal" in result
    # Other buckets excluded
    assert "VOL_BB_UPPER_288" not in result


def test_all_buckets_returns_all(sample_columns):
    all_buckets = set(FEATURE_BUCKETS.keys())
    result = get_active_features(sample_columns, all_buckets)
    assert result == sample_columns


def test_unknown_features_always_included():
    """Unclassified features should never be dropped."""
    cols = ["ATR_14", "BRAND_NEW_SIGNAL_42"]
    result = get_active_features(cols, {"core"})
    assert "ATR_14" in result
    assert "BRAND_NEW_SIGNAL_42" in result  # unclassified → included


# ---------------------------------------------------------------------------
# Bucket summary
# ---------------------------------------------------------------------------

def test_bucket_summary(sample_columns):
    summary = get_bucket_summary(sample_columns)
    assert summary["core"] == 3
    assert summary["momentum"] == 2
    assert summary["divergence"] == 1


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

def test_toggleable_excludes_core():
    assert "core" not in TOGGLEABLE_BUCKETS


def test_min_trials_is_150():
    assert BUCKET_MIN_TRIALS == 150
