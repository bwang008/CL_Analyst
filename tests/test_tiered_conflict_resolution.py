"""Unit tests for TieredEnsembleStrategy conflict_resolution modes.

Tests that TieredEnsembleStrategy now supports the same conflict_resolution
behavior that was previously exclusive to JointPortfolioStrategy:
  - hold (default): ignore opposing signals while in position
  - close_existing_position: EXIT on opposing signal
  - reverse_position: EXIT + ENTER opposite on opposing signal
"""

import copy
import pytest
from src.live_execution.strategies.execution_models import (
    TieredEnsembleStrategy,
    EngineState,
)


def _make_config(conflict_resolution="hold", **overrides):
    """Build a minimal TieredEnsembleStrategy config."""
    cfg = {
        "execution_class": "TieredEnsembleStrategy",
        "nickname": "test_tiered_conflict",
        "allow_concurrent": False,
        "max_concurrent": 1,
        "conflict_resolution": conflict_resolution,
        "long": {
            "tiers": [{"min_prob": 0.55, "lots": 1}],
            "cooldown_bars": 3,
            "consecutive_signal_threshold": 0,
        },
        "short": {
            "tiers": [{"min_prob": 0.60, "lots": 1}],
            "cooldown_bars": 5,
            "consecutive_signal_threshold": 0,
        },
    }
    cfg.update(overrides)
    return cfg


def _make_state(**overrides):
    """Build a default EngineState."""
    state = EngineState()
    for k, v in overrides.items():
        setattr(state, k, v)
    return state


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------

class TestTieredConflictConfigValidation:
    """Test config parsing and validation."""

    def test_valid_conflict_modes(self):
        for mode in ("hold", "close_existing_position", "reverse_position"):
            strat = TieredEnsembleStrategy(_make_config(mode))
            assert strat.conflict_resolution == mode

    def test_default_conflict_mode_is_hold(self):
        cfg = _make_config()
        del cfg["conflict_resolution"]
        strat = TieredEnsembleStrategy(cfg)
        assert strat.conflict_resolution == "hold"

    def test_invalid_conflict_mode_raises(self):
        with pytest.raises(ValueError, match="Invalid conflict_resolution"):
            TieredEnsembleStrategy(_make_config("flip_and_pray"))


# ---------------------------------------------------------------------------
# FLAT behavior (same across all conflict modes)
# ---------------------------------------------------------------------------

class TestTieredConflictFlatBehavior:
    """Test signal evaluation when FLAT — should be identical across modes."""

    def test_single_buy_signal(self):
        strat = TieredEnsembleStrategy(_make_config())
        state = _make_state()
        orders = strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.60, 0.0, state)
        assert orders[0].action == "BUY"

    def test_single_sell_signal(self):
        strat = TieredEnsembleStrategy(_make_config())
        state = _make_state()
        orders = strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.0, 0.65, state)
        assert orders[0].action == "SELL"

    def test_conflict_flat_higher_buy_wins(self):
        """Both fire while flat: higher buy prob wins."""
        for mode in ("hold", "close_existing_position", "reverse_position"):
            strat = TieredEnsembleStrategy(_make_config(mode))
            state = _make_state()
            orders = strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.70, 0.65, state)
            assert orders[0].action == "BUY", f"Failed for mode={mode}"

    def test_conflict_flat_higher_sell_wins(self):
        """Both fire while flat: higher sell prob wins."""
        for mode in ("hold", "close_existing_position", "reverse_position"):
            strat = TieredEnsembleStrategy(_make_config(mode))
            state = _make_state()
            orders = strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.60, 0.80, state)
            assert orders[0].action == "SELL", f"Failed for mode={mode}"


# ---------------------------------------------------------------------------
# IN POSITION — hold mode (default, same as pre-refactor)
# ---------------------------------------------------------------------------

class TestTieredConflictInPositionHold:
    """Test hold mode: ignore all signals while in position."""

    def test_opposite_signal_ignored(self):
        strat = TieredEnsembleStrategy(_make_config("hold"))
        state = _make_state(position=1, side=1)
        orders = strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.0, 0.80, state)
        assert all(o.action == "HOLD" for o in orders)

    def test_same_side_signal_ignored(self):
        strat = TieredEnsembleStrategy(_make_config("hold"))
        state = _make_state(position=1, side=1)
        orders = strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.80, 0.0, state)
        assert all(o.action == "HOLD" for o in orders)

    def test_both_signals_in_position_hold(self):
        strat = TieredEnsembleStrategy(_make_config("hold"))
        state = _make_state(position=1, side=1)
        orders = strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.70, 0.80, state)
        assert all(o.action == "HOLD" for o in orders)


# ---------------------------------------------------------------------------
# IN POSITION — close_existing_position mode
# ---------------------------------------------------------------------------

class TestTieredConflictInPositionCloseExisting:
    """Test close_existing_position mode: EXIT on opposing signal."""

    def test_long_position_sell_signal_exits(self):
        strat = TieredEnsembleStrategy(_make_config("close_existing_position"))
        state = _make_state(position=1, side=1)
        orders = strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.0, 0.65, state)
        assert len(orders) == 1
        assert orders[0].action == "EXIT"
        assert orders[0].side == 1  # exiting the long

    def test_short_position_buy_signal_exits(self):
        strat = TieredEnsembleStrategy(_make_config("close_existing_position"))
        state = _make_state(position=-1, side=-1)
        orders = strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.60, 0.0, state)
        assert len(orders) == 1
        assert orders[0].action == "EXIT"
        assert orders[0].side == -1  # exiting the short

    def test_same_side_signal_holds(self):
        strat = TieredEnsembleStrategy(_make_config("close_existing_position"))
        state = _make_state(position=1, side=1)
        orders = strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.60, 0.0, state)
        assert all(o.action == "HOLD" for o in orders)

    def test_no_signal_holds(self):
        strat = TieredEnsembleStrategy(_make_config("close_existing_position"))
        state = _make_state(position=1, side=1)
        orders = strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.0, 0.0, state)
        assert all(o.action == "HOLD" for o in orders)


# ---------------------------------------------------------------------------
# IN POSITION — reverse_position mode
# ---------------------------------------------------------------------------

class TestTieredConflictInPositionReverse:
    """Test reverse_position mode: EXIT + ENTER opposite."""

    def test_long_to_short_reversal(self):
        strat = TieredEnsembleStrategy(_make_config("reverse_position"))
        state = _make_state(position=1, side=1)
        orders = strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.0, 0.65, state)
        assert len(orders) == 2
        assert orders[0].action == "EXIT"
        assert orders[0].side == 1  # exit the long
        assert orders[1].action == "SELL"
        assert orders[1].side == -1  # enter short

    def test_short_to_long_reversal(self):
        strat = TieredEnsembleStrategy(_make_config("reverse_position"))
        state = _make_state(position=-1, side=-1)
        orders = strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.60, 0.0, state)
        assert len(orders) == 2
        assert orders[0].action == "EXIT"
        assert orders[0].side == -1  # exit the short
        assert orders[1].action == "BUY"
        assert orders[1].side == 1  # enter long

    def test_same_side_signal_holds(self):
        strat = TieredEnsembleStrategy(_make_config("reverse_position"))
        state = _make_state(position=1, side=1)
        orders = strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.60, 0.0, state)
        assert all(o.action == "HOLD" for o in orders)

    def test_no_signal_holds(self):
        strat = TieredEnsembleStrategy(_make_config("reverse_position"))
        state = _make_state(position=1, side=1)
        orders = strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.0, 0.0, state)
        assert all(o.action == "HOLD" for o in orders)


# ---------------------------------------------------------------------------
# Optimizer parameter routing
# ---------------------------------------------------------------------------

class TestTieredConflictApplyTrialParams:
    """Test that conflict_resolution is correctly routed by the optimizer."""

    def test_conflict_resolution_routed(self):
        strat = TieredEnsembleStrategy(_make_config())
        cfg = copy.deepcopy(_make_config())
        params = {"conflict_resolution": "reverse_position"}
        result = strat.apply_trial_params(cfg, params)
        assert result["conflict_resolution"] == "reverse_position"

    def test_threshold_routed_to_tiers(self):
        strat = TieredEnsembleStrategy(_make_config())
        cfg = copy.deepcopy(_make_config())
        params = {"entry_threshold": 0.70, "tp_atr_mult": 4.0}
        result = strat.apply_trial_params(cfg, params, side="long")
        assert result["long"]["tiers"][0]["min_prob"] == 0.70
        assert result["long"]["tiers"][0]["tp_atr_mult"] == 4.0

    def test_conflict_mode_not_overwritten_by_side_params(self):
        """conflict_resolution should survive side-specific param routing."""
        strat = TieredEnsembleStrategy(_make_config("close_existing_position"))
        cfg = copy.deepcopy(_make_config("close_existing_position"))
        params = {"tp_atr_mult": 5.0, "sl_atr_mult": 2.0}
        result = strat.apply_trial_params(cfg, params, side="long")
        assert result["conflict_resolution"] == "close_existing_position"


# ---------------------------------------------------------------------------
# Backward compatibility: no conflict_resolution key = hold
# ---------------------------------------------------------------------------

class TestTieredConflictBackwardCompat:
    """Existing configs without conflict_resolution should behave identically."""

    def test_no_conflict_key_means_hold(self):
        cfg = _make_config()
        del cfg["conflict_resolution"]
        strat = TieredEnsembleStrategy(cfg)
        state = _make_state(position=1, side=1)
        orders = strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.0, 0.80, state)
        assert all(o.action == "HOLD" for o in orders)
