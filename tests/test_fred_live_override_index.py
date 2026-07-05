"""
TDD-TESTER AUTHORIZATION
Target Implementation File: src/features/macro_features.py
Target Class/Function: MacroFeatureEngine._build_fred_features
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)

Ticket: ValueError: index must be monotonic increasing or decreasing
Root Cause: _build_fred_features() live-override injection at line 449
            appends an intraday timestamp that can fall BEFORE the last
            shifted FRED date (+1 day), creating a non-monotonic index.
            Downstream reindex(method='ffill') then crashes.
Fix Under Test: A single `df = df.sort_index()` after the injection block.
"""

from __future__ import annotations

from unittest.mock import patch, MagicMock
import numpy as np
import pandas as pd
import pytest

from src.features.macro_features import MacroFeatureEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fred_csv_df(n_days: int = 30) -> pd.DataFrame:
    """Build a minimal FRED-shaped DataFrame as if read from CSV.

    The DataFrame has a ``Date`` column (not yet set as index) and the
    four columns that ``_build_fred_features`` processes:
    VIX, OVX, DXY, YIELD_CURVE.  Values are monotonically increasing
    to avoid triggering staleness detection.
    """
    dates = pd.bdate_range("2026-06-01", periods=n_days, freq="B")
    return pd.DataFrame({
        "Date": dates,
        "VIX":         np.linspace(18.0, 22.0, n_days),
        "OVX":         np.linspace(32.0, 38.0, n_days),
        "DXY":         np.linspace(104.0, 106.0, n_days),
        "YIELD_CURVE": np.linspace(-0.5, 0.5, n_days),
        "FED_FUNDS":   np.full(n_days, 5.25),
    })


def _make_engine_with_mock_fred(fred_df: pd.DataFrame) -> MacroFeatureEngine:
    """Create a MacroFeatureEngine whose _load_fred returns *fred_df*.

    Patches the __init__ to avoid real filesystem / instrument resolution.
    """
    with patch.object(MacroFeatureEngine, "__init__", lambda self, **kw: None):
        engine = MacroFeatureEngine()
    engine._fred_df = fred_df
    engine._cot_df = None
    engine.instrument = MagicMock()
    engine.instrument.symbol = "CL"
    # T4: vol_label_for(instrument) derives the vol column from
    # volatility_index — a bare MagicMock attribute would break matching.
    engine.instrument.volatility_index = "OVXCLS"
    return engine


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLiveOverrideIndexMonotonicity:
    """Verify _build_fred_features restores index monotonicity after
    injecting live_overrides with an intraday timestamp that precedes
    the last +1-day-shifted FRED date.

    Bug reproduction recipe
    -----------------------
    1.  FRED daily data ends on 2026-07-01.
    2.  After +1 day shift → last index entry = 2026-07-02 00:00.
    3.  live_time = pd.Timestamp("2026-07-01 23:00")  (current bar time).
    4.  df.loc[live_time] appends 2026-07-01 23:00 **after** 2026-07-02 00:00.
    5.  Index is now non-monotonic:  [..., 2026-07-02 00:00, 2026-07-01 23:00].
    6.  reindex(method='ffill') raises ValueError.

    After the fix (sort_index() post-injection), the index is re-sorted
    and the reindex call succeeds.
    """

    def _build_scenario(self):
        """Set up the exact crash scenario.

        Returns (engine, live_overrides, live_time, bar_df).
        """
        fred_df = _make_fred_csv_df(n_days=30)

        # The last FRED date in the CSV is 2026-07-10 (30 business days
        # from 2026-06-01).  After +1 day shift this becomes 2026-07-11.
        # We set live_time to 2026-07-10 23:00 — chronologically BEFORE
        # the shifted tail, which triggers the non-monotonic append.
        last_fred_date = fred_df["Date"].iloc[-1]
        live_time = pd.Timestamp(last_fred_date) + pd.Timedelta(hours=23)
        # live_time is an intraday timestamp on the SAME calendar day as
        # the raw FRED tail.  After FRED shift (+1d), the shifted tail
        # is last_fred_date + 1 day = midnight next day, which is > live_time.

        live_overrides = {"VIX": 21.5, "DXY": 105.5}

        engine = _make_engine_with_mock_fred(fred_df)

        # Build a bar DataFrame spanning the live_time neighbourhood.
        bar_times = pd.date_range(
            live_time - pd.Timedelta(hours=5),
            live_time,
            freq="1h",
        )
        bar_df = pd.DataFrame(
            {"Close": np.linspace(70.0, 71.0, len(bar_times))},
            index=bar_times,
        )
        return engine, live_overrides, live_time, bar_df

    # ----- Core failing test: _build_fred_features produces monotonic index -----

    def test_build_fred_features_index_is_monotonic_after_live_injection(self):
        """_build_fred_features must return a monotonic-increasing index
        even when live_time < max(shifted FRED dates).

        This test FAILS on the buggy code (no sort_index after injection)
        because the returned features DataFrame inherits the non-monotonic
        index from df.
        """
        engine, live_overrides, live_time, _ = self._build_scenario()

        features = engine._build_fred_features(
            live_overrides=live_overrides,
            live_time=live_time,
            check_staleness=False,
        )

        assert features.index.is_monotonic_increasing, (
            "Expected features index to be monotonic increasing after live "
            "override injection, but got non-monotonic index.  "
            f"Last 3 index values: {list(features.index[-3:])}"
        )

    # ----- Downstream crash test: reindex(method='ffill') must not raise -----

    def test_reindex_ffill_does_not_raise_after_live_injection(self):
        """The downstream reindex(method='ffill') in merge_all (line 646)
        crashes with ``ValueError: index must be monotonic`` when the
        source (fred_features) index is non-monotonic.

        This test directly exercises that reindex call and asserts no
        ValueError is raised.
        """
        engine, live_overrides, live_time, bar_df = self._build_scenario()

        features = engine._build_fred_features(
            live_overrides=live_overrides,
            live_time=live_time,
            check_staleness=False,
        )

        # This is the exact operation merge_all performs at line 646.
        # On buggy code this raises:
        #   ValueError: index must be monotonic increasing or decreasing
        try:
            aligned = features.reindex(bar_df.index, method="ffill")
        except ValueError as exc:
            pytest.fail(
                f"reindex(method='ffill') raised ValueError after live "
                f"override injection — index is non-monotonic: {exc}"
            )

        # Sanity: aligned frame should have the same length as bar_df
        assert len(aligned) == len(bar_df)

    # ----- Live override values are present in the output features -----

    def test_live_override_values_present_in_features(self):
        """After a successful build, the injected override values must
        appear in the output features at (or near) the live_time row.
        """
        engine, live_overrides, live_time, _ = self._build_scenario()

        features = engine._build_fred_features(
            live_overrides=live_overrides,
            live_time=live_time,
            check_staleness=False,
        )

        # The live_time should exist in the index after injection + sort.
        assert live_time in features.index, (
            f"live_time {live_time} not found in features index"
        )

        # The raw MACRO_ columns should reflect the overridden values.
        assert features.loc[live_time, "MACRO_VIX"] == pytest.approx(21.5), (
            "VIX override not reflected in MACRO_VIX at live_time"
        )
        assert features.loc[live_time, "MACRO_DXY"] == pytest.approx(105.5), (
            "DXY override not reflected in MACRO_DXY at live_time"
        )

    # ----- End-to-end via merge_all (the actual crash site) -----

    def test_merge_all_does_not_crash_with_live_overrides(self):
        """Full integration: merge_all must not crash when live_overrides
        produce a non-monotonic FRED index.

        This exercises the exact code path from the production traceback:
            merge_all → _build_fred_features → reindex(method='ffill')
        """
        engine, live_overrides, live_time, bar_df = self._build_scenario()

        # merge_all also loads COT — skip it to isolate the FRED path.
        try:
            result = engine.merge_all(
                bar_df,
                include_fred=True,
                include_cot=False,
                live_overrides=live_overrides,
                live_time=live_time,
                check_staleness=False,
            )
        except ValueError as exc:
            pytest.fail(
                f"merge_all raised ValueError with live_overrides — "
                f"FRED index is non-monotonic: {exc}"
            )

        # The result should have more columns than the input (FRED features added).
        assert len(result.columns) > 1, (
            "Expected FRED features to be merged into bar_df"
        )
        # Index should be preserved.
        assert result.index.equals(bar_df.index)
