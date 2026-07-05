"""Exec-data index normalization (ticket exec-data-index-zeros_07052026_0935).

Non-CL <SYM>_raw.parquet files carry an int64 RangeIndex with DateTime as a
column. Reindexing that against a DatetimeIndex silently yields all-NaN exec
columns -> 0 trades / $0 in every non-CL cloud baseline backtest.
"""
import pandas as pd
import pytest

from gcp.vm_e2e_pipeline import _normalize_exec_index


def _int64_indexed_frame():
    """Mirror the ZC_raw.parquet layout: RangeIndex + DateTime column."""
    idx = pd.date_range("2024-01-02 09:00", periods=24, freq="h")
    return pd.DataFrame({
        "DateTime": idx,
        "Open": 450.0, "High": 451.0, "Low": 449.0, "Close": 450.5,
        "Volume": 100,
    })  # default RangeIndex (int64)


class TestNormalizeExecIndex:
    def test_int64_index_with_datetime_column_normalized(self):
        df = _int64_indexed_frame()
        assert not isinstance(df.index, pd.DatetimeIndex)
        out = _normalize_exec_index(df, "ZC_raw.parquet")
        assert isinstance(out.index, pd.DatetimeIndex)
        assert "DateTime" not in out.columns
        assert out.index[0] == pd.Timestamp("2024-01-02 09:00")

    def test_reindex_after_normalization_is_not_all_nan(self):
        """The actual failure mode: reindex against a DatetimeIndex."""
        df = _int64_indexed_frame()
        target_idx = pd.date_range("2024-01-02 10:00", periods=5, freq="h")
        raw_reindex = df.reindex(target_idx)
        assert raw_reindex["Close"].isna().all()  # the bug
        fixed = _normalize_exec_index(df, "ZC_raw.parquet").reindex(target_idx)
        assert not fixed["Close"].isna().any()  # the fix

    def test_datetimeindex_passthrough_untouched(self):
        df = _int64_indexed_frame().set_index("DateTime")
        out = _normalize_exec_index(df, "CL_raw.parquet")
        assert out is df  # no copy, no mutation — CL parity

    def test_string_dates_index_coerced(self):
        df = _int64_indexed_frame().drop(columns=["DateTime"])
        df.index = pd.date_range("2024-01-02 09:00", periods=len(df), freq="h").strftime("%Y-%m-%d %H:%M")
        out = _normalize_exec_index(df, "x.csv")
        assert isinstance(out.index, pd.DatetimeIndex)

    def test_int64_index_without_datetime_column_raises(self):
        """pd.to_datetime on int64 'succeeds' as epoch-ns garbage — must raise
        instead of producing 1970 dates that silently NaN out on reindex."""
        df = _int64_indexed_frame().drop(columns=["DateTime"])
        with pytest.raises(ValueError, match="neither a DatetimeIndex"):
            _normalize_exec_index(df, "garbage.parquet")
