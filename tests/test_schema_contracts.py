"""
Schema & Contract Validation Tests.

These tests enforce strict data contracts across the pipeline:
- OHLCV column naming (exact capitalization)
- Column dtypes (float64 for prices/volumes)
- DatetimeIndex type enforcement
- No silent type coercions

If any of these break, the feature pipeline will crash or produce
garbage during live trading.
"""

import numpy as np
import pandas as pd
import pytest

from src.features.alpha_factory import AlphaFactory
from src.live_execution.ibkr_client import _normalize_ohlcv_columns


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def valid_ohlcv():
    """Correctly formatted OHLCV DataFrame."""
    n = 200
    index = pd.date_range("2024-01-01", periods=n, freq="5min")
    rng = np.random.RandomState(42)
    close = 75.0 + np.cumsum(rng.normal(0, 0.1, n))
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
def ibkr_raw_bars():
    """Simulated raw IBKR bar data (lowercase column names)."""
    n = 50
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=n, freq="5min"),
            "open": np.linspace(70, 80, n),
            "high": np.linspace(71, 81, n),
            "low": np.linspace(69, 79, n),
            "close": np.linspace(70, 80, n),
            "volume": np.full(n, 1000.0),
        }
    )


# =========================================================================
# TESTS
# =========================================================================

class TestOHLCVSchema:
    """Enforce that OHLCV columns follow the exact naming contract."""

    REQUIRED_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]

    def test_ohlcv_columns_capitalized(self, valid_ohlcv):
        """Columns must be exactly Open, High, Low, Close, Volume."""
        for col in self.REQUIRED_COLUMNS:
            assert col in valid_ohlcv.columns, f"Missing required column: {col}"

    def test_lowercase_columns_rejected(self):
        """Lowercase column names must NOT satisfy the contract."""
        df = pd.DataFrame({"open": [1], "high": [2], "low": [0], "close": [1], "volume": [100]})
        for col in self.REQUIRED_COLUMNS:
            assert col not in df.columns, f"Lowercase '{col.lower()}' should not match '{col}'"


class TestOHLCVDtypes:
    """Enforce numeric dtypes for all OHLCV columns."""

    def test_ohlcv_dtypes_float64(self, valid_ohlcv):
        """OHLC and Volume must be float64 (not object, int, or mixed)."""
        for col in ["Open", "High", "Low", "Close", "Volume"]:
            assert valid_ohlcv[col].dtype == np.float64, (
                f"Column '{col}' should be float64, got {valid_ohlcv[col].dtype}"
            )

    def test_datetime_index_type(self, valid_ohlcv):
        """Index must be DatetimeIndex (not RangeIndex or string)."""
        assert isinstance(valid_ohlcv.index, pd.DatetimeIndex), (
            f"Index should be DatetimeIndex, got {type(valid_ohlcv.index).__name__}"
        )

    def test_no_object_dtype_columns(self, valid_ohlcv):
        """No columns should have object dtype (indicates string corruption)."""
        object_cols = valid_ohlcv.select_dtypes(include="object").columns.tolist()
        assert len(object_cols) == 0, (
            f"Found object-dtype columns (likely string corruption): {object_cols}"
        )


class TestIBKRNormalization:
    """Tests for _normalize_ohlcv_columns from ibkr_client."""

    def test_normalize_renames_correctly(self, ibkr_raw_bars):
        """_normalize_ohlcv_columns should capitalize IBKR's lowercase names."""
        result = _normalize_ohlcv_columns(ibkr_raw_bars)
        for col in ["DateTime", "Open", "High", "Low", "Close", "Volume"]:
            assert col in result.columns, f"Missing '{col}' after normalization"

    def test_normalize_preserves_values(self, ibkr_raw_bars):
        """Values should be unchanged after column renaming."""
        result = _normalize_ohlcv_columns(ibkr_raw_bars)
        np.testing.assert_array_equal(result["Open"].values, ibkr_raw_bars["open"].values)
        np.testing.assert_array_equal(result["Close"].values, ibkr_raw_bars["close"].values)

    def test_normalize_raises_on_missing_columns(self):
        """If a required column is missing, should raise ValueError."""
        df = pd.DataFrame({"open": [1], "high": [2], "low": [0]})  # missing close, volume
        with pytest.raises(ValueError, match="Missing required columns from IBKR bars"):
            _normalize_ohlcv_columns(df)


class TestAlphaFactorySchemaPreservation:
    """Verify AlphaFactory doesn't corrupt the OHLCV columns."""

    def test_alphafactory_preserves_ohlcv_columns(self, valid_ohlcv):
        """AlphaFactory must not rename or drop OHLCV columns."""
        factory = AlphaFactory(valid_ohlcv.copy())
        df = factory.add_all_features(windows=[24], include_macro=False)

        for col in ["Open", "High", "Low", "Close", "Volume"]:
            assert col in df.columns, (
                f"AlphaFactory dropped required column '{col}'"
            )

    def test_feature_dtypes_numeric(self, valid_ohlcv):
        """All feature columns added by AlphaFactory must be numeric."""
        factory = AlphaFactory(valid_ohlcv.copy())
        df = factory.add_all_features(windows=[24], include_macro=False)

        ohlcv = {"Open", "High", "Low", "Close", "Volume"}
        feature_cols = [c for c in df.columns if c not in ohlcv]

        for col in feature_cols:
            assert pd.api.types.is_numeric_dtype(df[col]), (
                f"Feature '{col}' has non-numeric dtype: {df[col].dtype}"
            )
