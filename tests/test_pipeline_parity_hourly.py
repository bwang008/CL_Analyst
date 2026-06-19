"""
Pipeline Parity Tests — Historical Batch vs. Live Incremental (Hourly).

These tests verify that AlphaFactory produces identical feature values
whether fed a static batch all at once (historical training pipeline)
or fed incrementally (simulating live build_live_features) on HOURLY data.

This is the production-relevant parity test — all active strategies
use bar_size="1h".

If these tests break, it means live inference is diverging from the
model's training data — predictions will be silently wrong.
"""

import numpy as np
import pandas as pd
import pytest

from src.features.alpha_factory import AlphaFactory
from src.live_execution.feature_pipeline import build_live_features


# Hourly alpha windows (matches feature_pipeline.py bar_size="1h" branch)
_ALPHA_WINDOWS_1H = [24, 72, 168, 336, 840]

# Hourly macro windows (matches feature_pipeline.py bar_size="1h" branch)
_MACRO_WINDOWS_1H = {"1W": 168, "2W": 336, "1M": 840, "3M": 2160, "6M": 4320}


# ---------------------------------------------------------------------------
# Helper: generate realistic hourly OHLCV data
# ---------------------------------------------------------------------------

def _make_ohlcv_1h(n: int = 2_500, seed: int = 42) -> pd.DataFrame:
    """
    Generate deterministic hourly OHLCV data with realistic dynamics.

    Default 2,500 hourly bars (~104 days) exceeds the 1,680-bar minimum
    needed for second-order volatility features (VOL_ROC_840, VOL_VOLVOL_840)
    and matches the livetest workflow guidance of >=2,200 bars.
    """
    rng = np.random.RandomState(seed)
    close = np.empty(n)
    close[0] = 75.0
    for i in range(1, n):
        close[i] = close[i - 1] + rng.normal(0, 0.3)
        close[i] = max(close[i], 10.0)  # floor

    high = close + rng.uniform(0.05, 0.8, n)
    low = close - rng.uniform(0.05, 0.8, n)
    open_ = close + rng.normal(0, 0.15, n)

    index = pd.date_range("2024-01-01", periods=n, freq="1h")
    return pd.DataFrame(
        {
            "Open": open_,
            "High": high,
            "Low": low,
            "Close": close,
            "Volume": rng.randint(500, 5000, n).astype(float),
        },
        index=index,
    )


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def ohlcv_data_1h():
    """2,500 rows of deterministic hourly OHLCV data.

    Must be >= 1,680 bars so second-order volatility features
    (VOL_ROC_840, VOL_VOLVOL_840) have enough warmup.
    """
    return _make_ohlcv_1h(n=2_500)


@pytest.fixture(scope="module")
def batch_features_1h(ohlcv_data_1h):
    """
    Run the TRAINING-like pipeline on the full hourly batch.

    Replicates the training pipeline steps for hourly data:
    time features, alpha factory (extended), volume log.
    """
    df = ohlcv_data_1h.copy()

    # Step 1: time features
    minutes = df.index.hour * 60 + df.index.minute
    df["Time_Sin"] = np.sin(2 * np.pi * minutes / 1440)
    df["Time_Cos"] = np.cos(2 * np.pi * minutes / 1440)

    # Step 1b: day-of-week (set_07 / hourly models)
    day_of_week = df.index.dayofweek
    df["Time_DayOfWeek_Sin"] = np.sin(2 * np.pi * day_of_week / 5)
    df["Time_DayOfWeek_Cos"] = np.cos(2 * np.pi * day_of_week / 5)

    # Step 2: AlphaFactory (hourly windows, extended features, bars_per_hour=1)
    df = AlphaFactory(df, bars_per_hour=1).add_all_features(
        windows=_ALPHA_WINDOWS_1H,
        include_momentum=True,
        include_macro=True,
        include_extended=True,
        macro_windows=_MACRO_WINDOWS_1H,
    )

    # Step 3: ATR_14
    if "ATR_14" not in df.columns:
        import pandas_ta as ta  # noqa: F401
        df["ATR_14"] = df.ta.atr(length=14)

    # Step 4: Volume_Log
    df["Volume_Log"] = np.log1p(df["Volume"])

    # Clean NaN/inf same way the batch training pipeline does
    df.replace([np.inf, -np.inf], np.nan, inplace=True)
    df.ffill(inplace=True)
    df.bfill(inplace=True)
    df.fillna(0, inplace=True)

    return df


@pytest.fixture(scope="module")
def live_feature_names_1h(batch_features_1h):
    """
    Extract the feature column names produced by the hourly batch pipeline.
    """
    exclude = {"Open", "High", "Low", "Close", "Volume", "DateTime", "log_ret"}
    cols = [c for c in batch_features_1h.columns if c not in exclude]
    return cols


@pytest.fixture(scope="module")
def live_last_row_1h(ohlcv_data_1h, live_feature_names_1h):
    """
    Run the LIVE pipeline (build_live_features) on hourly data.

    Returns a single-row DataFrame (the last row of features).
    """
    return build_live_features(
        ohlcv_data_1h.copy(),
        live_feature_names_1h,
        bar_size="1h",
    )


# =========================================================================
# TESTS
# =========================================================================

class TestHourlyPipelineParity:
    """
    Assert that the batch (historical) and live (incremental) pipelines
    produce identical feature values from the same hourly input data.

    This is the production-critical parity test — all active strategies
    use bar_size="1h".
    """

    def test_batch_vs_live_features_match(self, batch_features_1h, live_last_row_1h, live_feature_names_1h):
        """
        The last row of the batch pipeline must equal the single row
        returned by build_live_features, within floating-point tolerance.
        """
        assert live_last_row_1h is not None, "build_live_features returned None"

        batch_last = batch_features_1h[live_feature_names_1h].iloc[[-1]]
        batch_last = batch_last.reset_index(drop=True)
        live_row = live_last_row_1h.reset_index(drop=True)

        for col in live_feature_names_1h:
            batch_val = float(batch_last[col].iloc[0])
            live_val = float(live_row[col].iloc[0])
            assert np.isclose(batch_val, live_val, atol=1e-6), (
                f"Parity violation in '{col}': batch={batch_val}, live={live_val}"
            )

    def test_feature_column_order_preserved(self, live_last_row_1h, live_feature_names_1h):
        """Column ordering must be identical in both paths."""
        assert live_last_row_1h is not None
        assert list(live_last_row_1h.columns) == live_feature_names_1h

    def test_time_features_parity(self, batch_features_1h, live_last_row_1h):
        """Time_Sin and Time_Cos must match between batch and live."""
        assert live_last_row_1h is not None

        for col in ["Time_Sin", "Time_Cos", "Time_DayOfWeek_Sin", "Time_DayOfWeek_Cos"]:
            if col in live_last_row_1h.columns and col in batch_features_1h.columns:
                batch_val = float(batch_features_1h[col].iloc[-1])
                live_val = float(live_last_row_1h[col].iloc[0])
                assert np.isclose(batch_val, live_val, atol=1e-12), (
                    f"{col} mismatch: batch={batch_val}, live={live_val}"
                )

    def test_volume_log_parity(self, batch_features_1h, live_last_row_1h):
        """Volume_Log transformation must produce identical values."""
        assert live_last_row_1h is not None

        if "Volume_Log" in live_last_row_1h.columns:
            batch_val = float(batch_features_1h["Volume_Log"].iloc[-1])
            live_val = float(live_last_row_1h["Volume_Log"].iloc[0])
            assert np.isclose(batch_val, live_val, atol=1e-12), (
                f"Volume_Log mismatch: batch={batch_val}, live={live_val}"
            )

    def test_hourly_windows_used(self, live_last_row_1h):
        """Verify that hourly window suffixes (_24, _840) appear in features."""
        assert live_last_row_1h is not None
        cols = list(live_last_row_1h.columns)
        # Should have hourly windows, not 5m windows
        assert any("_840" in c for c in cols), "Missing _840 suffix (largest hourly window)"
        assert any("_24" in c for c in cols), "Missing _24 suffix (smallest hourly window)"
        # Should NOT have 5m-only windows
        assert not any("_10080" in c for c in cols), "Found _10080 suffix — 5m windows leaked into 1h pipeline"
        assert not any("_4032" in c for c in cols), "Found _4032 suffix — 5m windows leaked into 1h pipeline"

    def test_no_nan_in_features(self, live_last_row_1h, live_feature_names_1h):
        """All features must be non-NaN (sufficient warmup)."""
        assert live_last_row_1h is not None
        nan_cols = live_last_row_1h.columns[live_last_row_1h.isna().iloc[0]].tolist()
        assert len(nan_cols) == 0, f"NaN in: {nan_cols}"

    def test_no_inf_in_features(self, live_last_row_1h, live_feature_names_1h):
        """No infinite values in the feature row."""
        assert live_last_row_1h is not None
        inf_mask = np.isinf(live_last_row_1h.values.astype(float))
        assert not inf_mask.any(), "Inf detected in feature row"
