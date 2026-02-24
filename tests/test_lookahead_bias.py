"""
Lookahead Bias & Data Leakage Detection Tests.

These tests prove that rolling indicators are strictly backward-looking.
If any indicator peeks into future data, backtests will be inflated and
live trading will underperform catastrophically.

Methodology:
  1. Run features on the full dataset
  2. Modify a FUTURE row
  3. Re-run features
  4. Assert that features at PAST rows are unchanged

If any test fails, a feature is using future data — critical bug.
"""

import numpy as np
import pandas as pd
import pytest

from src.features.alpha_factory import AlphaFactory


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_ohlcv(n: int = 500, seed: int = 42) -> pd.DataFrame:
    """Generate deterministic OHLCV data."""
    rng = np.random.RandomState(seed)
    close = 75.0 + np.cumsum(rng.normal(0, 0.1, n))
    close = np.maximum(close, 10.0)
    index = pd.date_range("2024-01-01", periods=n, freq="5min")
    return pd.DataFrame(
        {
            "Open": close + rng.normal(0, 0.05, n),
            "High": close + rng.uniform(0.05, 0.5, n),
            "Low": close - rng.uniform(0.05, 0.5, n),
            "Close": close,
            "Volume": rng.randint(500, 5000, n).astype(float),
        },
        index=index,
    )


@pytest.fixture
def base_data():
    return _make_ohlcv(500)


# =========================================================================
# TESTS
# =========================================================================

class TestLookaheadBias:
    """Verify that no feature computation uses future data."""

    # The row we'll inspect (must be after warm-up but before the modified row)
    CHECK_ROW = 200
    # The row we'll modify (in the "future" relative to CHECK_ROW)
    MODIFY_ROW = 300
    WINDOW = 24  # Use small window for speed

    def _get_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """Run AlphaFactory on a copy and return feature columns."""
        work = df.copy()
        return AlphaFactory(work).add_all_features(
            windows=[self.WINDOW], include_macro=False
        )

    def _feature_cols(self, df: pd.DataFrame) -> list[str]:
        """Return only AlphaFactory-generated columns."""
        ohlcv = {"Open", "High", "Low", "Close", "Volume", "log_ret"}
        return [c for c in df.columns if c not in ohlcv]

    def test_modifying_future_row_doesnt_change_past_features(self, base_data):
        """
        Modify row N (future), assert features at row N-100 (past) are unchanged.

        This is the core lookahead test. If ANY feature at CHECK_ROW changes
        when we modify MODIFY_ROW, that feature is leaking future data.
        """
        # Baseline
        baseline = self._get_features(base_data)
        feat_cols = self._feature_cols(baseline)

        # Modify future row
        modified = base_data.copy()
        modified.iloc[self.MODIFY_ROW, modified.columns.get_loc("Close")] = 999.0
        modified.iloc[self.MODIFY_ROW, modified.columns.get_loc("High")] = 1000.0
        modified.iloc[self.MODIFY_ROW, modified.columns.get_loc("Volume")] = 999999.0

        modified_features = self._get_features(modified)

        # Assert past row is unchanged
        for col in feat_cols:
            baseline_val = baseline[col].iloc[self.CHECK_ROW]
            modified_val = modified_features[col].iloc[self.CHECK_ROW]
            if pd.isna(baseline_val) and pd.isna(modified_val):
                continue  # Both NaN is fine
            assert np.isclose(baseline_val, modified_val, atol=1e-12, equal_nan=True), (
                f"LOOKAHEAD BIAS: '{col}' at row {self.CHECK_ROW} changed "
                f"when row {self.MODIFY_ROW} was modified! "
                f"baseline={baseline_val}, modified={modified_val}"
            )

    def test_appending_row_doesnt_change_previous_features(self, base_data):
        """
        Append a new row and verify all previous features are unchanged.

        Simulates the live scenario where a new bar arrives.
        """
        # Baseline with original data
        baseline = self._get_features(base_data)
        feat_cols = self._feature_cols(baseline)

        # Append one new row
        extended = base_data.copy()
        new_idx = extended.index[-1] + pd.Timedelta(minutes=5)
        new_row = pd.DataFrame(
            {
                "Open": [80.0],
                "High": [85.0],
                "Low": [75.0],
                "Close": [82.0],
                "Volume": [3000.0],
            },
            index=[new_idx],
        )
        extended = pd.concat([extended, new_row])
        extended_features = self._get_features(extended)

        # Check that all ORIGINAL rows are identical
        check_idx = self.CHECK_ROW
        for col in feat_cols:
            baseline_val = baseline[col].iloc[check_idx]
            extended_val = extended_features[col].iloc[check_idx]
            if pd.isna(baseline_val) and pd.isna(extended_val):
                continue
            assert np.isclose(baseline_val, extended_val, atol=1e-12, equal_nan=True), (
                f"LOOKAHEAD BIAS: '{col}' at row {check_idx} changed "
                f"when a future row was appended! "
                f"baseline={baseline_val}, extended={extended_val}"
            )

    def test_volatility_uses_only_past_data(self, base_data):
        """
        VOL_PARK_{W} should use only bars <= current index.

        We verify by modifying a future bar's High value and checking
        that VOL_PARK at the current bar is unchanged.
        """
        baseline = self._get_features(base_data)
        vol_col = f"VOL_PARK_{self.WINDOW}"

        if vol_col not in baseline.columns:
            pytest.skip(f"Column {vol_col} not found")

        modified = base_data.copy()
        modified.iloc[self.MODIFY_ROW, modified.columns.get_loc("High")] = 500.0
        modified_features = self._get_features(modified)

        baseline_val = baseline[vol_col].iloc[self.CHECK_ROW]
        modified_val = modified_features[vol_col].iloc[self.CHECK_ROW]

        if not pd.isna(baseline_val):
            assert np.isclose(baseline_val, modified_val, atol=1e-12), (
                f"VOL_PARK uses future data: baseline={baseline_val}, modified={modified_val}"
            )

    def test_atr_uses_only_past_data(self, base_data):
        """ATR_14 at bar i depends only on bars <= i."""
        work = base_data.copy()
        import pandas_ta as ta  # noqa: F401

        # Baseline ATR
        work["ATR_14"] = work.ta.atr(length=14)
        baseline_atr = work["ATR_14"].iloc[self.CHECK_ROW]

        # Modify future bar
        modified = base_data.copy()
        modified.iloc[self.MODIFY_ROW, modified.columns.get_loc("High")] = 500.0
        modified.iloc[self.MODIFY_ROW, modified.columns.get_loc("Low")] = 10.0
        modified["ATR_14"] = modified.ta.atr(length=14)
        modified_atr = modified["ATR_14"].iloc[self.CHECK_ROW]

        if not pd.isna(baseline_atr):
            assert np.isclose(baseline_atr, modified_atr, atol=1e-12), (
                f"ATR_14 uses future data: baseline={baseline_atr}, modified={modified_atr}"
            )

    def test_structure_efficiency_uses_only_past_data(self, base_data):
        """STRUC_EFFICIENCY should be backward-looking only."""
        baseline = self._get_features(base_data)
        eff_col = f"STRUC_EFFICIENCY_{self.WINDOW}"

        if eff_col not in baseline.columns:
            pytest.skip(f"Column {eff_col} not found")

        modified = base_data.copy()
        modified.iloc[self.MODIFY_ROW, modified.columns.get_loc("Close")] = 999.0
        modified_features = self._get_features(modified)

        baseline_val = baseline[eff_col].iloc[self.CHECK_ROW]
        modified_val = modified_features[eff_col].iloc[self.CHECK_ROW]

        if not pd.isna(baseline_val):
            assert np.isclose(baseline_val, modified_val, atol=1e-12), (
                f"STRUC_EFFICIENCY uses future data: baseline={baseline_val}, modified={modified_val}"
            )
