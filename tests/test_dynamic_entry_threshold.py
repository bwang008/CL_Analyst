"""
TDD-TESTER AUTHORIZATION
Target Implementation File: agent/strategy_optimizer.py
Target Class/Function: _entry_threshold_bounds, make_objective, _extract_warm_start_params
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)
"""

# ---------------------------------------------------------------------------
# Ticket: dynamic-entry-threshold_07072026_1011
#
# The post-optimizer currently searches a STATIC entry-threshold range
# (``_PARAM_RANGES["entry_threshold"] = (0.30, 0.70, 0.04, "float")``) for
# every model.  Because different models emit probabilities on completely
# different scales, a fixed 0.30 floor can sit BELOW a model's entire
# probability mass -> the optimizer picks a threshold that fires on ~100% of
# bars ("always-on"), throwing away the model's ranking edge (the SI-model bug
# this ticket fixes).
#
# The feature under test: per-model, per-side entry-threshold search bounds
# derived from the model's own prediction distribution, expressed as a
# signal-firing band [f_min, f_max]:
#
#   firing fraction at threshold t:  f(t) = P(prob >= t) = 1 - CDF(t)
#   low  = quantile(1 - f_max)   (most-permissive threshold, fires ~f_max)
#   high = quantile(1 - f_min)   (most-selective threshold, fires ~f_min)
#   step = max((high - low) / 10, 1e-3)
#
# These tests are written FIRST (TDD): they must be RED against the pre-fix
# module (missing ``_entry_threshold_bounds`` / new kwargs) and GREEN after
# the Coder implements the feature in agent/strategy_optimizer.py.
# ---------------------------------------------------------------------------

import copy
import os
import sys

import numpy as np
import pandas as pd
import optuna
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import agent.strategy_optimizer as so

optuna.logging.set_verbosity(optuna.logging.WARNING)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _firing_fraction(prob_series: pd.Series, threshold: float) -> float:
    """Empirical firing fraction f(t) = P(prob >= t) on the given series."""
    s = prob_series.dropna()
    return float((s >= threshold).mean())


def _make_tiered_cfg(conflict_resolution="hold"):
    """Small synthetic tiered ensemble config (shape copied from
    tests/test_strategy_optimizer_reconstruction.py::_make_tiered_cfg)."""
    return {
        "nickname": "dyn_thr_test",
        "execution_class": "TieredEnsembleStrategy",
        "exit_mode": "TIERED",
        "tp_atr_mult": 3.0,
        "sl_atr_mult": 1.5,
        "trailing_atr_mult": 100.0,
        "max_hold_bars": 24,
        "cooldown_bars": 5,
        "entry_threshold": 0.55,
        "allow_concurrent": False,
        "max_concurrent": 1,
        "conflict_resolution": conflict_resolution,
        "models": {"long": {"threshold": 0.55}, "short": {"threshold": 0.55}},
        "long": {
            "tp_atr_mult": 3.0,
            "sl_atr_mult": 1.5,
            "tiered_exits": [{"qty_pct": 1.0, "tp_atr_mult": 3.0}],
            "tiers": [{"min_prob": 0.55, "lots": 1}],
        },
        "short": {
            "tp_atr_mult": 3.0,
            "sl_atr_mult": 1.5,
            "tiered_exits": [{"qty_pct": 1.0, "tp_atr_mult": 3.0}],
            "tiers": [{"min_prob": 0.55, "lots": 1}],
        },
    }


def _make_synthetic_ohlcv(index: pd.DatetimeIndex, seed: int = 7) -> pd.DataFrame:
    """Minimal but valid OHLCV frame for BacktestEngine.run().

    Columns Open/High/Low/Close/Volume with a DatetimeIndex (the engine's
    documented contract).  ATR_{period} columns are pre-stamped via
    so.attach_atr_cache() exactly as the real pipeline does in
    run_optimization, so BacktestEngine can compute TP/SL sizing without a
    pandas_ta recomputation crash.
    """
    rng = np.random.default_rng(seed)
    n = len(index)
    # A gently trending random walk so High/Low straddle Close realistically.
    steps = rng.normal(0.0, 0.25, size=n)
    close = 60.0 + np.cumsum(steps)
    close = np.clip(close, 20.0, 120.0)
    high = close + np.abs(rng.normal(0.5, 0.2, size=n))
    low = close - np.abs(rng.normal(0.5, 0.2, size=n))
    open_ = close - steps  # previous-ish level
    open_ = np.clip(open_, low, high)
    df = pd.DataFrame(
        {
            "Open": open_,
            "High": high,
            "Low": low,
            "Close": close,
            "Volume": rng.integers(500, 5000, size=n).astype(float),
        },
        index=index,
    )
    # Pre-stamp ATR cache columns (matches run_optimization's attach_atr_cache).
    so.attach_atr_cache(df)
    return df


# ===========================================================================
# G. MODULE CONSTANTS
# ===========================================================================

class TestModuleConstants:
    def test_firing_frac_constants(self):
        assert so.FIRING_FRAC_MIN == 0.05
        assert so.FIRING_FRAC_MAX == 0.45


# ===========================================================================
# A. Known-distribution quantile identities + firing fractions
# ===========================================================================

class TestKnownDistributionBounds:
    def test_uniform_quantile_identities(self):
        # Large uniform(0,1) grid — deterministic, no RNG.
        series = pd.Series(np.linspace(0.0, 1.0, 10001))
        f_min, f_max = 0.05, 0.45
        low, high, step = so._entry_threshold_bounds(series, f_min, f_max)

        assert low == pytest.approx(series.quantile(1 - f_max), abs=1e-9)
        assert high == pytest.approx(series.quantile(1 - f_min), abs=1e-9)
        assert step == pytest.approx(max((high - low) / 10, 1e-3), abs=1e-12)

    def test_uniform_firing_fractions(self):
        # Firing fraction at low ~ f_max, at high ~ f_min.
        series = pd.Series(np.linspace(0.0, 1.0, 10001))
        f_min, f_max = 0.05, 0.45
        low, high, _ = so._entry_threshold_bounds(series, f_min, f_max)

        assert _firing_fraction(series, low) == pytest.approx(f_max, abs=0.02)
        assert _firing_fraction(series, high) == pytest.approx(f_min, abs=0.02)

    def test_seeded_uniform_sample_identities(self):
        rng = np.random.default_rng(12345)
        series = pd.Series(rng.uniform(0.0, 1.0, size=50000))
        f_min, f_max = 0.05, 0.45
        low, high, _ = so._entry_threshold_bounds(series, f_min, f_max)

        assert low == pytest.approx(series.quantile(1 - f_max), abs=1e-9)
        assert high == pytest.approx(series.quantile(1 - f_min), abs=1e-9)
        assert _firing_fraction(series, low) == pytest.approx(f_max, abs=0.02)
        assert _firing_fraction(series, high) == pytest.approx(f_min, abs=0.02)

    def test_skewed_distribution_identities(self):
        # Realistic SKEWED distribution (Beta(2, 5) — right-skewed, in [0,1]).
        rng = np.random.default_rng(2026)
        series = pd.Series(rng.beta(2.0, 5.0, size=50000))
        f_min, f_max = 0.05, 0.45
        low, high, step = so._entry_threshold_bounds(series, f_min, f_max)

        # Quantile identities hold regardless of shape.
        assert low == pytest.approx(series.quantile(1 - f_max), abs=1e-9)
        assert high == pytest.approx(series.quantile(1 - f_min), abs=1e-9)
        # Firing-fraction relationships hold for the skewed shape too.
        assert _firing_fraction(series, low) == pytest.approx(f_max, abs=0.02)
        assert _firing_fraction(series, high) == pytest.approx(f_min, abs=0.02)
        assert step > 0

    def test_clipped_normal_distribution_identities(self):
        # A second realistic shape: clipped normal centered mid-range.
        rng = np.random.default_rng(99)
        raw = rng.normal(0.5, 0.15, size=50000)
        series = pd.Series(np.clip(raw, 0.0, 1.0))
        f_min, f_max = 0.05, 0.45
        low, high, _ = so._entry_threshold_bounds(series, f_min, f_max)

        assert low == pytest.approx(series.quantile(1 - f_max), abs=1e-9)
        assert high == pytest.approx(series.quantile(1 - f_min), abs=1e-9)
        assert _firing_fraction(series, low) == pytest.approx(f_max, abs=0.02)
        assert _firing_fraction(series, high) == pytest.approx(f_min, abs=0.02)


# ===========================================================================
# B. THE INVERSION (critical): larger firing fraction -> LOWER threshold
# ===========================================================================

class TestFiringQuantileInversion:
    def test_low_below_high(self):
        rng = np.random.default_rng(7)
        series = pd.Series(rng.uniform(0.0, 1.0, size=20000))
        low, high, _ = so._entry_threshold_bounds(series, 0.05, 0.45)
        assert low < high

    def test_more_permissive_end_is_lower_threshold(self):
        # The core inversion guard: the more-permissive (higher-firing, f_max)
        # end must map to the LOWER threshold; the more-selective (f_min) end
        # to the HIGHER threshold.  i.e. quantile(1 - f_max) < quantile(1 - f_min).
        rng = np.random.default_rng(555)
        series = pd.Series(rng.uniform(0.0, 1.0, size=20000))
        f_min, f_max = 0.05, 0.45
        assert series.quantile(1 - f_max) < series.quantile(1 - f_min)

        low, high, _ = so._entry_threshold_bounds(series, f_min, f_max)
        # low fires ~45% (permissive), high fires ~5% (selective).
        assert _firing_fraction(series, low) > _firing_fraction(series, high)

    def test_increasing_f_max_lowers_low(self):
        # Widening the permissive end (larger f_max) must LOWER the floor.
        rng = np.random.default_rng(321)
        series = pd.Series(rng.uniform(0.0, 1.0, size=20000))
        low_narrow, _, _ = so._entry_threshold_bounds(series, 0.05, 0.30)
        low_wide, _, _ = so._entry_threshold_bounds(series, 0.05, 0.45)
        assert low_wide < low_narrow

    def test_increasing_f_min_lowers_high(self):
        # Loosening the selective end (larger f_min) must LOWER the ceiling.
        rng = np.random.default_rng(654)
        series = pd.Series(rng.uniform(0.0, 1.0, size=20000))
        _, high_selective, _ = so._entry_threshold_bounds(series, 0.05, 0.45)
        _, high_loose, _ = so._entry_threshold_bounds(series, 0.20, 0.45)
        assert high_loose < high_selective


# ===========================================================================
# C. DEGENERATE distribution (near-constant probs)
# ===========================================================================

class TestDegenerateDistribution:
    def test_constant_series_yields_valid_range(self):
        series = pd.Series([0.5] * 1000)
        low, high, step = so._entry_threshold_bounds(series, 0.05, 0.45)
        assert high > low            # non-empty range for Optuna
        assert 0.0 <= low <= 1.0
        assert 0.0 <= high <= 1.0
        assert step > 0

    def test_tiny_jitter_series_yields_valid_range(self):
        rng = np.random.default_rng(11)
        series = pd.Series(0.5 + rng.normal(0.0, 1e-5, size=1000))
        low, high, step = so._entry_threshold_bounds(series, 0.05, 0.45)
        assert high > low
        assert 0.0 <= low <= 1.0
        assert 0.0 <= high <= 1.0
        assert step > 0


# ===========================================================================
# D. COMPRESSED distribution (all in ~0.32-0.65) — the SI-model bug
# ===========================================================================

class TestCompressedDistribution:
    def test_floor_never_fires_everything(self):
        # This is the EXACT SI-model bug this ticket fixes: a model whose entire
        # probability mass lives in ~0.32-0.65.  The OLD static 0.30 floor sits
        # BELOW the whole distribution, so the optimizer could pick a threshold
        # that fires on ~100% of bars ("always-on").  The dynamic floor must
        # instead land inside the distribution so firing at `low` <= f_max.
        rng = np.random.default_rng(4242)
        series = pd.Series(rng.uniform(0.32, 0.65, size=50000))
        f_min, f_max = 0.05, 0.45
        low, high, step = so._entry_threshold_bounds(series, f_min, f_max)

        tol = 0.02
        # Firing at the floor is capped near f_max, NOT ~100%.
        assert _firing_fraction(series, low) <= f_max + tol
        # The dynamic floor sits ABOVE the old static 0.30 floor (which would
        # have fired ~100% on this compressed distribution).
        assert low > 0.30
        assert high > low
        assert 0.0 <= low <= 1.0
        assert 0.0 <= high <= 1.0
        assert step > 0

    def test_ceiling_within_distribution(self):
        rng = np.random.default_rng(8888)
        series = pd.Series(rng.uniform(0.32, 0.65, size=50000))
        f_min, f_max = 0.05, 0.45
        low, high, _ = so._entry_threshold_bounds(series, f_min, f_max)
        # Ceiling fires ~f_min, not ~0%.
        assert _firing_fraction(series, high) == pytest.approx(f_min, abs=0.02)
        # Both bounds sit inside the compressed support.
        assert series.min() <= low <= series.max()
        assert series.min() <= high <= series.max()


# ===========================================================================
# E. NaN handling
# ===========================================================================

class TestNaNHandling:
    def test_nans_match_dropna_bounds(self):
        rng = np.random.default_rng(101)
        clean = pd.Series(rng.uniform(0.1, 0.9, size=20000))

        # Inject NaNs into a copy.
        with_nans = clean.copy()
        nan_idx = rng.choice(len(with_nans), size=2000, replace=False)
        with_nans.iloc[nan_idx] = np.nan

        f_min, f_max = 0.05, 0.45
        low_n, high_n, step_n = so._entry_threshold_bounds(with_nans, f_min, f_max)
        low_c, high_c, step_c = so._entry_threshold_bounds(with_nans.dropna(), f_min, f_max)

        assert low_n == pytest.approx(low_c, abs=1e-9)
        assert high_n == pytest.approx(high_c, abs=1e-9)
        assert step_n == pytest.approx(step_c, abs=1e-12)


# ===========================================================================
# F. WARM-START CONSISTENCY (3-way invariant — main regression risk)
# ===========================================================================

class TestWarmStartConsistency:
    def test_baseline_snapped_onto_dynamic_grid(self):
        # The 3-way consistency invariant: a baseline entry_threshold that is
        # OUTSIDE the dynamic [low, high] must be snapped onto the SAME dynamic
        # grid used by the sampler — NOT the static 0.04-over-0.30 grid.  A
        # mismatch here silently corrupts the warm start (enqueue_trial distorts
        # or rejects the baseline), which is the main regression risk of this
        # ticket.
        rng = np.random.default_rng(3131)
        # Compressed prob series -> dynamic range sits well above 0.30.
        series = pd.Series(rng.uniform(0.32, 0.65, size=50000))
        low, high, step = so._entry_threshold_bounds(
            series, so.FIRING_FRAC_MIN, so.FIRING_FRAC_MAX
        )

        # Static default 0.30 is below the compressed dynamic range -> outside.
        raw = 0.30
        assert not (low <= raw <= high), (
            "fixture precondition: 0.30 must be OUTSIDE the dynamic range"
        )

        snapped = so._snap_to_grid(raw, low, high, step, "float")

        # (1) snapped value lands within the dynamic bounds.
        assert low <= snapped <= high

        # (2) snapped value is on the DYNAMIC grid: (snapped - low) is an
        #     integer multiple of the dynamic step within float tolerance.
        multiples = (snapped - low) / step
        assert multiples == pytest.approx(round(multiples), abs=1e-6)

    def test_static_default_055_snaps_into_dynamic_range(self):
        # The static 0.55 default (used as a baseline elsewhere) also snaps into
        # the dynamic range for a compressed model.
        rng = np.random.default_rng(9090)
        series = pd.Series(rng.uniform(0.32, 0.65, size=50000))
        low, high, step = so._entry_threshold_bounds(
            series, so.FIRING_FRAC_MIN, so.FIRING_FRAC_MAX
        )
        raw = 0.55
        snapped = so._snap_to_grid(raw, low, high, step, "float")
        assert low <= snapped <= high
        multiples = (snapped - low) / step
        assert multiples == pytest.approx(round(multiples), abs=1e-6)


# ===========================================================================
# H. INTEGRATION — dynamic bounds wired into the Optuna sampler
# ===========================================================================

class TestObjectiveIntegration:
    def _make_predictions(self):
        """Synthetic predictions_df with prob_Buy / prob_Sell columns and an
        hourly DatetimeIndex spanning ~2 years (so the trade-floor span math is
        well-defined and > 1 year)."""
        rng = np.random.default_rng(20260707)
        # ~2 years of hourly bars.
        index = pd.date_range("2022-01-01", periods=24 * 365 * 2, freq="1h")
        n = len(index)
        # Distinct per-side distributions so long/short bounds differ.
        prob_buy = np.clip(rng.beta(2.0, 3.0, size=n), 0.0, 1.0)      # right-ish
        prob_sell = np.clip(rng.uniform(0.32, 0.65, size=n), 0.0, 1.0)  # compressed
        preds = pd.DataFrame(
            {"prob_Buy": prob_buy, "prob_Sell": prob_sell}, index=index
        )
        return preds

    def test_sampled_entry_thresholds_within_dynamic_bounds(self):
        base_cfg = _make_tiered_cfg()
        preds = self._make_predictions()
        ohlcv = _make_synthetic_ohlcv(preds.index, seed=17)

        # Expected per-side dynamic bounds recomputed independently.
        low_long, high_long, _ = so._entry_threshold_bounds(
            preds["prob_Buy"], so.FIRING_FRAC_MIN, so.FIRING_FRAC_MAX
        )
        low_short, high_short, _ = so._entry_threshold_bounds(
            preds["prob_Sell"], so.FIRING_FRAC_MIN, so.FIRING_FRAC_MAX
        )

        objective = so.make_objective(
            base_cfg, preds, ohlcv, objective_metric="sharpe",
        )

        sampler = optuna.samplers.TPESampler(seed=42)
        study = optuna.create_study(direction="maximize", sampler=sampler)
        study.optimize(objective, n_trials=1)

        params = study.trials[0].params
        assert "entry_threshold_long" in params
        assert "entry_threshold_short" in params

        et_long = params["entry_threshold_long"]
        et_short = params["entry_threshold_short"]

        # End-to-end proof: sampled thresholds live inside the per-side dynamic
        # bounds derived from that side's own prediction distribution (tiny
        # float tolerance for grid-step rounding at the edges).
        assert low_long - 1e-9 <= et_long <= high_long + 1e-9
        assert low_short - 1e-9 <= et_short <= high_short + 1e-9

    def test_long_short_bounds_are_distinct(self):
        # The two sides use DIFFERENT distributions (prob_Buy vs prob_Sell), so
        # their dynamic bounds must differ — proving the objective keys bounds
        # per side rather than sharing one global range.
        preds = self._make_predictions()
        low_long, high_long, _ = so._entry_threshold_bounds(
            preds["prob_Buy"], so.FIRING_FRAC_MIN, so.FIRING_FRAC_MAX
        )
        low_short, high_short, _ = so._entry_threshold_bounds(
            preds["prob_Sell"], so.FIRING_FRAC_MIN, so.FIRING_FRAC_MAX
        )
        assert (low_long, high_long) != (low_short, high_short)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
