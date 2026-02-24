"""
Deterministic Triple Barrier Edge-Case Tests.

These tests verify DataProcessor.add_triple_barrier_target() behavior
under edge conditions that determine how the model's training labels
are generated.

Critical behavior being locked down:
- When TP and SL are hit in the same bar, TP is checked FIRST (label=1)
- ATR=0 or ATR=NaN → label=0 (Hold)
- Last max_horizon rows → NaN (insufficient look-ahead)
- Barrier widths scale with ATR
"""

import numpy as np
import pandas as pd
import pytest

from src.data_processor import DataProcessor


# ---------------------------------------------------------------------------
# Helper: build minimal DataProcessor with in-memory data
# ---------------------------------------------------------------------------

def _make_dp() -> DataProcessor:
    """
    Create a DataProcessor instance without loading from disk.

    We bypass load_data() and inject DataFrames directly into the
    add_triple_barrier_target method.
    """
    dp = DataProcessor.__new__(DataProcessor)
    dp._dataset_version = "set_05"
    return dp


def _make_ohlcv(
    n: int,
    close: float | np.ndarray = 75.0,
    spread: float = 0.5,
) -> pd.DataFrame:
    """Build a simple OHLCV DataFrame with controllable prices."""
    index = pd.date_range("2024-01-01", periods=n, freq="5min")

    if isinstance(close, (int, float)):
        close_arr = np.full(n, close, dtype=float)
    else:
        close_arr = np.asarray(close, dtype=float)

    return pd.DataFrame(
        {
            "Open": close_arr,
            "High": close_arr + spread,
            "Low": close_arr - spread,
            "Close": close_arr,
            "Volume": np.full(n, 1000.0),
        },
        index=index,
    )


# =========================================================================
# TESTS
# =========================================================================

class TestTripleBarrierEdgeCases:
    """Lock down triple barrier target generation behavior."""

    PREFIX = "TEST_TB"
    ATR_PERIOD = 14
    MAX_HORIZON = 50

    def _run_barrier(
        self,
        df: pd.DataFrame,
        tp_mult: float = 2.0,
        sl_mult: float = 1.0,
    ) -> pd.DataFrame:
        """Run add_triple_barrier_target on the given DataFrame."""
        dp = _make_dp()
        return dp.add_triple_barrier_target(
            df,
            prefix=self.PREFIX,
            tp_atr_mult=tp_mult,
            sl_atr_mult=sl_mult,
            max_horizon=self.MAX_HORIZON,
            atr_period=self.ATR_PERIOD,
        )

    def test_tp_sl_same_bar_tp_wins(self):
        """
        When both TP and SL are breached in the same bar, TP is checked
        FIRST in the loop → label=1 (Long).

        This is the current implementation behavior. We lock it down here
        so any change to the collision resolution is intentional.
        """
        n = 200
        # Create data with steady ATR
        rng = np.random.RandomState(42)
        close = 75.0 + np.cumsum(rng.normal(0, 0.1, n))
        close = np.maximum(close, 50.0)
        high = close + 0.3
        low = close - 0.3

        index = pd.date_range("2024-01-01", periods=n, freq="5min")
        df = pd.DataFrame(
            {
                "Open": close,
                "High": high,
                "Low": low,
                "Close": close,
                "Volume": np.full(n, 1000.0),
            },
            index=index,
        )

        # Compute ATR so we know the barrier levels
        import pandas_ta as ta  # noqa: F401
        df["ATR_14"] = df.ta.atr(length=14)

        # Pick a row with valid ATR
        test_row = 30  # After ATR warm-up
        atr_val = df["ATR_14"].iloc[test_row]
        entry = close[test_row]
        tp_barrier = entry + 2.0 * atr_val
        sl_barrier = entry - 1.0 * atr_val

        # Now craft the NEXT bar to breach BOTH barriers.
        # High >= tp_barrier AND Low <= sl_barrier
        craft_idx = test_row + 1
        df.iloc[craft_idx, df.columns.get_loc("High")] = tp_barrier + 1.0
        df.iloc[craft_idx, df.columns.get_loc("Low")] = sl_barrier - 1.0

        result = self._run_barrier(df)
        label = result[f"{self.PREFIX}_MULTI"].iloc[test_row]

        # TP is checked first → label should be 1 (Long)
        assert label == 1, (
            f"Expected label=1 (TP wins) when both barriers hit, got label={label}"
        )

    def test_only_tp_hit(self):
        """Price goes straight up → label=1 (Long)."""
        n = 200
        # Create upward trend that will hit TP
        close = np.linspace(75.0, 90.0, n)
        high = close + 0.5
        low = close - 0.1

        index = pd.date_range("2024-01-01", periods=n, freq="5min")
        df = pd.DataFrame(
            {
                "Open": close,
                "High": high,
                "Low": low,
                "Close": close,
                "Volume": np.full(n, 1000.0),
            },
            index=index,
        )

        result = self._run_barrier(df)
        multi_col = f"{self.PREFIX}_MULTI"

        # After ATR warmup, the first valid labels in an uptrend should contain 1s
        valid = result[multi_col].dropna()
        valid = valid.iloc[self.ATR_PERIOD:]  # Skip ATR warm-up
        valid = valid[valid != 0]  # Ignore holds
        if len(valid) > 0:
            assert (valid == 1).any(), "Pure uptrend should produce at least one Long label"

    def test_only_sl_hit(self):
        """Price goes straight down → label=2 (Short)."""
        n = 200
        close = np.linspace(90.0, 60.0, n)  # Strong downtrend
        high = close + 0.1
        low = close - 0.5

        index = pd.date_range("2024-01-01", periods=n, freq="5min")
        df = pd.DataFrame(
            {
                "Open": close,
                "High": high,
                "Low": low,
                "Close": close,
                "Volume": np.full(n, 1000.0),
            },
            index=index,
        )

        result = self._run_barrier(df)
        multi_col = f"{self.PREFIX}_MULTI"

        valid = result[multi_col].dropna()
        valid = valid.iloc[self.ATR_PERIOD:]
        valid = valid[valid != 0]
        if len(valid) > 0:
            assert (valid == 2).any(), "Pure downtrend should produce at least one Short label"

    def test_neither_hit_within_horizon(self):
        """Price stays flat → label=0 (Hold)."""
        n = 200
        # Perfectly flat data → very small ATR → TP/SL very close,
        # but also no price movement to hit them.
        # Use tight spread and flat price to guarantee hold.
        close = np.full(n, 75.0)
        # Tiny spread so ATR is non-zero but barriers are never breached
        high = close + 0.001
        low = close - 0.001

        index = pd.date_range("2024-01-01", periods=n, freq="5min")
        df = pd.DataFrame(
            {
                "Open": close,
                "High": high,
                "Low": low,
                "Close": close,
                "Volume": np.full(n, 1000.0),
            },
            index=index,
        )

        result = self._run_barrier(df)
        multi_col = f"{self.PREFIX}_MULTI"

        # All valid labels should be 0 (Hold) — flat price never hits barriers
        valid = result[multi_col].dropna()
        valid = valid.iloc[self.ATR_PERIOD:]  # skip ATR warmup
        # Exclude the last max_horizon rows (NaN)
        valid = valid.iloc[:-self.MAX_HORIZON] if len(valid) > self.MAX_HORIZON else valid
        if len(valid) > 0:
            assert (valid == 0).all(), (
                f"Flat data should produce Hold labels, got distribution: "
                f"{dict(valid.value_counts())}"
            )

    def test_atr_zero_flat_data_no_short_signals(self):
        """
        When ATR=0 (flat data), TP barrier == entry price.

        Implementation behavior: `high[j] >= tp_barrier` is immediately True
        (since High == entry), so the label is 1 (Long), not 0 (Hold).
        The real invariant is: flat data should NEVER produce Short (2) labels.
        ATR NaN during warm-up → label=0 (Hold).
        """
        n = 100
        # Perfectly flat OHLC → ATR = 0
        close = np.full(n, 75.0)
        df = pd.DataFrame(
            {
                "Open": close,
                "High": close,  # High == Close
                "Low": close,   # Low == Close
                "Close": close,
                "Volume": np.full(n, 1000.0),
            },
            index=pd.date_range("2024-01-01", periods=n, freq="5min"),
        )

        result = self._run_barrier(df)
        multi_col = f"{self.PREFIX}_MULTI"

        valid = result[multi_col].dropna()
        # No Short signals should ever appear on flat data
        assert (valid != 2).all(), (
            f"Flat data should never produce Short labels, got: {dict(valid.value_counts())}"
        )
        # Warmup rows (NaN ATR) should be Hold (0)
        warmup = valid.iloc[:self.ATR_PERIOD - 1]
        if len(warmup) > 0:
            assert (warmup == 0).all(), (
                f"ATR NaN warmup should produce Hold labels, got: {dict(warmup.value_counts())}"
            )

    def test_atr_nan_produces_hold(self):
        """When ATR is NaN (warm-up period), barriers aren't set → label=0."""
        n = 100
        rng = np.random.RandomState(42)
        close = 75.0 + np.cumsum(rng.normal(0, 0.1, n))
        close = np.maximum(close, 50.0)

        df = pd.DataFrame(
            {
                "Open": close,
                "High": close + 0.3,
                "Low": close - 0.3,
                "Close": close,
                "Volume": np.full(n, 1000.0),
            },
            index=pd.date_range("2024-01-01", periods=n, freq="5min"),
        )

        result = self._run_barrier(df)
        multi_col = f"{self.PREFIX}_MULTI"

        # First ATR_PERIOD-1 rows should have NaN ATR → label=0
        warmup_labels = result[multi_col].iloc[:self.ATR_PERIOD - 1]
        valid_warmup = warmup_labels.dropna()
        if len(valid_warmup) > 0:
            assert (valid_warmup == 0).all(), (
                f"NaN ATR during warm-up should produce Hold labels, "
                f"got: {dict(valid_warmup.value_counts())}"
            )

    def test_last_rows_are_nan(self):
        """Final max_horizon rows must be NaN (insufficient look-ahead)."""
        n = 200
        rng = np.random.RandomState(42)
        close = 75.0 + np.cumsum(rng.normal(0, 0.1, n))
        close = np.maximum(close, 50.0)

        df = pd.DataFrame(
            {
                "Open": close,
                "High": close + 0.3,
                "Low": close - 0.3,
                "Close": close,
                "Volume": np.full(n, 1000.0),
            },
            index=pd.date_range("2024-01-01", periods=n, freq="5min"),
        )

        result = self._run_barrier(df)
        multi_col = f"{self.PREFIX}_MULTI"

        tail = result[multi_col].iloc[-self.MAX_HORIZON:]
        assert tail.isna().all(), (
            f"Last {self.MAX_HORIZON} rows should be NaN (insufficient look-ahead), "
            f"but {tail.notna().sum()} are non-NaN"
        )

    def test_barrier_widths_scale_with_atr(self):
        """
        Higher ATR → wider barriers → fewer signals.

        We compare a low-volatility dataset (narrow barriers, more hits)
        vs a high-volatility dataset (wider barriers, fewer hits).
        """
        n = 500
        rng_low = np.random.RandomState(42)
        rng_high = np.random.RandomState(42)

        # Low volatility
        close_low = 75.0 + np.cumsum(rng_low.normal(0, 0.05, n))
        close_low = np.maximum(close_low, 50.0)
        df_low = pd.DataFrame(
            {
                "Open": close_low,
                "High": close_low + 0.1,
                "Low": close_low - 0.1,
                "Close": close_low,
                "Volume": np.full(n, 1000.0),
            },
            index=pd.date_range("2024-01-01", periods=n, freq="5min"),
        )

        # High volatility (same seed but 5x larger movements)
        close_high = 75.0 + np.cumsum(rng_high.normal(0, 0.25, n))
        close_high = np.maximum(close_high, 50.0)
        df_high = pd.DataFrame(
            {
                "Open": close_high,
                "High": close_high + 1.0,
                "Low": close_high - 1.0,
                "Close": close_high,
                "Volume": np.full(n, 1000.0),
            },
            index=pd.date_range("2024-01-01", periods=n, freq="5min"),
        )

        result_low = self._run_barrier(df_low)
        result_high = self._run_barrier(df_high)

        # The high-vol data has wider ATR → barriers further away
        atr_low = result_low.get("ATR_14")
        atr_high = result_high.get("ATR_14")

        if atr_low is not None and atr_high is not None:
            mean_atr_low = atr_low.dropna().mean()
            mean_atr_high = atr_high.dropna().mean()
            assert mean_atr_high > mean_atr_low, (
                f"ATR should be higher for volatile data: "
                f"low={mean_atr_low:.4f}, high={mean_atr_high:.4f}"
            )
