"""
Tests for DataManager — Three-Tier data architecture.

Tests seed loading, cache creation/reuse, dedup, monotonicity,
and append logic. IBKR backfill is tested via mocking.

TDD-TESTER AUTHORIZATION
Target Implementation File: src/live_execution/data_manager.py
Target Class/Function: DataManager.append_bar
Ticket: alpha-factory-downcast-warning_07142026_0816
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)
"""

import os
import tempfile
import warnings
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.live_execution.data_manager import DataManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_mock_seed_csv(path: Path, n_days: int = 200) -> None:
    """
    Create a mock seed CSV matching the cl-5m_bk.csv format.

    Format: semicolon-delimited, no header
        Date;Time;Open;High;Low;Close;Volume
        20/11/2008;00:00;181.588;...
    """
    # Generate bars for n_days at 5-min intervals
    n_bars = n_days * 288  # 288 bars per day
    start = datetime(2025, 12, 1)
    rows = []
    price = 70.0

    for i in range(n_bars):
        dt = start + timedelta(minutes=5 * i)
        date_str = dt.strftime("%d/%m/%Y")
        time_str = dt.strftime("%H:%M")
        # Simple random walk
        np.random.seed(i)
        change = np.random.normal(0, 0.05)
        price = max(price + change, 50.0)
        high = price + abs(np.random.normal(0, 0.02))
        low = price - abs(np.random.normal(0, 0.02))
        volume = np.random.randint(100, 5000)
        rows.append(
            f"{date_str};{time_str};{price:.6f};{high:.6f};{low:.6f};{price:.6f};{volume}"
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def _make_ohlcv_df(n_bars: int = 100, start: str = "2026-01-01") -> pd.DataFrame:
    """Create a simple OHLCV DataFrame for testing."""
    np.random.seed(42)
    dates = pd.date_range(start=start, periods=n_bars, freq="5min")
    close = 70.0 + np.cumsum(np.random.normal(0, 0.05, n_bars))
    return pd.DataFrame(
        {
            "DateTime": dates,
            "Open": close - 0.02,
            "High": close + 0.05,
            "Low": close - 0.05,
            "Close": close,
            "Volume": np.random.randint(100, 5000, n_bars).astype(float),
        },
        index=dates,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_dirs(tmp_path):
    """Create temp directories for seed and cache."""
    seed_dir = tmp_path / "raw"
    seed_dir.mkdir()
    cache_dir = tmp_path / "processed"
    cache_dir.mkdir()
    return seed_dir, cache_dir


@pytest.fixture
def mock_seed(tmp_dirs):
    """Create a mock seed CSV and return its path."""
    seed_dir, _ = tmp_dirs
    seed_path = seed_dir / "cl-5m_bk.csv"
    _create_mock_seed_csv(seed_path, n_days=200)
    return seed_path


@pytest.fixture
def cache_path(tmp_dirs):
    """Return the cache file path."""
    _, cache_dir = tmp_dirs
    return cache_dir / "warm_start_cache.parquet"


# ---------------------------------------------------------------------------
# Tests: Seed Loading
# ---------------------------------------------------------------------------

class TestSeedLoading:
    """Tests for loading data from the seed CSV."""

    def test_seed_creates_cache(self, mock_seed, cache_path):
        """DataManager should create a Parquet cache on first init."""
        dm = DataManager(
            symbol="CL",
            seed_path=str(mock_seed),
            cache_path=str(cache_path),
            data_client=None,
        )
        df = dm.initialize()

        assert cache_path.exists(), "Cache file should be created"
        assert len(df) > 0, "Should have data from seed"

    def test_seed_loads_last_200_days(self, mock_seed, cache_path):
        """Seed should only contain last 200 days of data."""
        dm = DataManager(
            symbol="CL",
            seed_path=str(mock_seed),
            cache_path=str(cache_path),
            data_client=None,
        )
        df = dm.initialize()

        date_range = df.index.max() - df.index.min()
        # Should be roughly 200 days (within margin)
        assert date_range.days <= 201, f"Range too large: {date_range.days} days"
        assert date_range.days >= 198, f"Range too small: {date_range.days} days"

    def test_seed_has_correct_columns(self, mock_seed, cache_path):
        """Seed DataFrame should have standard OHLCV + DateTime columns."""
        dm = DataManager(
            symbol="CL",
            seed_path=str(mock_seed),
            cache_path=str(cache_path),
            data_client=None,
        )
        df = dm.initialize()

        required = {"DateTime", "Open", "High", "Low", "Close", "Volume"}
        assert required.issubset(set(df.columns))

    def test_missing_seed_raises(self, cache_path):
        """Should raise FileNotFoundError if seed doesn't exist."""
        dm = DataManager(
            symbol="CL",
            seed_path="/nonexistent/seed.csv",
            cache_path=str(cache_path),
            data_client=None,
        )
        with pytest.raises(FileNotFoundError):
            dm.initialize()

    def test_seed_datetime_index(self, mock_seed, cache_path):
        """DateTime should be the index of the returned DataFrame."""
        dm = DataManager(
            symbol="CL",
            seed_path=str(mock_seed),
            cache_path=str(cache_path),
            data_client=None,
        )
        df = dm.initialize()

        assert isinstance(df.index, pd.DatetimeIndex)
        assert df.index.name == "DateTime"


# ---------------------------------------------------------------------------
# Tests: Cache reuse
# ---------------------------------------------------------------------------

class TestCacheReuse:
    """Tests for cache loading and reuse."""

    def test_cache_reuse_on_second_init(self, mock_seed, cache_path):
        """Second init should load from cache, not re-read seed."""
        dm1 = DataManager(
            symbol="CL",
            seed_path=str(mock_seed),
            cache_path=str(cache_path),
            data_client=None,
        )
        df1 = dm1.initialize()
        n_bars_1 = len(df1)

        # Second time — should read from cache
        dm2 = DataManager(
            symbol="CL",
            seed_path=str(mock_seed),
            cache_path=str(cache_path),
            data_client=None,
        )
        df2 = dm2.initialize()

        assert len(df2) == n_bars_1, "Cache should produce same bar count"


# ---------------------------------------------------------------------------
# Tests: Dedup & monotonicity
# ---------------------------------------------------------------------------

class TestDedupAndSort:
    """Tests for deduplication and monotonic index validation."""

    def test_dedup_removes_duplicates(self, mock_seed, cache_path):
        """Appending a duplicate bar should not increase row count."""
        dm = DataManager(
            symbol="CL",
            seed_path=str(mock_seed),
            cache_path=str(cache_path),
            data_client=None,
        )
        df = dm.initialize()
        n_before = len(df)

        # Append the last bar again (duplicate)
        last_row = df.iloc[[-1]]
        dm.append_bar(last_row)

        assert len(dm.dataframe) == n_before, "Duplicate should be removed"

    def test_monotonic_index_after_append(self, mock_seed, cache_path):
        """Index should be monotonically increasing after appending."""
        dm = DataManager(
            symbol="CL",
            seed_path=str(mock_seed),
            cache_path=str(cache_path),
            data_client=None,
        )
        dm.initialize()

        # Append a new bar 5 minutes after last
        last_ts = dm.last_timestamp
        new_ts = last_ts + timedelta(minutes=5)
        new_row = pd.DataFrame(
            [{
                "DateTime": new_ts,
                "Open": 70.0, "High": 70.5, "Low": 69.5,
                "Close": 70.2, "Volume": 1000.0,
            }],
            index=pd.DatetimeIndex([new_ts], name="DateTime"),
        )
        dm.append_bar(new_row)

        assert dm.dataframe.index.is_monotonic_increasing

    def test_out_of_order_append_gets_sorted(self, mock_seed, cache_path):
        """Appending an out-of-order bar should still result in sorted index."""
        dm = DataManager(
            symbol="CL",
            seed_path=str(mock_seed),
            cache_path=str(cache_path),
            data_client=None,
        )
        dm.initialize()

        # Append a bar that's BEFORE the last bar (out of order)
        early_ts = dm.last_timestamp - timedelta(minutes=10)
        new_row = pd.DataFrame(
            [{
                "DateTime": early_ts,
                "Open": 70.0, "High": 70.5, "Low": 69.5,
                "Close": 70.2, "Volume": 500.0,
            }],
            index=pd.DatetimeIndex([early_ts], name="DateTime"),
        )
        dm.append_bar(new_row)

        assert dm.dataframe.index.is_monotonic_increasing


# ---------------------------------------------------------------------------
# Tests: Append + flush
# ---------------------------------------------------------------------------

class TestAppendAndFlush:
    """Tests for bar appending and cache persistence."""

    def test_append_increases_count(self, mock_seed, cache_path):
        """Appending a new bar should increase the bar count by 1."""
        dm = DataManager(
            symbol="CL",
            seed_path=str(mock_seed),
            cache_path=str(cache_path),
            data_client=None,
        )
        dm.initialize()
        n_before = len(dm.dataframe)

        new_ts = dm.last_timestamp + timedelta(minutes=5)
        new_row = pd.DataFrame(
            [{
                "DateTime": new_ts,
                "Open": 71.0, "High": 71.5, "Low": 70.5,
                "Close": 71.2, "Volume": 2000.0,
            }],
            index=pd.DatetimeIndex([new_ts], name="DateTime"),
        )
        dm.append_bar(new_row)

        assert len(dm.dataframe) == n_before + 1

    def test_save_cache_creates_parquet(self, mock_seed, cache_path):
        """save_cache should create a valid Parquet file."""
        dm = DataManager(
            symbol="CL",
            seed_path=str(mock_seed),
            cache_path=str(cache_path),
            data_client=None,
        )
        dm.initialize()
        dm.save_cache()

        assert cache_path.exists()
        # Verify we can read it back
        df_reloaded = pd.read_parquet(str(cache_path))
        assert len(df_reloaded) > 0

    def test_last_timestamp_property(self, mock_seed, cache_path):
        """last_timestamp should return the max DateTime."""
        dm = DataManager(
            symbol="CL",
            seed_path=str(mock_seed),
            cache_path=str(cache_path),
            data_client=None,
        )
        df = dm.initialize()

        assert dm.last_timestamp == df.index.max()

    def test_last_timestamp_none_before_init(self, mock_seed, cache_path):
        """last_timestamp should be None before initialization."""
        dm = DataManager(
            symbol="CL",
            seed_path=str(mock_seed),
            cache_path=str(cache_path),
            data_client=None,
        )
        assert dm.last_timestamp is None

    def test_append_before_init_raises(self, mock_seed, cache_path):
        """Appending before initialization should raise RuntimeError."""
        dm = DataManager(
            symbol="CL",
            seed_path=str(mock_seed),
            cache_path=str(cache_path),
            data_client=None,
        )
        new_row = pd.Series({
            "Open": 70.0, "High": 70.5, "Low": 69.5,
            "Close": 70.2, "Volume": 1000.0,
        })
        with pytest.raises(RuntimeError, match="not initialized"):
            dm.append_bar(new_row)


# ---------------------------------------------------------------------------
# Tests: OHLCV dtype coercion on append (reconnect-backfill poisoning guard)
# Ticket 1h-reconnect-object-dtype_07082026_0032
# ---------------------------------------------------------------------------

class TestAppendBarDtypeCoercion:
    """The reconnect gap-backfill loop appends bars via
    ``append_bar(row.to_frame().T)`` where ``row`` comes from ``iterrows()`` over
    a mixed DateTime+float frame — i.e. an OBJECT-dtype Series. Without coercion,
    ``pd.concat`` upcasts the cache's float64 OHLCV columns to object, and the
    shared feature engine's ``np.log(Close / Close.shift(1))`` (alpha_factory.py)
    then raises a ufunc TypeError. Result in production: the whole fleet goes
    hourly-blind after every Gateway/TWS reconnect until a manual restart.
    """

    @staticmethod
    def _reconnect_style_row(ts):
        """A single bar shaped exactly like the reconnect-backfill append input:
        an object-dtype 1-row frame produced by iterrows() -> to_frame().T,
        mirroring ib_bars_to_dataframe output (DateTime column + float OHLCV)."""
        df = pd.DataFrame(
            [{
                "DateTime": ts,
                "Open": 71.0, "High": 71.5, "Low": 70.5,
                "Close": 71.2, "Volume": 2000.0,
            }]
        )
        df = df.set_index(pd.DatetimeIndex(df["DateTime"]), drop=False)
        df.index.name = "DateTime"
        _, series_row = next(df.iterrows())        # object-dtype Series
        return series_row.to_frame().T             # all columns object dtype

    def test_reconnect_style_append_keeps_ohlcv_float(self, mock_seed, cache_path):
        dm = DataManager(
            symbol="CL", seed_path=str(mock_seed),
            cache_path=str(cache_path), data_client=None,
        )
        dm.initialize()
        new_ts = dm.last_timestamp + timedelta(minutes=5)
        object_row = self._reconnect_style_row(new_ts)
        # Precondition: the incoming row really is the object-dtype input (the bug).
        assert object_row["Close"].dtype == object
        dm.append_bar(object_row)
        # The cache column must stay float64 — not be upcast to object.
        for col in ("Open", "High", "Low", "Close"):
            assert dm.dataframe[col].dtype == np.float64, f"{col} upcast to object"

    def test_feature_engine_runs_after_reconnect_style_append(self, mock_seed, cache_path):
        """Full-pipeline guard: the shared AlphaFactory (np.log at :180) must not
        raise on a ratio-adjusted frame built after a reconnect-style append."""
        from src.features.alpha_factory import AlphaFactory

        dm = DataManager(
            symbol="CL", seed_path=str(mock_seed),
            cache_path=str(cache_path), data_client=None,
        )
        dm.initialize()
        new_ts = dm.last_timestamp + timedelta(minutes=5)
        dm.append_bar(self._reconnect_style_row(new_ts))
        adjusted = dm.get_ratio_adjusted_df()
        AlphaFactory(adjusted)  # must not raise the ufunc TypeError

    def test_non_numeric_bar_raises_loudly(self, mock_seed, cache_path):
        """A genuinely corrupt (non-numeric) OHLCV value must fail LOUDLY at
        ingestion — never silently NaN or survive to crash downstream."""
        dm = DataManager(
            symbol="CL", seed_path=str(mock_seed),
            cache_path=str(cache_path), data_client=None,
        )
        dm.initialize()
        new_ts = dm.last_timestamp + timedelta(minutes=5)
        bad_row = pd.DataFrame(
            [{
                "DateTime": new_ts,
                "Open": 71.0, "High": 71.5, "Low": 70.5,
                "Close": "NOTANUM", "Volume": 2000.0,
            }],
            index=pd.DatetimeIndex([new_ts], name="DateTime"),
        )
        with pytest.raises((ValueError, TypeError)):
            dm.append_bar(bad_row)


# ---------------------------------------------------------------------------
# Tests: DateTime dtype coercion on append (residual reconnect poisoning)
# Ticket alpha-factory-downcast-warning_07142026_0816
# ---------------------------------------------------------------------------

_PANDAS_MAJOR = int(pd.__version__.split(".")[0])


class TestAppendBarDateTimeDtype:
    """The prior OHLCV coercion (ticket 1h-reconnect-object-dtype_07082026_0032)
    fixed Open/High/Low/Close/Volume but SKIPPED the ``DateTime`` column. The
    reconnect gap-backfill loop (live_trader.py:4216-4217) appends bars via
    ``append_bar(row.to_frame().T)`` from ``iterrows()`` — an all-object 1-row
    frame — so ``pd.concat`` permanently upcasts the cache's ``datetime64[ns]``
    DateTime column to object. Every downstream
    ``replace([inf, -inf], nan)`` over the poisoned frame (alpha_factory.py:292)
    then fires the pandas 2.x "Downcasting behavior in `replace` is deprecated"
    FutureWarning on every hourly inference.

    Fix under test: ``append_bar`` must coerce the incoming row's ``DateTime``
    column with ``pd.to_datetime(..., errors="raise")`` alongside the existing
    OHLCV ``pd.to_numeric`` loop.
    """

    @staticmethod
    def _reconnect_style_row_object_datetime(ts):
        """Reconnect-backfill row with an OBJECT-dtype ``DateTime`` column —
        the exact shape the live path produces on the fleet interpreter.

        Pandas-version nuance: on pandas 2.x, ``iterrows() -> to_frame().T``
        leaves ALL columns object (silent datetime inference was removed from
        the DataFrame constructor in 2.0), so the pin below is a no-op there.
        On pandas 1.5.3 (this test env), the transpose constructor re-infers
        the all-Timestamp column back to datetime64, hiding the bug — so we
        pin ``DateTime`` back to object to reproduce the fleet-interpreter
        input byte-for-byte. ``append_bar`` must be robust to an object
        DateTime column regardless of pandas version.
        """
        row = TestAppendBarDtypeCoercion._reconnect_style_row(ts)
        row["DateTime"] = row["DateTime"].astype(object)
        return row

    def test_reconnect_style_append_keeps_datetime64(self, mock_seed, cache_path):
        """Core red test: after a reconnect-style append, the cache's DateTime
        column must still be datetime64[ns] — not upcast to object."""
        dm = DataManager(
            symbol="CL", seed_path=str(mock_seed),
            cache_path=str(cache_path), data_client=None,
        )
        dm.initialize()
        # Precondition: the cache starts with a proper datetime64 DateTime column.
        assert pd.api.types.is_datetime64_any_dtype(dm.dataframe["DateTime"])

        new_ts = dm.last_timestamp + timedelta(minutes=5)
        object_row = self._reconnect_style_row_object_datetime(new_ts)
        # Precondition: the incoming row's DateTime really is object dtype (the bug).
        assert object_row["DateTime"].dtype == object

        dm.append_bar(object_row)

        assert pd.api.types.is_datetime64_any_dtype(dm.dataframe["DateTime"]), (
            f"DateTime column upcast to {dm.dataframe['DateTime'].dtype} — "
            "object-dtype reconnect row poisoned the cache on concat"
        )

    @pytest.mark.skipif(
        _PANDAS_MAJOR < 2,
        reason="replace() downcast FutureWarning only exists in pandas 2.x; "
               "on 1.5.3 this test would be trivially green regardless of the "
               "fix. Run under the fleet interpreter (pandas 2.2.2) for signal.",
    )
    def test_inf_replace_no_futurewarning_after_reconnect_append(
        self, mock_seed, cache_path
    ):
        """Symptom guard: the inf-replace feature step (alpha_factory.py:292
        pattern) over the post-append frame must not emit any FutureWarning."""
        dm = DataManager(
            symbol="CL", seed_path=str(mock_seed),
            cache_path=str(cache_path), data_client=None,
        )
        dm.initialize()
        new_ts = dm.last_timestamp + timedelta(minutes=5)
        dm.append_bar(self._reconnect_style_row_object_datetime(new_ts))

        frame = dm.dataframe
        with warnings.catch_warnings():
            warnings.simplefilter("error", FutureWarning)
            # Exact pattern from src/features/alpha_factory.py:292
            frame.replace([np.inf, -np.inf], np.nan).infer_objects()

    def test_unparseable_datetime_bar_raises_loudly(self, mock_seed, cache_path):
        """A bar whose DateTime value is unparseable garbage must fail LOUDLY at
        ingestion (pd.to_datetime errors="raise") — never silently enter the
        cache or coerce to NaT (repo no-silent-defaults rule)."""
        dm = DataManager(
            symbol="CL", seed_path=str(mock_seed),
            cache_path=str(cache_path), data_client=None,
        )
        dm.initialize()
        new_ts = dm.last_timestamp + timedelta(minutes=5)
        # Valid DatetimeIndex (so the set_index branch is skipped) but garbage
        # in the DateTime COLUMN — current code silently concats it into _df.
        bad_row = pd.DataFrame(
            [{
                "DateTime": "not-a-date",
                "Open": 71.0, "High": 71.5, "Low": 70.5,
                "Close": 71.2, "Volume": 2000.0,
            }],
            index=pd.DatetimeIndex([new_ts], name="DateTime"),
        )
        with pytest.raises((ValueError, TypeError)):
            dm.append_bar(bad_row)
