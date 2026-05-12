"""
Test that optimizer parameters actually reach the execution layer.

Verifies that TieredEnsembleStrategy.apply_trial_params() correctly
routes Optuna-suggested values into the tier blocks that the strategy
reads, preventing the "dead code" bug where top-level params were
silently overridden by hardcoded tier values.
"""

import copy
import json
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.live_execution.strategies.execution_models import (
    TieredEnsembleStrategy,
    EngineState,
    create_execution_strategy,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TIERED_CONFIG = {
    "nickname": "test_tiered",
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
    "models": {
        "long": {"threshold": 0.55},
        "short": {"threshold": 0.55},
    },
    "long": {
        "tp_atr_mult": 1.5,
        "sl_atr_mult": 1.0,
        "tiered_exits": [{"qty_pct": 1.0, "tp_atr_mult": 1.5}],
        "tiers": [{"min_prob": 0.55, "lots": 1}],
    },
    "short": {
        "tp_atr_mult": 1.5,
        "sl_atr_mult": 1.0,
        "tiered_exits": [{"qty_pct": 1.0, "tp_atr_mult": 1.5}],
        "tiers": [{"min_prob": 0.55, "lots": 1}],
    },
}

SINGLE_CONFIG = {
    "nickname": "test_single",
    "execution_class": "SingleModelStrategy",
    "direction": "LONG",
    "entry_threshold": 0.50,
    "tp_atr_mult": 2.0,
    "sl_atr_mult": 1.0,
    "trailing_atr_mult": 1.5,
}


# ---------------------------------------------------------------------------
# Tests: TieredEnsembleStrategy.apply_trial_params
# ---------------------------------------------------------------------------


class TestTieredApplyTrialParams:
    """Verify that apply_trial_params writes to all tier locations."""

    def test_tp_reaches_tiers(self):
        """TP must be written into long.tiers, long.tiered_exits, and top-level."""
        cfg = copy.deepcopy(TIERED_CONFIG)
        strategy = create_execution_strategy(cfg)

        strategy.apply_trial_params(cfg, {"tp_atr_mult": 7.5})

        # Top-level
        assert cfg["tp_atr_mult"] == 7.5
        # Long tier
        assert cfg["long"]["tp_atr_mult"] == 7.5
        assert cfg["long"]["tiers"][0]["tp_atr_mult"] == 7.5
        assert cfg["long"]["tiered_exits"][0]["tp_atr_mult"] == 7.5
        # Short tier
        assert cfg["short"]["tp_atr_mult"] == 7.5
        assert cfg["short"]["tiers"][0]["tp_atr_mult"] == 7.5
        assert cfg["short"]["tiered_exits"][0]["tp_atr_mult"] == 7.5

    def test_sl_reaches_tiers(self):
        """SL must be written into tiers and top-level."""
        cfg = copy.deepcopy(TIERED_CONFIG)
        strategy = create_execution_strategy(cfg)

        strategy.apply_trial_params(cfg, {"sl_atr_mult": 3.5})

        assert cfg["sl_atr_mult"] == 3.5
        assert cfg["long"]["sl_atr_mult"] == 3.5
        assert cfg["long"]["tiers"][0]["sl_atr_mult"] == 3.5
        assert cfg["short"]["sl_atr_mult"] == 3.5
        assert cfg["short"]["tiers"][0]["sl_atr_mult"] == 3.5

    def test_threshold_reaches_tiers_and_models(self):
        """entry_threshold must update tier min_prob AND models.*.threshold."""
        cfg = copy.deepcopy(TIERED_CONFIG)
        strategy = create_execution_strategy(cfg)

        strategy.apply_trial_params(cfg, {"entry_threshold": 0.70})

        assert cfg["entry_threshold"] == 0.70
        assert cfg["long"]["tiers"][0]["min_prob"] == 0.70
        assert cfg["short"]["tiers"][0]["min_prob"] == 0.70
        assert cfg["models"]["long"]["threshold"] == 0.70
        assert cfg["models"]["short"]["threshold"] == 0.70

    def test_trailing_reaches_tiers(self):
        """trailing_atr_mult must be defensively written into tier blocks."""
        cfg = copy.deepcopy(TIERED_CONFIG)
        strategy = create_execution_strategy(cfg)

        strategy.apply_trial_params(cfg, {"trailing_atr_mult": 2.5})

        assert cfg["trailing_atr_mult"] == 2.5
        assert cfg["long"]["tiers"][0]["trailing_atr_mult"] == 2.5
        assert cfg["short"]["tiers"][0]["trailing_atr_mult"] == 2.5

    def test_max_hold_reaches_tiers(self):
        """max_hold_bars must be written into tiers and top-level."""
        cfg = copy.deepcopy(TIERED_CONFIG)
        strategy = create_execution_strategy(cfg)

        strategy.apply_trial_params(cfg, {"max_hold_bars": 144})

        assert cfg["max_hold_bars"] == 144
        assert cfg["long"]["tiers"][0]["max_hold_bars"] == 144
        assert cfg["short"]["tiers"][0]["max_hold_bars"] == 144

    def test_per_side_long_only(self):
        """side='long' must only modify long tier blocks."""
        cfg = copy.deepcopy(TIERED_CONFIG)
        strategy = create_execution_strategy(cfg)

        strategy.apply_trial_params(cfg, {"tp_atr_mult": 9.0}, side="long")

        assert cfg["long"]["tiers"][0]["tp_atr_mult"] == 9.0
        assert cfg["long"]["tiered_exits"][0]["tp_atr_mult"] == 9.0
        # Short should retain original value
        assert cfg["short"]["tiers"][0].get("tp_atr_mult") != 9.0

    def test_per_side_short_only(self):
        """side='short' must only modify short tier blocks."""
        cfg = copy.deepcopy(TIERED_CONFIG)
        strategy = create_execution_strategy(cfg)

        strategy.apply_trial_params(cfg, {"sl_atr_mult": 4.0}, side="short")

        assert cfg["short"]["tiers"][0]["sl_atr_mult"] == 4.0
        assert cfg["short"]["sl_atr_mult"] == 4.0
        # Long should retain original
        assert cfg["long"]["sl_atr_mult"] == 1.0


class TestTieredOrderCarriesParams:
    """Verify that Order objects from TieredEnsembleStrategy carry the
    correct TP/SL/trailing values after apply_trial_params."""

    def test_order_carries_optimized_tp(self):
        """After apply_trial_params, Order.tp_atr_mult must match."""
        cfg = copy.deepcopy(TIERED_CONFIG)
        strategy = create_execution_strategy(cfg)
        strategy.apply_trial_params(cfg, {
            "tp_atr_mult": 7.5,
            "sl_atr_mult": 3.0,
            "entry_threshold": 0.50,
        })

        # Re-create strategy from the MODIFIED config (as BacktestEngine does)
        strategy2 = create_execution_strategy(cfg)
        state = EngineState(position=0, side=0, bars_held=0, open_positions=0)

        orders = strategy2.on_bar(
            dt=None, open_=60.0, high=61.0, low=59.0, close=60.5,
            atr=0.5, prob_buy=0.55, prob_sell=0.0, state=state,
        )

        assert len(orders) >= 1
        order = orders[0]
        assert order.action == "BUY"
        assert order.tp_atr_mult == 7.5
        assert order.sl_atr_mult == 3.0


class TestBaseStrategyApplyTrialParams:
    """Verify backward-compatible default apply_trial_params for non-tiered."""

    def test_single_model_writes_top_level(self):
        cfg = copy.deepcopy(SINGLE_CONFIG)
        strategy = create_execution_strategy(cfg)

        strategy.apply_trial_params(cfg, {
            "tp_atr_mult": 5.0,
            "sl_atr_mult": 2.0,
            "entry_threshold": 0.65,
        })

        assert cfg["tp_atr_mult"] == 5.0
        assert cfg["sl_atr_mult"] == 2.0
        assert cfg["entry_threshold"] == 0.65


# ---------------------------------------------------------------------------
# Tests: Parameter Shadowing Detection
# ---------------------------------------------------------------------------


class TestParameterShadowing:
    """Verify that BacktestEngine.from_config warns on parameter shadowing."""

    def test_shadowed_tp_warns(self):
        """Mismatched top-level vs tier TP should emit a warning."""
        from agent.backtest_engine import BacktestEngine

        cfg = copy.deepcopy(TIERED_CONFIG)
        # Create a mismatch: top-level TP differs from tier TP
        cfg["tp_atr_mult"] = 5.0  # top-level
        cfg["long"]["tp_atr_mult"] = 1.5  # tier says 1.5

        with pytest.warns(UserWarning, match="PARAM SHADOW"):
            BacktestEngine.from_config(cfg)

    def test_no_warning_when_aligned(self):
        """No warning when top-level and tiers agree."""
        from agent.backtest_engine import BacktestEngine

        cfg = copy.deepcopy(TIERED_CONFIG)
        # All aligned at 1.5
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            # Should not raise
            BacktestEngine.from_config(cfg)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
