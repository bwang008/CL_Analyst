"""Unit tests for conflict_resolution='close_existing_position_if_own_weak'.

The mode flattens an open position only when BOTH conditions hold on a bar:
  1. The opposite side's RAW probability clears its tier threshold
     (entry gates — consecutive counters, cooldown — deliberately bypassed:
     they are entry filters and the consecutive counters freeze in-position).
  2. The own side's probability has collapsed to <= the per-side
     ``weak_prob_exit_threshold`` floor (computed offline, e.g.
     mean - 1*std of the model's OOS probability distribution).

Config floors are REQUIRED for every side with entry tiers — no silent
defaults (missing floor = ValueError at construction).
"""

import pytest
from src.live_execution.strategies.execution_models import (
    TieredEnsembleStrategy,
    EngineState,
)

MODE = "close_existing_position_if_own_weak"


def _make_config(conflict_resolution=MODE, long_floor=0.30, short_floor=0.25,
                 **overrides):
    """Minimal TieredEnsembleStrategy config with weak floors."""
    cfg = {
        "execution_class": "TieredEnsembleStrategy",
        "nickname": "test_weak_own_prob_exit",
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
    if long_floor is not None:
        cfg["long"]["weak_prob_exit_threshold"] = long_floor
    if short_floor is not None:
        cfg["short"]["weak_prob_exit_threshold"] = short_floor
    cfg.update(overrides)
    return cfg


def _make_state(**overrides):
    state = EngineState()
    for k, v in overrides.items():
        setattr(state, k, v)
    return state


def _on_bar(strat, prob_buy, prob_sell, state):
    return strat.on_bar(None, 70.0, 71.0, 69.0, 70.5, 0.5,
                        prob_buy, prob_sell, state)


# ---------------------------------------------------------------------------
# Config validation — no silent defaults
# ---------------------------------------------------------------------------

class TestWeakFloorConfigValidation:

    def test_mode_accepted_with_both_floors(self):
        strat = TieredEnsembleStrategy(_make_config())
        assert strat.conflict_resolution == MODE
        assert strat.long_weak_prob_exit == 0.30
        assert strat.short_weak_prob_exit == 0.25

    def test_missing_long_floor_raises(self):
        with pytest.raises(ValueError, match="long.weak_prob_exit_threshold"):
            TieredEnsembleStrategy(_make_config(long_floor=None))

    def test_missing_short_floor_raises(self):
        with pytest.raises(ValueError, match="short.weak_prob_exit_threshold"):
            TieredEnsembleStrategy(_make_config(short_floor=None))

    def test_side_without_tiers_needs_no_floor(self):
        """A long-only config (no short tiers) may omit the short floor."""
        cfg = _make_config(short_floor=None)
        cfg["short"] = {"tiers": []}
        strat = TieredEnsembleStrategy(cfg)
        assert strat.short_weak_prob_exit is None

    @pytest.mark.parametrize("bad", [1.5, -0.1, "0.30", True])
    def test_invalid_floor_value_raises(self, bad):
        with pytest.raises(ValueError, match="must be a number"):
            TieredEnsembleStrategy(_make_config(long_floor=bad))

    def test_other_modes_do_not_require_floors(self):
        for mode in ("hold", "close_existing_position", "reverse_position"):
            strat = TieredEnsembleStrategy(
                _make_config(conflict_resolution=mode,
                             long_floor=None, short_floor=None)
            )
            assert strat.long_weak_prob_exit is None
            assert strat.short_weak_prob_exit is None


# ---------------------------------------------------------------------------
# FLAT behavior — entries unchanged
# ---------------------------------------------------------------------------

class TestWeakExitFlatBehavior:

    def test_buy_entry_unchanged(self):
        strat = TieredEnsembleStrategy(_make_config())
        orders = _on_bar(strat, 0.60, 0.0, _make_state())
        assert orders[0].action == "BUY"

    def test_sell_entry_unchanged(self):
        strat = TieredEnsembleStrategy(_make_config())
        orders = _on_bar(strat, 0.0, 0.65, _make_state())
        assert orders[0].action == "SELL"


# ---------------------------------------------------------------------------
# IN POSITION — LONG
# ---------------------------------------------------------------------------

class TestWeakExitInLong:

    def test_opposite_fires_own_healthy_holds(self):
        """Sell triggered but buy prob above floor -> HOLD (AND semantics)."""
        strat = TieredEnsembleStrategy(_make_config())
        state = _make_state(position=1, side=1)
        orders = _on_bar(strat, 0.40, 0.65, state)
        assert all(o.action == "HOLD" for o in orders)

    def test_opposite_fires_own_collapsed_exits(self):
        strat = TieredEnsembleStrategy(_make_config())
        state = _make_state(position=1, side=1)
        orders = _on_bar(strat, 0.20, 0.65, state)
        assert orders[0].action == "EXIT"
        assert orders[0].side == 1
        assert "TIERED_WEAK_EXIT" in orders[0].reason

    def test_own_collapsed_but_opposite_silent_holds(self):
        """Buy prob collapsed but sell below its tier threshold -> HOLD."""
        strat = TieredEnsembleStrategy(_make_config())
        state = _make_state(position=1, side=1)
        orders = _on_bar(strat, 0.10, 0.55, state)  # 0.55 < short 0.60
        assert all(o.action == "HOLD" for o in orders)

    def test_own_prob_exactly_at_floor_exits(self):
        strat = TieredEnsembleStrategy(_make_config())
        state = _make_state(position=1, side=1)
        orders = _on_bar(strat, 0.30, 0.65, state)
        assert orders[0].action == "EXIT"

    def test_opposite_trigger_bypasses_consecutive_gate(self):
        """Raw tier match: consecutive_signal_threshold must not block the
        exit (counters freeze in-position; gating would deadlock)."""
        cfg = _make_config()
        cfg["short"]["consecutive_signal_threshold"] = 3
        strat = TieredEnsembleStrategy(cfg)
        state = _make_state(position=1, side=1)
        orders = _on_bar(strat, 0.20, 0.65, state)
        assert orders[0].action == "EXIT"

    def test_opposite_trigger_bypasses_cooldown_gate(self):
        """Raw tier match: an in-cooldown opposite side still triggers the
        exit (cooldown is an entry filter; live feeds sentinel state)."""
        strat = TieredEnsembleStrategy(_make_config())
        state = _make_state(position=1, side=1,
                            last_exit_bars_ago_short=1)  # within cooldown 5
        orders = _on_bar(strat, 0.20, 0.65, state)
        assert orders[0].action == "EXIT"


# ---------------------------------------------------------------------------
# IN POSITION — SHORT (symmetric)
# ---------------------------------------------------------------------------

class TestWeakExitInShort:

    def test_opposite_fires_own_collapsed_exits(self):
        strat = TieredEnsembleStrategy(_make_config())
        state = _make_state(position=1, side=-1)
        orders = _on_bar(strat, 0.60, 0.10, state)
        assert orders[0].action == "EXIT"
        assert orders[0].side == -1
        assert "TIERED_WEAK_EXIT" in orders[0].reason

    def test_opposite_fires_own_healthy_holds(self):
        strat = TieredEnsembleStrategy(_make_config())
        state = _make_state(position=1, side=-1)
        orders = _on_bar(strat, 0.60, 0.40, state)
        assert all(o.action == "HOLD" for o in orders)

    def test_own_collapsed_but_opposite_silent_holds(self):
        strat = TieredEnsembleStrategy(_make_config())
        state = _make_state(position=1, side=-1)
        orders = _on_bar(strat, 0.50, 0.10, state)  # 0.50 < long 0.55
        assert all(o.action == "HOLD" for o in orders)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestWeakExitEdgeCases:

    def test_side_zero_at_max_concurrent_holds(self):
        """position=0 but open_positions >= max_concurrent (side unknown):
        must HOLD, never crash or emit EXIT."""
        strat = TieredEnsembleStrategy(_make_config())
        state = _make_state(position=0, side=0, open_positions=1)
        orders = _on_bar(strat, 0.20, 0.65, state)
        assert all(o.action == "HOLD" for o in orders)

    def test_nan_probs_hold(self):
        strat = TieredEnsembleStrategy(_make_config())
        state = _make_state(position=1, side=1)
        orders = _on_bar(strat, float("nan"), float("nan"), state)
        assert all(o.action == "HOLD" for o in orders)
