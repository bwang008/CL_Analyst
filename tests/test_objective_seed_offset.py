"""
TDD-TESTER AUTHORIZATION
Target Implementation File: agent/strategy_optimizer.py
Target Class/Function: run_optimization
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)

Ticket: ensemble-objective-report-parity_07032026_2055

Tests for per-objective seed offset in run_optimization().

The bug: both Sharpe and Sortino objectives receive the same TPE sampler
seed, so with small trial budgets (≤ n_startup_trials), all parameter
suggestions are identical regardless of objective.

The fix: derive ``effective_seed = random_seed + _OBJECTIVE_SEED_OFFSETS[objective_metric]``
and use it for both ``np.random.seed()`` and ``TPESampler(seed=...)``.
"""

from __future__ import annotations

import copy
import os
import sys
from unittest.mock import patch, MagicMock, call

import numpy as np
import pandas as pd
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_stub_predictions():
    """Minimal DataFrame that quacks like OOS predictions."""
    idx = pd.date_range("2024-01-01", periods=200, freq="h")
    return pd.DataFrame(
        {"pred_long": 0.6, "pred_short": 0.4},
        index=idx,
    )


def _make_stub_ohlcv():
    """Minimal OHLCV DataFrame with enough columns to pass surface checks."""
    idx = pd.date_range("2024-01-01", periods=200, freq="h")
    return pd.DataFrame(
        {"open": 70.0, "high": 71.0, "low": 69.0, "close": 70.5, "volume": 1000},
        index=idx,
    )


def _make_mock_baseline_result():
    """MagicMock that satisfies extract_metrics()."""
    result = MagicMock()
    result.trades = []
    result.total_pnl = 0.0
    result.profit_factor = 0.0
    result.win_rate = 0.0
    result.trade_count = 0
    result.max_drawdown = 0.0
    result.equity_curve = pd.Series([0.0])
    result.start_dt = pd.Timestamp("2024-01-01")
    result.end_dt = pd.Timestamp("2024-06-01")
    result.exit_distribution = {}
    return result


def _make_mock_study():
    """Return a MagicMock that quacks like an optuna.Study."""
    study = MagicMock()
    study.best_trial = MagicMock()
    study.best_trial.number = 0
    study.best_trial.params = {"tp_atr_mult": 1.5}
    study.best_trial.value = 1.0
    study.trials = [study.best_trial]
    return study


# We need to build a patch context that short-circuits the heavy machinery
# in run_optimization while still allowing us to capture the seed arguments.
# The patch list covers the entire data-loading preamble so the function can
# reach the Optuna study creation (where we capture the TPE seed) before
# the _EarlyExit short-circuit fires via study.optimize().

_MINIMAL_CONFIG = {
    "nickname": "test_model",
    "execution_class": "TieredEnsembleStrategy",
    "exit_mode": "TIERED",
    "tp_atr_mult": 1.5,
    "sl_atr_mult": 1.0,
    "trailing_atr_mult": 100.0,
    "max_hold_bars": 24,
    "cooldown_bars": 4,
    "entry_threshold": 0.55,
    "allow_concurrent": False,
    "max_concurrent": 1,
    "models": {"long": {"threshold": 0.55}, "short": {"threshold": 0.55}},
    "long": {
        "tp_atr_mult": 1.5, "sl_atr_mult": 1.0,
        "tiered_exits": [{"qty_pct": 1.0, "tp_atr_mult": 1.5}],
        "tiers": [{"min_prob": 0.55, "lots": 1}],
    },
    "short": {
        "tp_atr_mult": 1.5, "sl_atr_mult": 1.0,
        "tiered_exits": [{"qty_pct": 1.0, "tp_atr_mult": 1.5}],
        "tiers": [{"min_prob": 0.55, "lots": 1}],
    },
}


def _run_optimization_capturing_seeds(objective_metric: str, random_seed: int = 42):
    """Call run_optimization with heavy I/O mocked out.

    Returns (np_seed_arg, tpe_seed_arg) — the actual values passed to
    ``np.random.seed()`` and ``TPESampler(seed=...)``.
    """
    import agent.strategy_optimizer as mod

    captured_np_seed = None
    captured_tpe_seed = None

    original_np_seed = np.random.seed

    def fake_np_seed(seed):
        nonlocal captured_np_seed
        # Only capture the FIRST call (the one at the top of run_optimization)
        if captured_np_seed is None:
            captured_np_seed = seed
        original_np_seed(seed)

    mock_study = _make_mock_study()

    class FakeTPESampler:
        def __init__(self, *, seed=None, **kwargs):
            nonlocal captured_tpe_seed
            captured_tpe_seed = seed

    # Controlled exception to short-circuit after study creation
    class _EarlyExit(Exception):
        pass

    mock_study.optimize.side_effect = _EarlyExit("short-circuit after study creation")

    # Mock BacktestEngine used in the baseline computation
    mock_engine = MagicMock()
    mock_engine.run.return_value = _make_mock_baseline_result()
    mock_engine_cls = MagicMock()
    mock_engine_cls.from_config.return_value = mock_engine

    stub_preds = _make_stub_predictions()
    stub_ohlcv = _make_stub_ohlcv()

    patches = [
        # Seed capture
        patch.object(np.random, "seed", side_effect=fake_np_seed),
        # Optuna layer
        patch("optuna.samplers.TPESampler", FakeTPESampler),
        patch("optuna.create_study", return_value=mock_study),
        # Config loading (inline import inside run_optimization)
        patch(
            "src.live_execution.config_loader.load_strategy_config",
            return_value=copy.deepcopy(_MINIMAL_CONFIG),
        ),
        # Predictions loading — config has "models" key, so
        # _load_ensemble_predictions is tried first.
        patch.object(
            mod, "_load_ensemble_predictions",
            return_value=stub_preds,
        ),
        # OHLCV + ATR cache
        patch.object(mod, "load_ohlcv_dual", return_value=(stub_ohlcv, stub_ohlcv.copy())),
        patch.object(mod, "attach_atr_cache", side_effect=lambda df: df),
        # Baseline backtest
        patch.object(mod, "BacktestEngine", mock_engine_cls),
        patch.object(mod, "extract_metrics", return_value={
            "total_pnl": 0, "profit_factor": 0, "win_rate": 0,
            "trade_count": 0, "max_drawdown": 0,
        }),
        # Objective + trackers
        patch.object(mod, "make_objective", return_value=MagicMock()),
        patch.object(mod, "BestResultTracker", return_value=MagicMock()),
        patch.object(mod, "TopKTracker", return_value=MagicMock()),
        # DB path filesystem ops (mkdir + unlink)
        patch("pathlib.Path.mkdir"),
        patch("pathlib.Path.unlink"),
        # Warm-start param extraction
        patch.object(mod, "_extract_warm_start_params", return_value=None),
    ]

    for p in patches:
        p.start()
    try:
        try:
            mod.run_optimization(
                config_path="fake_config.json",
                n_trials=3,
                objective_metric=objective_metric,
                random_seed=random_seed,
                quiet=True,
            )
        except _EarlyExit:
            pass  # expected — we only need the seed captures
    finally:
        for p in patches:
            p.stop()

    return captured_np_seed, captured_tpe_seed


# ---------------------------------------------------------------------------
# Test 1: Sharpe gets seed offset 0 (backward compatible)
# ---------------------------------------------------------------------------


class TestSeedOffsetSharpe:
    """Sharpe objective must use offset 0, so effective_seed == random_seed."""

    def test_np_seed_sharpe_unchanged(self):
        """np.random.seed() receives the raw random_seed for sharpe (offset=0)."""
        np_seed, _ = _run_optimization_capturing_seeds("sharpe", random_seed=42)
        assert np_seed == 42, (
            f"Sharpe np.random.seed should be 42 (offset 0), got {np_seed}"
        )

    def test_tpe_seed_sharpe_unchanged(self):
        """TPESampler receives the raw random_seed for sharpe (offset=0)."""
        _, tpe_seed = _run_optimization_capturing_seeds("sharpe", random_seed=42)
        assert tpe_seed == 42, (
            f"Sharpe TPESampler seed should be 42 (offset 0), got {tpe_seed}"
        )


# ---------------------------------------------------------------------------
# Test 2: Sortino gets seed offset 1
# ---------------------------------------------------------------------------


class TestSeedOffsetSortino:
    """Sortino objective must use offset 1, so effective_seed == random_seed + 1."""

    def test_np_seed_sortino_offset(self):
        """np.random.seed() receives random_seed + 1 for sortino."""
        np_seed, _ = _run_optimization_capturing_seeds("sortino", random_seed=42)
        assert np_seed == 43, (
            f"Sortino np.random.seed should be 43 (seed 42 + offset 1), got {np_seed}"
        )

    def test_tpe_seed_sortino_offset(self):
        """TPESampler receives random_seed + 1 for sortino."""
        _, tpe_seed = _run_optimization_capturing_seeds("sortino", random_seed=42)
        assert tpe_seed == 43, (
            f"Sortino TPESampler seed should be 43 (seed 42 + offset 1), got {tpe_seed}"
        )


# ---------------------------------------------------------------------------
# Test 3: Different objectives produce different seeds (divergence)
# ---------------------------------------------------------------------------


class TestObjectiveSeedDivergence:
    """Same random_seed with different objectives must produce different TPE seeds."""

    def test_sharpe_vs_sortino_seeds_differ(self):
        """Sharpe and Sortino must get different TPE sampler seeds from the same base seed."""
        _, tpe_sharpe = _run_optimization_capturing_seeds("sharpe", random_seed=42)
        _, tpe_sortino = _run_optimization_capturing_seeds("sortino", random_seed=42)
        assert tpe_sharpe != tpe_sortino, (
            f"TPE seeds must differ across objectives but both got {tpe_sharpe}"
        )

    def test_sharpe_vs_sortino_np_seeds_differ(self):
        """np.random seeds must also diverge across objectives."""
        np_sharpe, _ = _run_optimization_capturing_seeds("sharpe", random_seed=42)
        np_sortino, _ = _run_optimization_capturing_seeds("sortino", random_seed=42)
        assert np_sharpe != np_sortino, (
            f"np.random seeds must differ across objectives but both got {np_sharpe}"
        )


# ---------------------------------------------------------------------------
# Test 4: Reproducibility — same (seed, objective) => same effective seed
# ---------------------------------------------------------------------------


class TestSeedReproducibility:
    """The same (random_seed, objective_metric) pair must always yield the
    same effective seed — ensuring study-level reproducibility."""

    @pytest.mark.parametrize("objective", ["sharpe", "sortino"])
    def test_deterministic_np_seed(self, objective):
        """Two calls with the same args must produce the same np seed."""
        np1, _ = _run_optimization_capturing_seeds(objective, random_seed=99)
        np2, _ = _run_optimization_capturing_seeds(objective, random_seed=99)
        assert np1 == np2, (
            f"np.random.seed not deterministic for {objective}: {np1} vs {np2}"
        )

    @pytest.mark.parametrize("objective", ["sharpe", "sortino"])
    def test_deterministic_tpe_seed(self, objective):
        """Two calls with the same args must produce the same TPE seed."""
        _, tpe1 = _run_optimization_capturing_seeds(objective, random_seed=99)
        _, tpe2 = _run_optimization_capturing_seeds(objective, random_seed=99)
        assert tpe1 == tpe2, (
            f"TPESampler seed not deterministic for {objective}: {tpe1} vs {tpe2}"
        )


# ---------------------------------------------------------------------------
# Test 5: Offset mapping contract
# ---------------------------------------------------------------------------


class TestOffsetMappingContract:
    """Verify the _OBJECTIVE_SEED_OFFSETS constant exists and has the right shape."""

    def test_offset_dict_exists(self):
        """The module must define _OBJECTIVE_SEED_OFFSETS as a dict."""
        from agent.strategy_optimizer import _OBJECTIVE_SEED_OFFSETS
        assert isinstance(_OBJECTIVE_SEED_OFFSETS, dict), (
            f"Expected dict, got {type(_OBJECTIVE_SEED_OFFSETS)}"
        )

    def test_sharpe_offset_is_zero(self):
        """Sharpe offset must be 0 for backward compatibility."""
        from agent.strategy_optimizer import _OBJECTIVE_SEED_OFFSETS
        assert _OBJECTIVE_SEED_OFFSETS["sharpe"] == 0

    def test_sortino_offset_is_one(self):
        """Sortino offset must be 1."""
        from agent.strategy_optimizer import _OBJECTIVE_SEED_OFFSETS
        assert _OBJECTIVE_SEED_OFFSETS["sortino"] == 1

    def test_unknown_objective_defaults_to_zero(self):
        """An unrecognized objective must fall back to offset 0 (via .get default)."""
        from agent.strategy_optimizer import _OBJECTIVE_SEED_OFFSETS
        # The get() call in run_optimization should default to 0
        assert _OBJECTIVE_SEED_OFFSETS.get("unknown_metric", 0) == 0


# ---------------------------------------------------------------------------
# Test 6: Multiple base seeds — offset arithmetic is correct
# ---------------------------------------------------------------------------


class TestMultipleBaseSeeds:
    """Offset arithmetic must work across different base seeds."""

    @pytest.mark.parametrize("base_seed", [0, 1, 100, 9999])
    def test_sortino_is_base_plus_one(self, base_seed):
        """For any base seed, sortino effective seed == base_seed + 1."""
        _, tpe_seed = _run_optimization_capturing_seeds("sortino", random_seed=base_seed)
        assert tpe_seed == base_seed + 1, (
            f"Expected TPE seed {base_seed + 1} for sortino with base {base_seed}, got {tpe_seed}"
        )

    @pytest.mark.parametrize("base_seed", [0, 1, 100, 9999])
    def test_sharpe_is_base_unchanged(self, base_seed):
        """For any base seed, sharpe effective seed == base_seed."""
        _, tpe_seed = _run_optimization_capturing_seeds("sharpe", random_seed=base_seed)
        assert tpe_seed == base_seed, (
            f"Expected TPE seed {base_seed} for sharpe with base {base_seed}, got {tpe_seed}"
        )
