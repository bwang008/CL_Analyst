"""
Bad Data Handling — NaN, Infinity, and Extreme Volatility Tests.

These tests inject simulated "bad" market data scenarios that the live
pipeline WILL encounter:
- Zero-volume bars (illiquid after-hours)
- Flash crashes (50%+ intrabar drops)
- Missing timestamps (IBKR data gaps)
- Duplicate timestamps (reconnection artifacts)
- All-NaN columns (sensor failure)

Each test asserts the pipeline handles these gracefully without
crashing or feeding NaN/inf to the model.
"""

import numpy as np
import pandas as pd
import pytest

from src.features.alpha_factory import AlphaFactory
from src.live_execution.live_trader import build_live_features, _ALPHA_WINDOWS


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_ohlcv(n: int = 11_000, seed: int = 42) -> pd.DataFrame:
    """Generate deterministic OHLCV data."""
    rng = np.random.RandomState(seed)
    close = 75.0 + np.cumsum(rng.normal(0, 0.1, n))
    close = np.maximum(close, 10.0)
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


@pytest.fixture
def clean_data():
    """11,000 rows of clean OHLCV data with known feature names."""
    return _make_ohlcv(11_000)


@pytest.fixture
def feature_names(clean_data):
    """Generate the feature names from a clean run."""
    df = clean_data.copy()
    minutes = df.index.hour * 60 + df.index.minute
    df["Time_Sin"] = np.sin(2 * np.pi * minutes / 1440)
    df["Time_Cos"] = np.cos(2 * np.pi * minutes / 1440)
    df = AlphaFactory(df).add_all_features(
        windows=_ALPHA_WINDOWS, include_momentum=True, include_macro=True,
    )
    if "ATR_14" not in df.columns:
        import pandas_ta as ta  # noqa: F401
        df["ATR_14"] = df.ta.atr(length=14)
    df["Volume_Log"] = np.log1p(df["Volume"])
    ohlcv = {"Open", "High", "Low", "Close", "Volume", "DateTime", "log_ret"}
    return [c for c in df.columns if c not in ohlcv]


# =========================================================================
# TESTS
# =========================================================================

class TestZeroVolume:
    """Test handling of zero-volume bars."""

    def test_zero_volume_bar_doesnt_crash(self, clean_data, feature_names):
        """
        A bar with Volume=0 should not produce inf/NaN in the final output.

        This happens in real markets during illiquid after-hours sessions.
        """
        df = clean_data.copy()
        # Inject zero-volume bars
        df.iloc[5000, df.columns.get_loc("Volume")] = 0.0
        df.iloc[5001, df.columns.get_loc("Volume")] = 0.0
        df.iloc[5002, df.columns.get_loc("Volume")] = 0.0

        result = build_live_features(df, feature_names)
        assert result is not None, "Pipeline crashed on zero-volume bars"
        assert not result.isna().any().any(), (
            f"NaN found in output after zero-volume injection: "
            f"{result.columns[result.isna().iloc[0]].tolist()}"
        )
        assert not result.isin([np.inf, -np.inf]).any().any(), (
            "Inf found in output after zero-volume injection"
        )


class TestFlashCrash:
    """Test handling of extreme price movements."""

    def test_flash_crash_spike(self, clean_data, feature_names):
        """
        A 50% intrabar price drop should produce valid (large but finite)
        features, not NaN/inf.

        Scenario: CL flash crashes from $75 to $37.50 in one 5-min bar.
        """
        df = clean_data.copy()
        crash_idx = 5000
        pre_crash_close = df.iloc[crash_idx - 1]["Close"]

        # Simulate flash crash
        df.iloc[crash_idx, df.columns.get_loc("Open")] = pre_crash_close
        df.iloc[crash_idx, df.columns.get_loc("High")] = pre_crash_close
        df.iloc[crash_idx, df.columns.get_loc("Low")] = pre_crash_close * 0.5
        df.iloc[crash_idx, df.columns.get_loc("Close")] = pre_crash_close * 0.5
        df.iloc[crash_idx, df.columns.get_loc("Volume")] = 50000.0

        result = build_live_features(df, feature_names)
        assert result is not None, "Pipeline crashed on flash crash data"
        assert not result.isna().any().any(), "NaN found after flash crash"
        assert not result.isin([np.inf, -np.inf]).any().any(), "Inf found after flash crash"

        # Features should be large but finite
        max_val = result.abs().max(axis=1).iloc[0]
        assert np.isfinite(max_val), f"Non-finite max feature value: {max_val}"


class TestMissingTimestamps:
    """Test handling of gaps in the timestamp series."""

    def test_missing_timestamp_gap(self, clean_data, feature_names):
        """
        A 30-minute gap in 5-min bars (6 missing bars) should not crash
        the feature pipeline.

        This happens during IBKR disconnections or market halts.
        """
        df = clean_data.copy()
        # Remove 6 consecutive bars (30-minute gap)
        gap_start = 5000
        gap_end = 5006
        df = pd.concat([df.iloc[:gap_start], df.iloc[gap_end:]])

        result = build_live_features(df, feature_names)
        assert result is not None, "Pipeline crashed on timestamp gap"
        assert not result.isna().any().any(), "NaN found after timestamp gap"


class TestDuplicateTimestamps:
    """Test handling of duplicate timestamps (reconnection artifacts)."""

    def test_duplicate_timestamps_handled(self, clean_data, feature_names):
        """
        Duplicate timestamps should not cause index errors or crash.

        This can happen when IBKR reconnects and resends recent bars.
        """
        df = clean_data.copy()
        # Duplicate a block of rows
        dup_rows = df.iloc[5000:5010].copy()
        df = pd.concat([df, dup_rows])
        df = df.sort_index()

        result = build_live_features(df, feature_names)
        assert result is not None, "Pipeline crashed on duplicate timestamps"
        assert not result.isna().any().any(), "NaN found after duplicate timestamps"


class TestNegativePrice:
    """Test handling of invalid negative prices."""

    def test_negative_close_produces_finite_output(self, clean_data, feature_names):
        """
        Negative OHLC prices (corrupt data) should not crash the pipeline.

        While we shouldn't normally see negative CL prices, the pipeline
        must degrade gracefully rather than crash mid-trade.
        """
        df = clean_data.copy()
        df.iloc[5000, df.columns.get_loc("Close")] = -1.0
        df.iloc[5000, df.columns.get_loc("Low")] = -1.0

        # Pipeline should either handle it or return None — not crash
        try:
            result = build_live_features(df, feature_names)
            if result is not None:
                # If it returns something, it must not contain NaN or inf
                assert not result.isna().any().any(), "NaN output from negative price"
                assert not result.isin([np.inf, -np.inf]).any().any(), "Inf from negative price"
        except (ValueError, ArithmeticError):
            # Acceptable: raising a clear error is better than silent corruption
            pass


class TestAllNaNColumn:
    """Test handling of entirely NaN columns."""

    def test_all_nan_volume_handled(self, clean_data, feature_names):
        """
        An entirely NaN Volume column should be caught.

        In practice this shouldn't happen, but if the IBKR feed drops
        volume data, we need to handle it without NaN reaching the model.
        """
        df = clean_data.copy()
        df["Volume"] = np.nan

        result = build_live_features(df, feature_names)
        if result is not None:
            # build_live_features does fillna(0), so this should be handled
            assert not result.isna().any().any(), "NaN reached model from all-NaN Volume"


class TestBuildLiveFeaturesNaNGuarantee:
    """Ultimate safety net: build_live_features must never return NaN."""

    def test_build_live_features_never_returns_nan_to_model(self, clean_data, feature_names):
        """
        The FINAL output of build_live_features must have exactly zero NaN.

        This is the last line of defense — even if intermediate steps
        produce NaN, the final fillna(0) and warning must catch them.
        """
        result = build_live_features(clean_data.copy(), feature_names)
        assert result is not None
        nan_count = result.isna().sum().sum()
        assert nan_count == 0, (
            f"build_live_features returned {nan_count} NaN values — "
            f"model will receive corrupt input"
        )

        inf_count = result.isin([np.inf, -np.inf]).sum().sum()
        assert inf_count == 0, (
            f"build_live_features returned {inf_count} inf values — "
            f"model will receive corrupt input"
        )
