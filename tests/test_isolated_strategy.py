"""Unit tests for IsolatedAsymmetricalStrategy.

Tests that long and short sides operate independently with their own
cooldowns, consecutive signal tracking, and concurrent positions.
"""

import pytest
from src.live_execution.strategies.execution_models import (
    IsolatedAsymmetricalStrategy,
    EngineState,
)


def _make_config(**overrides):
    """Build a minimal IsolatedAsymmetricalStrategy config."""
    cfg = {
        "execution_class": "IsolatedAsymmetricalStrategy",
        "nickname": "test_isolated",
        "allow_concurrent": True,
        "max_concurrent": 2,
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


class TestIsolatedAsymmetricalBasic:
    """Basic signal evaluation tests."""

    def test_buy_signal_above_threshold(self):
        strat = IsolatedAsymmetricalStrategy(_make_config())
        state = _make_state()
        orders = strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.60, 0.0, state)
        assert len(orders) == 1
        assert orders[0].action == "BUY"
        assert orders[0].side == 1

    def test_sell_signal_above_threshold(self):
        strat = IsolatedAsymmetricalStrategy(_make_config())
        state = _make_state()
        orders = strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.0, 0.65, state)
        assert len(orders) == 1
        assert orders[0].action == "SELL"
        assert orders[0].side == -1

    def test_both_signals_fire_simultaneously(self):
        """Both sides should fire independently → 2 orders."""
        strat = IsolatedAsymmetricalStrategy(_make_config())
        state = _make_state()
        orders = strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.60, 0.65, state)
        assert len(orders) == 2
        actions = {o.action for o in orders}
        assert actions == {"BUY", "SELL"}

    def test_below_threshold_hold(self):
        strat = IsolatedAsymmetricalStrategy(_make_config())
        state = _make_state()
        orders = strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.40, 0.50, state)
        assert all(o.action == "HOLD" for o in orders)

    def test_nan_prob_treated_as_zero(self):
        strat = IsolatedAsymmetricalStrategy(_make_config())
        state = _make_state()
        orders = strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, float("nan"), float("nan"), state)
        assert all(o.action == "HOLD" for o in orders)


class TestIsolatedIndependentPositions:
    """Test that each side tracks open positions independently."""

    def test_long_open_does_not_block_short(self):
        strat = IsolatedAsymmetricalStrategy(_make_config())
        state = _make_state()

        # Long enters
        orders = strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.60, 0.0, state)
        assert orders[0].action == "BUY"
        assert strat._long_is_open is True

        # Short should still be able to enter
        orders = strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.0, 0.65, state)
        assert orders[0].action == "SELL"
        assert strat._short_is_open is True

    def test_long_open_blocks_second_long(self):
        strat = IsolatedAsymmetricalStrategy(_make_config())
        state = _make_state()

        # First long enters
        strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.60, 0.0, state)
        assert strat._long_is_open is True

        # Second long is blocked
        orders = strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.60, 0.0, state)
        assert all(o.action == "HOLD" for o in orders)

    def test_on_exit_frees_position(self):
        strat = IsolatedAsymmetricalStrategy(_make_config())
        state = _make_state()

        # Long enters
        strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.60, 0.0, state)
        assert strat._long_is_open is True

        # Engine closes the long via TP: frees the slot but does NOT arm the
        # cooldown counter (trailing-sl-no-cooldown_07222026_2050 — only an
        # original SL arms).
        strat.on_exit(1, "TP", 10)
        assert strat._long_is_open is False
        assert strat._bars_since_long_exit >= 9999  # TP must not arm

        # An SL close arms the counter
        strat._long_is_open = True
        strat.on_exit(1, "SL", 10)
        assert strat._long_is_open is False
        assert strat._bars_since_long_exit == 0

        # Short should remain unaffected
        assert strat._short_is_open is False
        assert strat._bars_since_short_exit >= 9999  # unaffected by long exit


class TestIsolatedCooldownIndependence:
    """Test that cooldowns are per-side and independent."""

    def test_long_cooldown_does_not_affect_short(self):
        strat = IsolatedAsymmetricalStrategy(_make_config())
        state = _make_state()

        # Long enters, then exits
        strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.60, 0.0, state)
        strat.on_exit(1, "SL", 5)

        # Next bar: long is in cooldown (3 bars), short is free
        orders = strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.60, 0.65, state)

        # Only short should fire (long in cooldown)
        assert len(orders) == 1
        assert orders[0].action == "SELL"

    def test_cooldown_expires(self):
        strat = IsolatedAsymmetricalStrategy(_make_config())
        state = _make_state()

        # Long exits
        strat._long_is_open = False
        strat._bars_since_long_exit = 0

        # Advance through cooldown (3 bars) by calling on_bar with no signals
        for _ in range(4):
            strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.0, 0.0, state)

        # Now long should be available
        orders = strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.60, 0.0, state)
        assert orders[0].action == "BUY"


class TestIsolatedConsecutiveSignals:
    """Test per-side consecutive signal thresholds."""

    def test_consecutive_threshold_suppresses_until_met(self):
        cfg = _make_config()
        cfg["long"]["consecutive_signal_threshold"] = 3
        strat = IsolatedAsymmetricalStrategy(cfg)
        state = _make_state()

        # Bars 1-2: signal present but below consecutive threshold
        for _ in range(2):
            orders = strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.60, 0.0, state)
            assert all(o.action == "HOLD" for o in orders)

        # Bar 3: threshold met → BUY fires
        orders = strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.60, 0.0, state)
        assert orders[0].action == "BUY"

    def test_consecutive_reset_on_gap(self):
        cfg = _make_config()
        cfg["long"]["consecutive_signal_threshold"] = 3
        strat = IsolatedAsymmetricalStrategy(cfg)
        state = _make_state()

        # 2 consecutive signals
        strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.60, 0.0, state)
        strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.60, 0.0, state)

        # Gap — no signal
        strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.40, 0.0, state)

        # Counter reset; 2 more needed
        orders = strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.60, 0.0, state)
        assert all(o.action == "HOLD" for o in orders)

    def test_long_consecutive_independent_of_short(self):
        cfg = _make_config()
        cfg["long"]["consecutive_signal_threshold"] = 2
        cfg["short"]["consecutive_signal_threshold"] = 0  # immediate
        strat = IsolatedAsymmetricalStrategy(cfg)
        state = _make_state()

        # Bar 1: short fires immediately, long suppressed
        orders = strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.60, 0.65, state)
        actions = {o.action for o in orders}
        assert "SELL" in actions
        assert "BUY" not in actions

        # Bar 2: both fire (long consecutive met)
        orders = strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5, 0.60, 0.0, state)
        # Short already open so only long fires
        assert orders[0].action == "BUY"


class TestIsolatedApplyTrialParams:
    """Test that apply_trial_params routes correctly."""

    def test_routes_to_tier_blocks(self):
        strat = IsolatedAsymmetricalStrategy(_make_config())
        import copy
        cfg = copy.deepcopy(_make_config())
        params = {
            "entry_threshold": 0.65,
            "tp_atr_mult": 5.0,
            "sl_atr_mult": 2.0,
        }
        result = strat.apply_trial_params(cfg, params, side="long")
        assert result["long"]["tiers"][0]["min_prob"] == 0.65
        assert result["long"]["tiers"][0]["tp_atr_mult"] == 5.0
        # Short should be untouched
        assert result["short"]["tiers"][0]["min_prob"] == 0.60
