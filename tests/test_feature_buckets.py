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
    """Realistic subset of production feature columns."""
    return [
        "ATR_14", "Volume_Log", "log_ret",           # core
        "Time_Sin", "Time_Cos", "Time_DayOfWeek_Sin",  # time (real column names)
        "VOL_BB_UPPER_288", "VOL_ATR_RATIO_864",      # volatility
        "MOM_RSI_14", "MOM_MACD_Signal",               # momentum
        "TREND_SLOPE_288", "TREND_R2_864",             # trend
        "LIQ_CORWIN_288", "LIQ_KYLE_864",             # microstructure
        "STRUC_BODY_RATIO", "VOLFLOW_OBV_SLOPE_288",  # structure
        "DIST_SKEW_288", "DIST_KURTOSIS_864",          # distribution
        "EXHAUST_CUM_RET_288",                         # exhaustion
        "EXHDIV_SLOPE_DIVERGE_288",                    # divergence
        "TS_VOL_PARK_DIFF_24v840",                     # term_structure
        "CURVE_SPREAD_PCT", "CURVE_SPREAD_SEASONAL_Z",  # curve
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
    # REAL production column names (the old Hour_sin/DayOfWeek_cos prefixes
    # were fictional — no dataset ever emitted them, silently making the time
    # bucket always-on in bucket mode).
    assert classify_feature("Time_Sin") == "time"
    assert classify_feature("Time_Cos") == "time"
    assert classify_feature("Time_DayOfWeek_Sin") == "time"
    assert classify_feature("Time_DayOfWeek_Cos") == "time"
    assert classify_feature("Time_Month_Sin") == "time"
    assert classify_feature("Time_Month_Cos") == "time"
    # The fictional legacy prefixes must NOT classify anymore
    assert classify_feature("Hour_sin") is None
    assert classify_feature("DayOfWeek_cos") is None


def test_classify_curve():
    assert classify_feature("CURVE_SPREAD_PCT") == "curve"
    assert classify_feature("CURVE_CONTANGO_SIGN") == "curve"
    assert classify_feature("CURVE_SPREAD_SEASONAL_Z") == "curve"
    assert classify_feature("CURVE_BARS_SINCE_ROLL") == "curve"
    # CURVE_ is strictly separate from the TS_ term-structure bucket
    assert classify_feature("TS_VOL_PARK_DIFF_24v840") == "term_structure"


def test_time_and_curve_buckets_toggle(sample_columns):
    """time and curve are genuinely toggleable: excluded when inactive,
    included when active (they must never be silently always-on)."""
    core_only = get_active_features(sample_columns, {"core"})
    assert "Time_Sin" not in core_only
    assert "Time_DayOfWeek_Sin" not in core_only
    assert "CURVE_SPREAD_PCT" not in core_only
    assert "CURVE_SPREAD_SEASONAL_Z" not in core_only

    with_time = get_active_features(sample_columns, {"core", "time"})
    assert "Time_Sin" in with_time
    assert "Time_DayOfWeek_Sin" in with_time
    assert "CURVE_SPREAD_PCT" not in with_time

    with_curve = get_active_features(sample_columns, {"core", "curve"})
    assert "CURVE_SPREAD_PCT" in with_curve
    assert "CURVE_SPREAD_SEASONAL_Z" in with_curve
    assert "Time_Sin" not in with_curve


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
