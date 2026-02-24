"""
Pipeline Parity Tests — Historical Batch vs. Live Incremental.

These tests verify that AlphaFactory produces identical feature values
whether fed a static batch all at once (historical training pipeline)
or fed incrementally (simulating live build_live_features).

If these tests break, it means live inference is diverging from the
model's training data — predictions will be silently wrong.
"""

import numpy as np
import pandas as pd
import pytest

from src.features.alpha_factory import AlphaFactory
from src.live_execution.live_trader import build_live_features, _ALPHA_WINDOWS


# ---------------------------------------------------------------------------
# Helper: generate realistic OHLCV data
# ---------------------------------------------------------------------------

def _make_ohlcv(n: int = 11_000, seed: int = 42) -> pd.DataFrame:
    """
    Generate deterministic OHLCV data with realistic dynamics.

    Uses the same approach as conftest.generate_ohlcv but keeps it
    self-contained so this test file has no external fixture dependency.
    """
    rng = np.random.RandomState(seed)
    close = np.empty(n)
    close[0] = 75.0
    for i in range(1, n):
        close[i] = close[i - 1] + rng.normal(0, 0.1)
        close[i] = max(close[i], 10.0)  # floor

    high = close + rng.uniform(0.05, 0.5, n)
    low = close - rng.uniform(0.05, 0.5, n)
    open_ = close + rng.normal(0, 0.05, n)

    index = pd.date_range("2024-01-01", periods=n, freq="5min")
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
# Shared fixture: feature names from the model
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def ohlcv_data():
    """11,000 rows of deterministic OHLCV data."""
    return _make_ohlcv(n=11_000)


@pytest.fixture(scope="module")
def batch_features(ohlcv_data):
    """
    Run the TRAINING-like pipeline on the full batch.

    Replicates process_set_05 steps 2–4 (time features, alpha factory,
    volume log) without targets/cleanup.
    """
    df = ohlcv_data.copy()

    # Step 1: time features
    minutes = df.index.hour * 60 + df.index.minute
    df["Time_Sin"] = np.sin(2 * np.pi * minutes / 1440)
    df["Time_Cos"] = np.cos(2 * np.pi * minutes / 1440)

    # Step 2: AlphaFactory (same windows as training & live)
    df = AlphaFactory(df).add_all_features(
        windows=_ALPHA_WINDOWS,
        include_momentum=True,
        include_macro=True,
    )

    # Step 3: ATR_14 (created by add_triple_barrier_target in training)
    if "ATR_14" not in df.columns:
        import pandas_ta as ta  # noqa: F401
        df["ATR_14"] = df.ta.atr(length=14)

    # Step 4: Volume_Log
    df["Volume_Log"] = np.log1p(df["Volume"])

    # Clean NaN/inf same way live pipeline does
    df.replace([np.inf, -np.inf], np.nan, inplace=True)
    df.ffill(inplace=True)
    df.bfill(inplace=True)
    df.fillna(0, inplace=True)

    return df


@pytest.fixture(scope="module")
def live_feature_names(batch_features):
    """
    Extract the feature column names that would be used by the model.

    Uses all non-OHLCV, non-DateTime columns from the batch features.
    Matches the columns produced by build_live_features.
    """
    exclude = {"Open", "High", "Low", "Close", "Volume", "DateTime", "log_ret"}
    cols = [c for c in batch_features.columns if c not in exclude]
    return cols


@pytest.fixture(scope="module")
def live_last_row(ohlcv_data, live_feature_names):
    """
    Run the LIVE pipeline (build_live_features) on the same data.

    Returns a single-row DataFrame (the last row of features).
    """
    return build_live_features(ohlcv_data.copy(), live_feature_names)


# =========================================================================
# TESTS
# =========================================================================

class TestPipelineParity:
    """
    Assert that the batch (historical) and live (incremental) pipelines
    produce identical feature values from the same input data.
    """

    def test_batch_vs_live_features_match(self, batch_features, live_last_row, live_feature_names):
        """
        The last row of the batch pipeline must equal the single row
        returned by build_live_features, within floating-point tolerance.
        """
        assert live_last_row is not None, "build_live_features returned None"

        batch_last = batch_features[live_feature_names].iloc[[-1]]
        batch_last = batch_last.reset_index(drop=True)
        live_row = live_last_row.reset_index(drop=True)

        for col in live_feature_names:
            batch_val = float(batch_last[col].iloc[0])
            live_val = float(live_row[col].iloc[0])
            assert np.isclose(batch_val, live_val, atol=1e-10), (
                f"Parity violation in '{col}': batch={batch_val}, live={live_val}"
            )

    def test_feature_column_order_preserved(self, live_last_row, live_feature_names):
        """Column ordering must be identical in both paths."""
        assert live_last_row is not None
        assert list(live_last_row.columns) == live_feature_names

    def test_time_features_parity(self, batch_features, live_last_row):
        """Time_Sin and Time_Cos must match between batch and live."""
        assert live_last_row is not None

        for col in ["Time_Sin", "Time_Cos"]:
            if col in live_last_row.columns and col in batch_features.columns:
                batch_val = float(batch_features[col].iloc[-1])
                live_val = float(live_last_row[col].iloc[0])
                assert np.isclose(batch_val, live_val, atol=1e-12), (
                    f"{col} mismatch: batch={batch_val}, live={live_val}"
                )

    def test_volume_log_parity(self, batch_features, live_last_row):
        """Volume_Log transformation must produce identical values."""
        assert live_last_row is not None

        if "Volume_Log" in live_last_row.columns:
            batch_val = float(batch_features["Volume_Log"].iloc[-1])
            live_val = float(live_last_row["Volume_Log"].iloc[0])
            assert np.isclose(batch_val, live_val, atol=1e-12), (
                f"Volume_Log mismatch: batch={batch_val}, live={live_val}"
            )
