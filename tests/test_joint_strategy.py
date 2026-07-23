"""Unit tests for JointPortfolioStrategy.

Tests the three conflict resolution modes: ignore_both,
close_existing_position, and reverse_position.
"""

import pytest
from src.live_execution.strategies.execution_models import (
    JointPortfolioStrategy,
    EngineState,
)


def _make_config(conflict_resolution="close_existing_position", **overrides):
    """Build a minimal JointPortfolioStrategy config."""
    cfg = {
        "execution_class": "JointPortfolioStrategy",
        "nickname": "test_joint",
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


class TestJointConfigValidation:
    """Test config parsing and validation."""

    def test_valid_conflict_modes(self):
        for mode in ("ignore_both", "close_existing_position", "reverse_position"):
            strat = JointPortfolioStrategy(_make_config(mode))
            assert strat.conflict_resolution == mode

    def test_invalid_conflict_mode_raises(self):
        with pytest.raises(ValueError, match="Invalid conflict_resolution"):
            JointPortfolioStrategy(_make_config("flip_and_pray"))


class TestJointFlatBehavior:
    """Test signal evaluation when FLAT."""

    def test_single_buy_signal(self):
        strat = JointPortfolioStrategy(_make_config())
        state = _make_state()
        orders = strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.60, 0.0, state)
        assert orders[0].action == "BUY"

    def test_single_sell_signal(self):
        strat = JointPortfolioStrategy(_make_config())
        state = _make_state()
        orders = strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.0, 0.65, state)
        assert orders[0].action == "SELL"

    def test_conflict_flat_close_existing_higher_prob_wins(self):
        """close_existing_position while flat: higher prob wins."""
        strat = JointPortfolioStrategy(_make_config("close_existing_position"))
        state = _make_state()

        # Buy prob higher
        orders = strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.70, 0.65, state)
        assert orders[0].action == "BUY"

        # Reset state for sell dominance
        strat2 = JointPortfolioStrategy(_make_config("close_existing_position"))
        orders = strat2.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.60, 0.80, state)
        assert orders[0].action == "SELL"

    def test_conflict_flat_reverse_higher_prob_wins(self):
        """reverse_position while flat: higher prob wins."""
        strat = JointPortfolioStrategy(_make_config("reverse_position"))
        state = _make_state()
        orders = strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.70, 0.65, state)
        assert orders[0].action == "BUY"

    def test_conflict_flat_ignore_both_holds(self):
        """ignore_both while flat: both fire → HOLD."""
        strat = JointPortfolioStrategy(_make_config("ignore_both"))
        state = _make_state()
        orders = strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.70, 0.65, state)
        assert all(o.action == "HOLD" for o in orders)


class TestJointInPositionIgnoreBoth:
    """Test ignore_both mode while in position."""

    def test_opposite_signal_ignored(self):
        strat = JointPortfolioStrategy(_make_config("ignore_both"))
        state = _make_state(position=1, side=1)  # In a long
        orders = strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.0, 0.80, state)
        assert all(o.action == "HOLD" for o in orders)

    def test_same_side_signal_ignored(self):
        strat = JointPortfolioStrategy(_make_config("ignore_both"))
        state = _make_state(position=1, side=1)  # In a long
        orders = strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.80, 0.0, state)
        assert all(o.action == "HOLD" for o in orders)


class TestJointInPositionCloseExisting:
    """Test close_existing_position mode while in position."""

    def test_opposite_signal_exits(self):
        strat = JointPortfolioStrategy(_make_config("close_existing_position"))
        state = _make_state(position=1, side=1)  # In a long
        orders = strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.0, 0.65, state)
        assert len(orders) == 1
        assert orders[0].action == "EXIT"
        assert orders[0].side == 1  # exiting the long

    def test_same_side_signal_holds(self):
        strat = JointPortfolioStrategy(_make_config("close_existing_position"))
        state = _make_state(position=1, side=1)  # In a long
        orders = strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.60, 0.0, state)
        assert all(o.action == "HOLD" for o in orders)

    def test_no_signal_holds(self):
        strat = JointPortfolioStrategy(_make_config("close_existing_position"))
        state = _make_state(position=1, side=1)
        orders = strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.0, 0.0, state)
        assert all(o.action == "HOLD" for o in orders)

    def test_short_position_buy_signal_exits(self):
        strat = JointPortfolioStrategy(_make_config("close_existing_position"))
        state = _make_state(position=-1, side=-1)  # In a short
        orders = strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.60, 0.0, state)
        assert len(orders) == 1
        assert orders[0].action == "EXIT"
        assert orders[0].side == -1  # exiting the short


class TestJointInPositionReverse:
    """Test reverse_position mode while in position."""

    def test_opposite_signal_reverses(self):
        strat = JointPortfolioStrategy(_make_config("reverse_position"))
        state = _make_state(position=1, side=1)  # In a long
        orders = strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.0, 0.65, state)
        assert len(orders) == 2
        assert orders[0].action == "EXIT"
        assert orders[0].side == 1  # exit the long
        assert orders[1].action == "SELL"
        assert orders[1].side == -1  # enter short

    def test_short_to_long_reversal(self):
        strat = JointPortfolioStrategy(_make_config("reverse_position"))
        state = _make_state(position=-1, side=-1)  # In a short
        orders = strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.60, 0.0, state)
        assert len(orders) == 2
        assert orders[0].action == "EXIT"
        assert orders[0].side == -1  # exit the short
        assert orders[1].action == "BUY"
        assert orders[1].side == 1  # enter long

    def test_same_side_signal_holds(self):
        strat = JointPortfolioStrategy(_make_config("reverse_position"))
        state = _make_state(position=1, side=1)  # In a long
        orders = strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.60, 0.0, state)
        assert all(o.action == "HOLD" for o in orders)


class TestJointCooldownAndConsecutive:
    """Test that cooldowns and consecutive thresholds work."""

    def test_cooldown_blocks_entry(self):
        strat = JointPortfolioStrategy(_make_config())
        state = _make_state()

        # Simulate long exit
        strat.on_exit(1, "SL", 5)

        # Next bar: long is in cooldown
        orders = strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.60, 0.0, state)
        assert all(o.action == "HOLD" for o in orders)

    def test_consecutive_threshold(self):
        cfg = _make_config()
        cfg["long"]["consecutive_signal_threshold"] = 2
        strat = JointPortfolioStrategy(cfg)
        state = _make_state()

        # Bar 1: suppressed
        orders = strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.60, 0.0, state)
        assert all(o.action == "HOLD" for o in orders)

        # Bar 2: fires
        orders = strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.60, 0.0, state)
        assert orders[0].action == "BUY"


class TestJointApplyTrialParams:
    """Test optimizer parameter routing."""

    def test_conflict_resolution_routed(self):
        strat = JointPortfolioStrategy(_make_config())
        import copy
        cfg = copy.deepcopy(_make_config())
        params = {"conflict_resolution": "reverse_position"}
        result = strat.apply_trial_params(cfg, params)
        assert result["conflict_resolution"] == "reverse_position"

    def test_threshold_routed_to_tiers(self):
        strat = JointPortfolioStrategy(_make_config())
        import copy
        cfg = copy.deepcopy(_make_config())
        params = {"entry_threshold": 0.70, "tp_atr_mult": 4.0}
        result = strat.apply_trial_params(cfg, params, side="long")
        assert result["long"]["tiers"][0]["min_prob"] == 0.70
        assert result["long"]["tiers"][0]["tp_atr_mult"] == 4.0

    def test_on_exit_tracks_state(self):
        strat = JointPortfolioStrategy(_make_config())
        strat._current_side = 1

        # TP close: clears the side but does NOT arm the cooldown counter
        # (trailing-sl-no-cooldown_07222026_2050 — only an original SL arms).
        strat.on_exit(1, "TP", 10)
        assert strat._current_side == 0
        assert strat._bars_since_long_exit == 9999

        # An SL close arms it
        strat._current_side = 1
        strat.on_exit(1, "SL", 10)
        assert strat._current_side == 0
        assert strat._bars_since_long_exit == 0

        # Short cooldown not affected
        assert strat._bars_since_short_exit == 9999
