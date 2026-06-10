"""Tests for agent.forward_returns — log forward return computation."""

import numpy as np
import pandas as pd
import pytest

from agent.forward_returns import compute_forward_returns


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def linear_ohlcv():
    """Synthetic OHLCV with linearly increasing Close from 100 to 109."""
    dates = pd.date_range("2024-01-01", periods=10, freq="h")
    df = pd.DataFrame(
        {
            "Open": range(100, 110),
            "High": range(101, 111),
            "Low": range(99, 109),
            "Close": range(100, 110),
            "Volume": [1000] * 10,
        },
        index=dates,
    )
    return df


@pytest.fixture
def constant_ohlcv():
    """Synthetic OHLCV with constant Close = 50."""
    dates = pd.date_range("2024-01-01", periods=20, freq="h")
    df = pd.DataFrame(
        {
            "Open": [50.0] * 20,
            "High": [51.0] * 20,
            "Low": [49.0] * 20,
            "Close": [50.0] * 20,
            "Volume": [500] * 20,
        },
        index=dates,
    )
    return df


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestComputeForwardReturns:
    """Unit tests for compute_forward_returns."""

    def test_column_names_match_horizons(self, linear_ohlcv):
        """Output columns should be 'fwd_ret_{H}' for each horizon."""
        horizons = [2, 5]
        result = compute_forward_returns(linear_ohlcv, horizons=horizons)
        assert list(result.columns) == ["fwd_ret_2", "fwd_ret_5"]

    def test_default_horizons(self, linear_ohlcv):
        """Default horizons [6, 12, 24, 48, 72] produce five columns."""
        result = compute_forward_returns(linear_ohlcv)
        assert len(result.columns) == 5
        for h in [6, 12, 24, 48, 72]:
            assert f"fwd_ret_{h}" in result.columns

    def test_index_preserved(self, linear_ohlcv):
        """Output index must be identical to the input."""
        result = compute_forward_returns(linear_ohlcv, horizons=[2])
        pd.testing.assert_index_equal(result.index, linear_ohlcv.index)

    def test_log_return_values(self, linear_ohlcv):
        """ln(110/100) ≈ 0.09531, ln(102/100) ≈ 0.01980."""
        result = compute_forward_returns(linear_ohlcv, horizons=[2])
        # Row 0: Close=100, Close at +2 = 102  →  ln(102/100)
        expected = np.log(102 / 100)
        assert result["fwd_ret_2"].iloc[0] == pytest.approx(expected, rel=1e-9)

        # With horizon=9 → only row 0 valid: ln(109/100)
        result9 = compute_forward_returns(linear_ohlcv, horizons=[9])
        expected9 = np.log(109 / 100)
        assert result9["fwd_ret_9"].iloc[0] == pytest.approx(expected9, rel=1e-9)

    def test_nan_tail_rows(self, linear_ohlcv):
        """Last H rows for each horizon must be NaN."""
        result = compute_forward_returns(linear_ohlcv, horizons=[3])
        # 10 rows total, last 3 should be NaN
        assert result["fwd_ret_3"].iloc[-3:].isna().all()
        # First 7 should be non-NaN
        assert result["fwd_ret_3"].iloc[:7].notna().all()

    def test_multiple_horizons_nan_independence(self, linear_ohlcv):
        """Each horizon's NaN tail is independent — shorter horizon has fewer NaNs."""
        result = compute_forward_returns(linear_ohlcv, horizons=[2, 5])
        nan_count_2 = result["fwd_ret_2"].isna().sum()
        nan_count_5 = result["fwd_ret_5"].isna().sum()
        assert nan_count_2 == 2
        assert nan_count_5 == 5

    def test_constant_price_zero_returns(self, constant_ohlcv):
        """Constant prices → log returns are exactly 0."""
        result = compute_forward_returns(constant_ohlcv, horizons=[3])
        valid = result["fwd_ret_3"].dropna()
        np.testing.assert_allclose(valid.values, 0.0, atol=1e-15)

    def test_custom_price_col(self, linear_ohlcv):
        """Custom price_col selects a different column."""
        result = compute_forward_returns(
            linear_ohlcv, horizons=[1], price_col="Open"
        )
        # Open goes from 100..109 ⇒ ln(101/100)
        expected = np.log(101 / 100)
        assert result["fwd_ret_1"].iloc[0] == pytest.approx(expected, rel=1e-9)

    def test_missing_price_col_raises(self, linear_ohlcv):
        """Requesting a non-existent price column raises KeyError."""
        with pytest.raises(KeyError, match="Nonexistent"):
            compute_forward_returns(linear_ohlcv, price_col="Nonexistent")

    def test_log_not_percentage_returns(self, linear_ohlcv):
        """Ensure returns are log, NOT percentage: (P2-P1)/P1 ≠ ln(P2/P1)."""
        result = compute_forward_returns(linear_ohlcv, horizons=[1])
        pct_return = (101 - 100) / 100  # 0.01
        log_return = np.log(101 / 100)   # ≈ 0.00995
        assert result["fwd_ret_1"].iloc[0] == pytest.approx(log_return, rel=1e-9)
        assert result["fwd_ret_1"].iloc[0] != pytest.approx(pct_return, rel=1e-4)

    def test_vectorized_no_loops(self, linear_ohlcv):
        """Verify the function returns a plain DataFrame (smoke test)."""
        result = compute_forward_returns(linear_ohlcv, horizons=[1, 2, 3])
        assert isinstance(result, pd.DataFrame)
        assert result.shape == (len(linear_ohlcv), 3)
