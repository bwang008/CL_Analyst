"""
TDD-TESTER AUTHORIZATION
Target Implementation File: src/features/macro_features.py
Target Class/Function: MacroFeatureEngine._build_fred_features, MacroFeatureEngine._build_cot_features
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)

Ticket: livetest-macro-pctile-slow_07042026_1748
Root Cause: Percentile features (MACRO_*_PCTILE_*D and COT_*_PCTILE_*W) are
            computed with a Python-level lambda:
                series.rolling(w).apply(
                    lambda x: pd.Series(x).rank(pct=True).iloc[-1], raw=False)
            invoked once per window position over the full daily FRED history
            (~13,000 rows) on EVERY live bar -> ~10 s/bar (96.6% of CPU).
Fix Under Test: Replace each occurrence with pandas' native Cython rolling rank:
                series.rolling(w).rank(pct=True)
            which is bitwise identical and ~340x faster.

Test design (Red/Green contract):
  1. TestEnginePctilePerformance  -- the RED discriminators. On a synthetic
     fixture sized like the real data (13,000 daily rows x 4 macro columns),
     the current lambda implementation takes ~10 s; the native rank takes
     ~tens of ms. Budget: < 2.0 s. FAILS on current code, PASSES after fix.
  2. TestEnginePctileValuesExact  -- engine-path value locks. Expected values
     are computed with the OLD lambda expression inline and compared with
     check_exact=True (no tolerance). PASSES before AND after the fix; the
     Coder cannot pass the timing tests by changing semantics.
  3. TestRollingRankPandasSemantics -- pure pandas equivalence on adversarial
     fixtures (heavy ties, leading NaNs, mid-series NaN, all-equal window,
     series shorter than window). Pins tie/NaN semantics of
     rolling(w).rank(pct=True) against future pandas upgrades.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from src.features.macro_features import PCTILE_WINDOWS, MacroFeatureEngine

# ---------------------------------------------------------------------------
# The legacy (slow) percentile expression, verbatim from the pre-fix code.
# Used to compute EXPECTED values so the fast implementation is pinned to be
# numerically identical to the old behavior.
# ---------------------------------------------------------------------------


def _old_lambda_pctile(series: pd.Series, w: int) -> pd.Series:
    """Exact legacy expression from macro_features.py (pre-fix)."""
    return series.rolling(w).apply(
        lambda x: pd.Series(x).rank(pct=True).iloc[-1],
        raw=False,
    )


# ---------------------------------------------------------------------------
# Fixture builders (no filesystem / network — frames are injected into the
# engine's _fred_df/_cot_df caches, which _load_fred/_load_cot return as-is).
# ---------------------------------------------------------------------------


def _make_fred_csv_df(n_days: int, adversarial: bool = False) -> pd.DataFrame:
    """FRED-shaped frame as if read from CSV (Date column, not yet indexed).

    Columns match the real download: VIX, OVX, DXY, YIELD_CURVE, FED_FUNDS.
    With ``adversarial=True``: ties (rounded values), leading NaNs (survive
    the engine's ffill), a mid-series NaN (ffilled by the engine), and an
    all-equal stretch longer than the largest percentile window (60).
    """
    dates = pd.bdate_range("1974-01-02", periods=n_days)
    rng = np.random.default_rng(42)

    vix = np.round(20.0 + np.cumsum(rng.normal(0, 0.3, n_days)), 1)
    ovx = np.round(35.0 + np.cumsum(rng.normal(0, 0.5, n_days)), 1)
    dxy = np.round(104.0 + np.cumsum(rng.normal(0, 0.1, n_days)), 2)
    yc = np.round(np.linspace(-0.5, 0.5, n_days) + rng.normal(0, 0.05, n_days), 2)

    if adversarial:
        vix[:5] = np.nan                # leading NaNs — ffill cannot fill these
        ovx[40] = np.nan                # mid-series NaN — engine ffills it
        dxy[60:130] = 101.23            # all-equal stretch > max window (60)
        yc[80:100] = 0.0                # heavy ties incl. exact zeros

    return pd.DataFrame({
        "Date": dates,
        "VIX": vix,
        "OVX": ovx,
        "DXY": dxy,
        "YIELD_CURVE": yc,
        "FED_FUNDS": np.full(n_days, 5.25),
    })


def _make_cot_csv_df(n_rows: int, adversarial: bool = False,
                     freq: str = "W-TUE") -> pd.DataFrame:
    """COT-shaped frame as if read from CSV (Date column, not yet indexed).

    Columns match the real download: OI, MM_Net, Prod_Net, Spec_Net.
    Integer-valued nets over a narrow range guarantee heavy ties. The COT
    path performs NO ffill, so injected NaNs reach the rolling directly.
    """
    dates = pd.date_range("1990-01-02", periods=n_rows, freq=freq)
    rng = np.random.default_rng(11)

    oi = rng.integers(300_000, 400_000, n_rows).astype(float)
    mm = (rng.integers(-50, 50, n_rows) * 1000).astype(float)
    prod = (rng.integers(-40, 40, n_rows) * 1000).astype(float)
    spec = (rng.integers(-30, 30, n_rows) * 1000).astype(float)

    if adversarial:
        mm[:3] = np.nan                 # leading NaNs (no ffill in COT path)
        mm[60] = np.nan                 # mid-series NaN inside 52W windows
        prod[70:80] = 12_000.0          # all-equal stretch (ties)

    return pd.DataFrame({
        "Date": dates,
        "OI": oi,
        "MM_Net": mm,
        "Prod_Net": prod,
        "Spec_Net": spec,
    })


def _make_engine(fred_df: pd.DataFrame | None = None,
                 cot_df: pd.DataFrame | None = None) -> MacroFeatureEngine:
    """MacroFeatureEngine with fixture frames injected into its data caches.

    Patches __init__ to a no-op (no instrument-master resolution, no
    data_paths lookup, no filesystem access). _load_fred/_load_cot return
    the injected caches directly, so tests never touch C:\\CL_Analyst_Data.
    """
    with patch.object(MacroFeatureEngine, "__init__", lambda self, **kw: None):
        engine = MacroFeatureEngine()
    engine._fred_df = fred_df
    engine._cot_df = cot_df
    engine.instrument = MagicMock()
    engine.instrument.symbol = "CL"
    return engine


def _prep_fred(fred_df: pd.DataFrame) -> pd.DataFrame:
    """Replicate _build_fred_features preprocessing (index, ffill, +1d shift)."""
    prep = fred_df.set_index("Date").sort_index().ffill()
    prep.index = prep.index + pd.Timedelta(days=1)
    return prep


def _prep_cot(cot_df: pd.DataFrame) -> pd.DataFrame:
    """Replicate _build_cot_features preprocessing (+3 BDay + 15.5h shift)."""
    prep = cot_df.set_index("Date").sort_index()
    prep.index = prep.index + pd.offsets.BDay(3) + pd.Timedelta(hours=15, minutes=30)
    return prep


# ---------------------------------------------------------------------------
# 1. RED DISCRIMINATORS — percentile construction must be FAST
# ---------------------------------------------------------------------------


class TestEnginePctilePerformance:
    """Timing gates sized like production data.

    Current lambda implementation: ~10 s for the FRED path (measured in the
    pinned trader env, py-spy attributed 96.6% of live CPU to this loop).
    Native rolling rank: ~tens of ms. The 2.0 s budget fails Red (~10 s)
    and passes Green (~0.05 s) with wide margins in both directions.
    """

    FRED_ROWS = 13_000   # matches real fred_macro_data_cl.csv (1954->2026)
    COT_ROWS = 15_000    # oversized weekly frame to expose the same lambda
    BUDGET_SECONDS = 2.0

    def test_build_fred_features_pctile_within_time_budget(self):
        """_build_fred_features over 13,000 daily rows x 4 macro columns
        (12 percentile columns) must complete within the budget.

        FAILS on the lambda implementation (~10 s). PASSES on native
        rolling(w).rank(pct=True) (~0.05 s).
        """
        fred_df = _make_fred_csv_df(self.FRED_ROWS)
        engine = _make_engine(fred_df=fred_df)

        t0 = time.perf_counter()
        features = engine._build_fred_features(check_staleness=False)
        elapsed = time.perf_counter() - t0

        # Sanity BEFORE the timing assert: all 12 percentile columns exist
        # and contain valid percentiles — a fast-but-empty result must fail.
        for col in ["VIX", "OVX", "DXY", "YIELD_CURVE"]:
            for w in PCTILE_WINDOWS:
                name = f"MACRO_{col}_PCTILE_{w}D"
                assert name in features.columns, f"missing column {name}"
                vals = features[name].dropna()
                assert len(vals) > 0, f"{name} is all-NaN"
                assert vals.between(0.0, 1.0).all(), (
                    f"{name} has values outside (0, 1]"
                )

        assert elapsed < self.BUDGET_SECONDS, (
            f"_build_fred_features took {elapsed:.2f}s for {self.FRED_ROWS} "
            f"rows (budget {self.BUDGET_SECONDS:.1f}s). The MACRO_*_PCTILE_*D "
            f"loop is still using the slow Python-lambda rolling apply — "
            f"replace it with series.rolling(w).rank(pct=True)."
        )

    def test_build_cot_features_pctile_within_time_budget(self):
        """_build_cot_features (5 percentile blocks) must complete within the
        budget on an oversized COT frame.

        FAILS on the lambda implementation. PASSES on native rolling rank.
        Daily-frequency dates are used purely to keep 15,000 timestamps
        inside pandas' Timestamp range; only row count drives the cost.
        """
        cot_df = _make_cot_csv_df(self.COT_ROWS, freq="D")
        engine = _make_engine(cot_df=cot_df)

        t0 = time.perf_counter()
        features = engine._build_cot_features()
        elapsed = time.perf_counter() - t0

        for name in [
            "COT_MM_NET_PCTILE_52W",
            "COT_MM_NET_PCTILE_14W",
            "COT_MM_NET_PCTILE_35W",
            "COT_PROD_NET_PCTILE_52W",
            "COT_SPEC_NET_PCTILE_52W",
        ]:
            assert name in features.columns, f"missing column {name}"
            vals = features[name].dropna()
            assert len(vals) > 0, f"{name} is all-NaN"
            assert vals.between(0.0, 1.0).all(), f"{name} outside (0, 1]"

        assert elapsed < self.BUDGET_SECONDS, (
            f"_build_cot_features took {elapsed:.2f}s for {self.COT_ROWS} "
            f"rows (budget {self.BUDGET_SECONDS:.1f}s). The COT_*_PCTILE_* "
            f"blocks are still using the slow Python-lambda rolling apply — "
            f"replace them with .rolling(w).rank(pct=True)."
        )


# ---------------------------------------------------------------------------
# 2. ENGINE-PATH VALUE LOCKS — exact equality with the legacy lambda
#    (pass before AND after the fix; forbid any semantic drift)
# ---------------------------------------------------------------------------


class TestEnginePctileValuesExact:
    """The engine's percentile outputs must be numerically IDENTICAL
    (check_exact=True, no tolerance) to the legacy lambda expression on an
    adversarial fixture. Combined with the timing gates above, this forces
    the implementation to be both fast and bitwise-equivalent.
    """

    def test_fred_pctile_columns_match_old_lambda_exactly(self):
        fred_df = _make_fred_csv_df(200, adversarial=True)
        engine = _make_engine(fred_df=fred_df)

        features = engine._build_fred_features(check_staleness=False)
        prep = _prep_fred(fred_df)

        for col in ["VIX", "OVX", "DXY", "YIELD_CURVE"]:
            for w in PCTILE_WINDOWS:
                name = f"MACRO_{col}_PCTILE_{w}D"
                assert name in features.columns, f"missing column {name}"

                expected = _old_lambda_pctile(prep[col], w).rename(name)

                # Guard against a vacuous all-NaN comparison.
                assert expected.notna().sum() > 0, (
                    f"fixture produced all-NaN expected values for {name}"
                )
                pd.testing.assert_series_equal(
                    features[name],
                    expected,
                    check_exact=True,
                    check_freq=False,
                    obj=name,
                )

    def test_cot_pctile_columns_match_old_lambda_exactly(self):
        cot_df = _make_cot_csv_df(120, adversarial=True)
        engine = _make_engine(cot_df=cot_df)

        features = engine._build_cot_features()
        prep = _prep_cot(cot_df)

        cases = [
            ("COT_MM_NET_PCTILE_52W", "MM_Net", 52),
            ("COT_MM_NET_PCTILE_14W", "MM_Net", 14),
            ("COT_MM_NET_PCTILE_35W", "MM_Net", 35),
            ("COT_PROD_NET_PCTILE_52W", "Prod_Net", 52),
            ("COT_SPEC_NET_PCTILE_52W", "Spec_Net", 52),
        ]
        for name, src_col, w in cases:
            assert name in features.columns, f"missing column {name}"

            expected = _old_lambda_pctile(prep[src_col], w).rename(name)

            assert expected.notna().sum() > 0, (
                f"fixture produced all-NaN expected values for {name}"
            )
            pd.testing.assert_series_equal(
                features[name],
                expected,
                check_exact=True,
                check_freq=False,
                obj=name,
            )

    def test_fred_pctile_values_via_merge_all_bar_path(self):
        """End-to-end through merge_all: percentile values ffilled onto bars
        must equal the legacy lambda values ffilled the same way. Exercises
        the exact production entry point (build_live_features -> merge_all).
        """
        fred_df = _make_fred_csv_df(200, adversarial=True)
        engine = _make_engine(fred_df=fred_df)

        prep = _prep_fred(fred_df)
        # Hourly bars spanning the tail of the FRED history.
        bar_times = pd.date_range(
            prep.index[-30], prep.index[-1], freq="1h"
        )
        bar_df = pd.DataFrame(
            {"Close": np.linspace(70.0, 71.0, len(bar_times))},
            index=bar_times,
        )

        result = engine.merge_all(
            bar_df,
            include_fred=True,
            include_cot=False,
            check_staleness=False,
        )

        name = f"MACRO_VIX_PCTILE_{PCTILE_WINDOWS[0]}D"
        expected_daily = _old_lambda_pctile(prep["VIX"], PCTILE_WINDOWS[0])
        expected_bars = expected_daily.reindex(bar_times, method="ffill")
        expected_bars.index = bar_df.index
        expected_bars = expected_bars.rename(name)

        assert expected_bars.notna().sum() > 0
        pd.testing.assert_series_equal(
            result[name],
            expected_bars,
            check_exact=True,
            check_freq=False,
            obj=name,
        )


# ---------------------------------------------------------------------------
# 3. PURE PANDAS SEMANTICS — pin rolling rank tie/NaN behavior
#    (independent of the engine; guards future pandas upgrades)
# ---------------------------------------------------------------------------


class TestRollingRankPandasSemantics:
    """series.rolling(w).rank(pct=True) must be exactly equivalent to the
    legacy lambda on adversarial inputs. This tests pandas itself and passes
    both before and after the fix — it is the semantic contract that makes
    the mechanical substitution safe.
    """

    @staticmethod
    def _adversarial_series(n: int = 200) -> pd.Series:
        rng = np.random.default_rng(7)
        vals = rng.integers(0, 5, size=n).astype(float)  # heavy ties
        vals[:6] = np.nan                                # leading NaNs
        vals[100] = np.nan                               # mid-series NaN
        vals[130:190] = 3.14                             # all-equal stretch (60)
        return pd.Series(
            vals, index=pd.bdate_range("2020-01-01", periods=n), name="X"
        )

    @pytest.mark.parametrize("w", [14, 35, 52, 60])
    def test_native_rank_equals_lambda_on_adversarial_series(self, w):
        series = self._adversarial_series()

        fast = series.rolling(w).rank(pct=True)
        slow = _old_lambda_pctile(series, w)

        # NaN masks must match exactly, and the comparison must not be vacuous.
        assert fast.isna().equals(slow.isna()), (
            f"NaN masks differ between native rank and lambda for w={w}"
        )
        assert slow.notna().sum() > 0, f"all-NaN comparison for w={w}"
        pd.testing.assert_series_equal(
            fast, slow, check_exact=True, check_freq=False,
        )

    def test_native_rank_equals_lambda_on_all_equal_window(self):
        """A window of identical values: every element ranks average -> the
        last element's pct rank is identical under both implementations."""
        w = 14
        series = pd.Series(
            np.full(40, 7.5),
            index=pd.bdate_range("2021-01-01", periods=40),
        )
        pd.testing.assert_series_equal(
            series.rolling(w).rank(pct=True),
            _old_lambda_pctile(series, w),
            check_exact=True,
            check_freq=False,
        )

    def test_native_rank_equals_lambda_when_series_shorter_than_window(self):
        """len(series) < window: both must return all-NaN (min_periods)."""
        w = 14
        series = pd.Series(
            np.arange(10, dtype=float),
            index=pd.bdate_range("2021-01-01", periods=10),
        )
        fast = series.rolling(w).rank(pct=True)
        slow = _old_lambda_pctile(series, w)

        assert fast.isna().all()
        assert slow.isna().all()
        pd.testing.assert_series_equal(
            fast, slow, check_exact=True, check_freq=False,
        )
