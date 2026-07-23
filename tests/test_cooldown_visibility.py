"""Cooldown visibility on TradeSignal (cooldown-visibility_07222026_2020).

Target: ConfigurableStrategy.evaluate() cooldown gate + TradeSignal fields.

The gate zeroes the gated side's probability post-inference, which renders in
the INFERENCE log line as ``sell_prob=0.0000`` — indistinguishable from a dead
model (operator misread it exactly that way on 2026-07-22). These tests pin the
display-only companion fields:

- ``cooldown_bars_left_<side>`` = number of bars the side is still blocked,
  INCLUDING the current bar (release reads cooldown+1, so
  bars_left = cooldown - bars_ago + 1 at gate time).
- ``None`` whenever the side is not gated (free, in position, or guard path).

The gate's zeroing behavior itself is pinned by
test_parity_cooldown_single_authority.py — nothing here changes it.
"""

from unittest.mock import MagicMock

import pandas as pd
import pytest

from src.live_execution.strategies.configurable_strategy import ConfigurableStrategy
from src.live_execution.strategies.execution_models import Order


BUY_PROB = 0.90
SELL_PROB = 0.80


def _hold_orders() -> list[Order]:
    return [Order(action="HOLD", side=0, lots=0, reason="no_signal")]


def _make_strategy(
    config: dict,
    *,
    buy_prob: float = BUY_PROB,
    sell_prob: float = SELL_PROB,
    exec_strategy=None,
) -> ConfigurableStrategy:
    """Mirror of test_parity_cooldown_single_authority._make_strategy."""
    strat = object.__new__(ConfigurableStrategy)
    strat.config = config
    strat._nickname = "CooldownVisibilityTest"
    strat._direction = "BOTH"
    strat.allow_concurrent = False
    strat._is_tiered = True
    strat._is_ensemble = False

    strat._long_learner = object()
    strat._short_learner = object()
    long_sentinel = strat._long_learner
    strat._run_inference = (
        lambda learner, features: buy_prob if learner is long_sentinel else sell_prob
    )

    strat._execution_guard = None

    strat._last_exit_bars_ago_long = 9999
    strat._last_exit_bars_ago_short = 9999

    strat.exit_mode = "SINGLE"
    strat.tp_atr_mult = 2.0
    strat.sl_atr_mult = 1.0
    strat._long_tiered_exits = None
    strat._short_tiered_exits = None
    strat.base_quantity = 1

    if exec_strategy is None:
        exec_strategy = MagicMock()
        exec_strategy.on_bar.return_value = _hold_orders()
    strat._exec_strategy = exec_strategy
    return strat


def _evaluate(strat: ConfigurableStrategy, current_position: int = 0):
    return strat.evaluate(pd.DataFrame(), 70.0, 0.5, current_position)


CFG = {
    "long": {"cooldown_bars": 1},
    "short": {"cooldown_bars": 11},
}


class TestCooldownBarsLeftFields:
    def test_fresh_strategy_reports_none_for_both_sides(self):
        strat = _make_strategy(CFG)
        sig = _evaluate(strat)
        assert sig.cooldown_bars_left_long is None
        assert sig.cooldown_bars_left_short is None
        assert sig.buy_prob == pytest.approx(BUY_PROB)
        assert sig.sell_prob == pytest.approx(SELL_PROB)

    def test_gated_short_reports_bars_left_counting_down(self):
        strat = _make_strategy(CFG)
        strat.on_exit(-1, "SL_HIT", 5)

        # Exit bar reads bars_ago=0 -> blocked bars incl. current = 11-0+1
        sig = _evaluate(strat)
        assert sig.sell_prob == 0.0
        assert sig.cooldown_bars_left_short == 12
        assert sig.cooldown_bars_left_long is None
        assert sig.buy_prob == pytest.approx(BUY_PROB), (
            "opposite side must stay unmodified and unreported"
        )

        # Next bar reads 1 -> 11-1+1 = 11
        sig = _evaluate(strat)
        assert sig.cooldown_bars_left_short == 11

    def test_release_bar_reports_none_and_raw_prob(self):
        strat = _make_strategy(CFG)
        strat.on_exit(1, "SL_HIT", 3)

        sig1 = _evaluate(strat)  # reads 0 -> blocked, 2 left
        assert sig1.buy_prob == 0.0
        assert sig1.cooldown_bars_left_long == 2
        sig2 = _evaluate(strat)  # reads 1 -> blocked, 1 left
        assert sig2.buy_prob == 0.0
        assert sig2.cooldown_bars_left_long == 1
        sig3 = _evaluate(strat)  # reads 2 > 1 -> free
        assert sig3.buy_prob == pytest.approx(BUY_PROB)
        assert sig3.cooldown_bars_left_long is None

    def test_in_position_reports_none_even_with_recent_exit(self):
        strat = _make_strategy(CFG)
        strat.on_exit(-1, "SL_HIT", 5)
        sig = _evaluate(strat, current_position=1)
        assert sig.cooldown_bars_left_short is None
        assert sig.cooldown_bars_left_long is None
        assert sig.sell_prob == pytest.approx(SELL_PROB), (
            "gate is flat-only; in-position probs must pass through raw"
        )

    def test_entry_signal_carries_opposite_side_cooldown(self):
        # Long entry fires while the short side is still cooling: the BUY
        # signal must still surface the short block for the log line.
        # (Armed via SL_HIT — under trailing-sl-no-cooldown_07222026_2050
        # only an original SL arms the cooldown.)
        exec_strategy = MagicMock()
        exec_strategy.on_bar.return_value = [
            Order(action="BUY", side=1, lots=1, reason="entry")
        ]
        strat = _make_strategy(CFG, exec_strategy=exec_strategy)
        strat.on_exit(-1, "SL_HIT", 4)

        sig = _evaluate(strat)
        assert sig.action == "BUY"
        assert sig.cooldown_bars_left_short == 12
        assert sig.cooldown_bars_left_long is None
