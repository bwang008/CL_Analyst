"""Aggressive search-space tier (2026-07-10) — contract tests.

Locks the shrunk Optuna search space defined by the dimensionality audit
(reports/analysis/optimizer_dim_audit_07102026.md):

  * _PARAM_RANGES keeps ONLY {tp_atr_mult, sl_atr_mult, cooldown_bars,
    entry_threshold, atr_period}, with coarsened/narrowed grids.
  * _FROZEN_PARAMS pins {trigger_frac, distance_frac, max_hold_bars,
    consecutive_signal_threshold} at the 112-winner consensus medians and is
    applied to EVERY trial cfg and the reconstructed best cfg.
  * conflict_resolution is frozen at "hold" (never suggested).
  * Ensemble (pass-2) mode ties atr_period across sides via ONE
    "atr_period_shared" suggestion.
  * Dynamic entry_threshold grids are 6-point (step = span/5, was span/10).
  * Warm-start extraction emits EXACTLY the suggestable key set per mode, so
    study.enqueue_trial() cannot desynchronize from the search space.
"""

import copy
import os
import sys
import unittest.mock as mock

import optuna
import pandas as pd
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import agent.strategy_optimizer as so

optuna.logging.set_verbosity(optuna.logging.WARNING)


# ---------------------------------------------------------------------------
# Expected grids (single source of truth for these tests)
# ---------------------------------------------------------------------------

TP_GRID = {4.0, 5.0, 6.0, 7.0, 8.0}
SL_GRID = {1.0, 1.5, 2.0, 2.5, 3.0}
COOLDOWN_GRID = {1, 5, 9, 13}
ATR_GRID = {4, 12, 20, 28, 36}

SEARCHED_BASE_KEYS = {
    "tp_atr_mult", "sl_atr_mult", "cooldown_bars", "entry_threshold", "atr_period",
}
FROZEN_KEYS = {
    "trigger_frac", "distance_frac", "max_hold_bars", "consecutive_signal_threshold",
}


# ---------------------------------------------------------------------------
# Fixtures — tiered cfg + FakeEngine (pattern from
# tests/test_strategy_optimizer_reconstruction.py)
# ---------------------------------------------------------------------------

def _make_tiered_cfg():
    return {
        "nickname": "agg_space_test",
        "execution_class": "TieredEnsembleStrategy",
        "exit_mode": "TIERED",
        "tp_atr_mult": 5.0,
        "sl_atr_mult": 1.5,
        "trailing_atr_mult": 100.0,
        "max_hold_bars": 24,
        "cooldown_bars": 5,
        "entry_threshold": 0.55,
        "allow_concurrent": False,
        "max_concurrent": 1,
        "conflict_resolution": "reverse_position",  # deliberately non-frozen value
        "models": {"long": {"threshold": 0.55}, "short": {"threshold": 0.55}},
        "long": {
            "tp_atr_mult": 5.0,
            "sl_atr_mult": 1.5,
            "atr_period": 20,
            "tiered_exits": [{"qty_pct": 1.0, "tp_atr_mult": 5.0}],
            "tiers": [{"min_prob": 0.55, "lots": 1}],
        },
        "short": {
            "tp_atr_mult": 5.0,
            "sl_atr_mult": 1.5,
            "atr_period": 20,
            "tiered_exits": [{"qty_pct": 1.0, "tp_atr_mult": 5.0}],
            "tiers": [{"min_prob": 0.55, "lots": 1}],
        },
    }


def _fixture_frames():
    idx = pd.date_range("2024-01-01", periods=6, freq="30D")
    preds = pd.DataFrame({"prob_Buy": [0.2, 0.4, 0.5, 0.6, 0.8, 0.9],
                          "prob_Sell": [0.9, 0.8, 0.6, 0.5, 0.4, 0.2]}, index=idx)
    ohlcv = pd.DataFrame(
        {"Open": 60.0, "High": 61.0, "Low": 59.0, "Close": 60.5, "Volume": 1000.0},
        index=idx,
    )
    return preds, ohlcv


class _FakeTrade:
    def __init__(self, dt):
        self.exit_dt = dt
        self.net_pnl_dollars = 100.0
        self.duration_bars = 5
        self.side = 1


def _fake_engine(captured):
    idx = pd.date_range("2024-01-01", periods=3, freq="30D")

    class _FakeResult:
        trade_count = 3
        trades = [_FakeTrade(d) for d in idx]
        total_pnl = 300.0
        profit_factor = 2.0
        win_rate = 1.0
        max_drawdown = -50.0
        start_dt = idx[0]
        end_dt = idx[-1]
        exit_distribution = {}
        equity_curve = [0.0, 100.0, 200.0, 300.0]

    class _FakeEngine:
        @classmethod
        def from_config(cls, cfg, **overrides):
            captured.setdefault("cfgs", []).append(copy.deepcopy(cfg))
            return cls()

        def run(self, *a, **k):
            return _FakeResult()

    return _FakeEngine


def _run_trials(optimize_side, n_trials=3, enqueue=None):
    """Run a tiny study against the objective with a stubbed engine.

    Returns (study, captured_cfgs).
    """
    base_cfg = _make_tiered_cfg()
    preds, ohlcv = _fixture_frames()
    captured = {}
    study = optuna.create_study(
        direction="maximize", sampler=optuna.samplers.TPESampler(seed=42)
    )
    with mock.patch.object(so, "BacktestEngine", _fake_engine(captured)):
        objective = so.make_objective(
            base_cfg, preds, ohlcv,
            objective_metric="sharpe", optimize_side=optimize_side,
        )
        if enqueue is not None:
            study.enqueue_trial(enqueue)
        study.optimize(objective, n_trials=n_trials)
    return study, captured["cfgs"]


# ---------------------------------------------------------------------------
# 1. Static contract: ranges + frozen constants
# ---------------------------------------------------------------------------

class TestStaticContract:
    def test_param_ranges_exact_keys(self):
        assert set(so._PARAM_RANGES) == SEARCHED_BASE_KEYS

    def test_param_ranges_grids(self):
        assert so._PARAM_RANGES["tp_atr_mult"] == (4.0, 8.0, 1.0, "float")
        assert so._PARAM_RANGES["sl_atr_mult"] == (1.0, 3.0, 0.5, "float")
        assert so._PARAM_RANGES["cooldown_bars"] == (1, 13, 4, "int")
        assert so._PARAM_RANGES["atr_period"] == (4, 36, 8, "int")
        low, high, step, dtype = so._PARAM_RANGES["entry_threshold"]
        # static fallback grid must stay 6-point: (high-low)/step == 5
        assert dtype == "float"
        assert (high - low) / step == pytest.approx(5.0)

    def test_int_ranges_divisible_by_step(self):
        # Optuna suggest_int requires (high-low) % step == 0.
        for key in ("cooldown_bars", "atr_period"):
            low, high, step, dtype = so._PARAM_RANGES[key]
            assert dtype == "int"
            assert (int(high) - int(low)) % int(step) == 0, key

    def test_frozen_params_contract(self):
        assert so._FROZEN_PARAMS == {
            "trigger_frac": 0.4,
            "distance_frac": 0.5,
            "max_hold_bars": 30,
            "consecutive_signal_threshold": 2,
        }
        assert so._FROZEN_CONFLICT_RESOLUTION == "hold"
        assert so.SEARCH_SPACE_TIER == "aggressive"

    def test_entry_threshold_bounds_six_point_grid(self):
        s = pd.Series([i / 1000.0 for i in range(1001)])  # uniform on [0,1]
        low, high, step = so._entry_threshold_bounds(s, 0.05, 0.45)
        assert step == pytest.approx((high - low) / 5.0)


# ---------------------------------------------------------------------------
# 2. Single-side (pass-1) trial contract
# ---------------------------------------------------------------------------

class TestSingleSideTrials:
    def test_trial_param_names_and_grids(self):
        study, cfgs = _run_trials(optimize_side="long", n_trials=3)
        for t in study.trials:
            assert set(t.params) == {f"{k}_long" for k in SEARCHED_BASE_KEYS}
            assert t.params["tp_atr_mult_long"] in TP_GRID
            assert t.params["sl_atr_mult_long"] in SL_GRID
            assert t.params["cooldown_bars_long"] in COOLDOWN_GRID
            assert t.params["atr_period_long"] in ATR_GRID
            # frozen dims and conflict_resolution must never be suggested
            assert not any("trigger_frac" in k or "distance_frac" in k
                           or "max_hold_bars" in k
                           or "consecutive_signal_threshold" in k
                           or k == "conflict_resolution" for k in t.params)

    def test_frozen_values_applied_to_trial_cfg(self):
        study, cfgs = _run_trials(optimize_side="long", n_trials=2)
        for cfg, t in zip(cfgs, study.trials):
            assert cfg["conflict_resolution"] == "hold"
            assert cfg["long"]["max_hold_bars"] == 30
            assert cfg["long"]["consecutive_signal_threshold"] == 2
            tp = t.params["tp_atr_mult_long"]
            assert cfg["long"]["trailing_atr_mult"] == pytest.approx(tp * 0.4)
            assert cfg["long"]["trailing_sl_atr_offset"] == pytest.approx(tp * 0.4 * 0.5)


# ---------------------------------------------------------------------------
# 3. Ensemble (pass-2) trial contract — tied ATR + frozen conflict
# ---------------------------------------------------------------------------

class TestEnsembleTrials:
    def test_tied_atr_and_param_names(self):
        study, cfgs = _run_trials(optimize_side=None, n_trials=3)
        expected = (
            {f"{k}_long" for k in SEARCHED_BASE_KEYS - {"atr_period"}}
            | {f"{k}_short" for k in SEARCHED_BASE_KEYS - {"atr_period"}}
            | {"atr_period_shared"}
        )
        for t in study.trials:
            assert set(t.params) == expected
            assert t.params["atr_period_shared"] in ATR_GRID

    def test_cfg_sides_share_atr_and_frozen_conflict(self):
        study, cfgs = _run_trials(optimize_side=None, n_trials=2)
        for cfg, t in zip(cfgs, study.trials):
            assert cfg["conflict_resolution"] == "hold"
            assert cfg["long"]["atr_period"] == cfg["short"]["atr_period"] \
                == t.params["atr_period_shared"]
            assert cfg["long"]["max_hold_bars"] == cfg["short"]["max_hold_bars"] == 30


# ---------------------------------------------------------------------------
# 4. Warm-start extraction emits exactly the suggestable key set
# ---------------------------------------------------------------------------

class TestWarmStart:
    def test_single_side_keyset(self):
        warm = so._extract_warm_start_params(
            _make_tiered_cfg(), is_tiered=True, optimize_side="long",
        )
        assert set(warm) == {f"{k}_long" for k in SEARCHED_BASE_KEYS}

    def test_ensemble_keyset(self):
        warm = so._extract_warm_start_params(
            _make_tiered_cfg(), is_tiered=True, optimize_side=None,
        )
        expected = (
            {f"{k}_long" for k in SEARCHED_BASE_KEYS - {"atr_period"}}
            | {f"{k}_short" for k in SEARCHED_BASE_KEYS - {"atr_period"}}
            | {"atr_period_shared"}
        )
        assert set(warm) == expected
        assert "conflict_resolution" not in warm
        assert warm["atr_period_shared"] in ATR_GRID  # snapped from cfg atr 20

    def test_warm_start_enqueue_roundtrip_single_side(self):
        """The enqueued baseline must complete as trial #0 with EXACTLY the
        enqueued values — the invariant the batch warm-start path relies on."""
        base_cfg = _make_tiered_cfg()
        preds, ohlcv = _fixture_frames()
        bounds = so._compute_entry_thr_bounds(preds, 0.05, 0.45)
        warm = so._extract_warm_start_params(
            base_cfg, is_tiered=True, optimize_side="long", entry_thr_bounds=bounds,
        )
        study, cfgs = _run_trials(optimize_side="long", n_trials=1, enqueue=warm)
        t0 = study.trials[0]
        assert t0.state == optuna.trial.TrialState.COMPLETE
        assert t0.params == warm

    def test_warm_start_enqueue_roundtrip_ensemble(self):
        base_cfg = _make_tiered_cfg()
        preds, ohlcv = _fixture_frames()
        bounds = so._compute_entry_thr_bounds(preds, 0.05, 0.45)
        warm = so._extract_warm_start_params(
            base_cfg, is_tiered=True, optimize_side=None, entry_thr_bounds=bounds,
        )
        study, cfgs = _run_trials(optimize_side=None, n_trials=1, enqueue=warm)
        t0 = study.trials[0]
        assert t0.state == optuna.trial.TrialState.COMPLETE
        assert t0.params == warm


# ---------------------------------------------------------------------------
# 5. Reconstruction mirror — frozen dims + tied ATR survive into best_cfg
# ---------------------------------------------------------------------------

def _reconstruct_ensemble_cfg(base_cfg, best_params):
    """Mirror of the aggressive-tier ensemble reconstruction block in
    run_optimization / run_hybrid_optimization."""
    from src.live_execution.strategies.execution_models import create_execution_strategy

    best_cfg = copy.deepcopy(base_cfg)
    strategy = create_execution_strategy(best_cfg)

    long_params = {k.replace("_long", ""): v for k, v in best_params.items() if k.endswith("_long")}
    short_params = {k.replace("_short", ""): v for k, v in best_params.items() if k.endswith("_short")}
    _shared_atr = best_params.get("atr_period_shared")
    if _shared_atr is not None:
        long_params["atr_period"] = _shared_atr
        short_params["atr_period"] = _shared_atr
    long_params.update(so._FROZEN_PARAMS)
    short_params.update(so._FROZEN_PARAMS)
    long_params = so._derive_trailing_params(long_params)
    short_params = so._derive_trailing_params(short_params)
    strategy.apply_trial_params(best_cfg, long_params, side="long")
    strategy.apply_trial_params(best_cfg, short_params, side="short")
    so._reapply_strategy_level_params(best_cfg, dict(best_params))
    best_cfg["conflict_resolution"] = so._FROZEN_CONFLICT_RESOLUTION
    return best_cfg


class TestReconstruction:
    def test_reconstructed_cfg_matches_objective_cfg(self):
        """best_cfg rebuilt from trial params must equal the cfg the objective
        actually backtested for that trial (frozen dims, tied ATR, conflict)."""
        study, cfgs = _run_trials(optimize_side=None, n_trials=1)
        objective_cfg = cfgs[0]
        best_cfg = _reconstruct_ensemble_cfg(_make_tiered_cfg(), dict(study.trials[0].params))
        for side in ("long", "short"):
            for key in ("tp_atr_mult", "sl_atr_mult", "atr_period", "max_hold_bars",
                        "consecutive_signal_threshold", "cooldown_bars",
                        "trailing_atr_mult", "trailing_sl_atr_offset"):
                assert best_cfg[side][key] == objective_cfg[side][key], (side, key)
        assert best_cfg["conflict_resolution"] == objective_cfg["conflict_resolution"] == "hold"

    def test_reapply_skips_atr_period_shared(self):
        cfg = {}
        so._reapply_strategy_level_params(cfg, {"atr_period_shared": 20, "x_long": 1})
        assert "atr_period_shared" not in cfg


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
