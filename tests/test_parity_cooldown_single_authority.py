"""
TDD-TESTER AUTHORIZATION
Target Implementation File: src/live_execution/strategies/configurable_strategy.py
Target Class/Function: ConfigurableStrategy.evaluate (cooldown gate + EngineState construction)
Secondary Target: scripts/livetest_engine.py (deletion of the _parity_on_exit monkey-patch)
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)
Ticket: parity-exit-signal_07022026_1930

Phenomenon A — ConfigurableStrategy.evaluate() must become the SOLE cooldown
authority, mirroring the backtest convention (agent/backtest_engine.py):

1. The per-side since-exit counter increments at the TOP of the cooldown
   section, BEFORE the prob-zeroing gate (backtest increments at start of
   bar, backtest_engine.py:1200-1201; reset to 0 in _close_trade). After an
   exit resets a side's counter to 0, the FIRST subsequent evaluate() call
   gates that side reading the post-increment value 1; with an effective
   cooldown of 1 the side is still blocked (1 <= 1) and the SECOND call
   reads 2 and releases the side.
2. The EngineState handed to TieredEnsembleStrategy.on_bar must carry the
   neutralizing sentinel last_exit_bars_ago_long == last_exit_bars_ago_short
   == 9999 so the downstream re-gate (execution_models.py:754-760,
   `bars_ago <= cooldown_bars`) can never fire — cooldown enforced in
   exactly ONE place.
3. The per-side advance semantics are preserved (flat: both sides advance;
   long position: only short advances; short position: only long advances).
4. The livetest harness compensating monkey-patch `_parity_on_exit`
   (scripts/livetest_engine.py) is deleted.

Do NOT modify execution_models.py. Deterministic, no I/O, no models, no
network: the strategy object is built via object.__new__ (established stub
pattern, see tests/test_cooldown.py) and inference is stubbed.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

from src.live_execution.strategies.configurable_strategy import ConfigurableStrategy
from src.live_execution.strategies.execution_models import (
    Order,
    TieredEnsembleStrategy,
)


BUY_PROB = 0.90
SELL_PROB = 0.80

SENTINEL = 9999  # neutralizing value for EngineState.last_exit_bars_ago_*


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hold_orders() -> list[Order]:
    return [Order(action="HOLD", side=0, lots=0, reason="no_signal")]


def _make_strategy(
    config: dict,
    *,
    buy_prob: float = BUY_PROB,
    sell_prob: float = SELL_PROB,
    exec_strategy=None,
) -> ConfigurableStrategy:
    """Build a ConfigurableStrategy without touching disk or models.

    Bypasses __init__ (which loads JSON config + LGBM models from disk) and
    sets exactly the attributes evaluate()/on_exit() read. Inference is
    stubbed so probabilities are deterministic constants.
    """
    strat = object.__new__(ConfigurableStrategy)
    strat.config = config
    strat._nickname = "ParityCooldownTest"
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

    # Cooldown state (fresh: no exits yet)
    strat._last_exit_bars_ago_long = 9999
    strat._last_exit_bars_ago_short = 9999
    strat._last_exit_reason_long = ""
    strat._last_exit_reason_short = ""

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
    """One evaluate() call with fixed price/ATR (values are irrelevant to gating)."""
    return strat.evaluate(pd.DataFrame(), 70.0, 0.5, current_position)


# HS14B-style key shapes: top-level sl/tp_cooldown_bars drive evaluate()'s own
# gate; nested long/short cooldown_bars drive TieredEnsembleStrategy's re-gate.
CFG_CD1 = {
    "nickname": "cd1",
    "sl_cooldown_bars": 1,
    "tp_cooldown_bars": 0,
    "long": {"cooldown_bars": 1},
    "short": {"cooldown_bars": 1},
}

CFG_SL7 = {
    "nickname": "sl7",
    "sl_cooldown_bars": 7,
    "tp_cooldown_bars": 0,
    "long": {"cooldown_bars": 1},
    "short": {"cooldown_bars": 1},
}

CFG_TIERED_HS14B_LIKE = {
    "nickname": "hs14b_like",
    "execution_class": "TieredEnsembleStrategy",
    "conflict_resolution": "hold",
    "sl_cooldown_bars": 7,
    "tp_cooldown_bars": 0,
    "long": {"cooldown_bars": 1, "tiers": [{"min_prob": 0.99, "lots": 1}]},
    "short": {"cooldown_bars": 1, "tiers": [{"min_prob": 0.55, "lots": 1}]},
}

CFG_REGATE_ONLY_DOWNSTREAM = {
    "nickname": "regate",
    "execution_class": "TieredEnsembleStrategy",
    "conflict_resolution": "hold",
    "sl_cooldown_bars": 0,
    "tp_cooldown_bars": 0,
    "long": {"cooldown_bars": 5, "tiers": [{"min_prob": 0.99, "lots": 1}]},
    "short": {"cooldown_bars": 5, "tiers": [{"min_prob": 0.55, "lots": 1}]},
}


# ---------------------------------------------------------------------------
# 1. Counter increments BEFORE the gate (backtest start-of-bar convention)
# ---------------------------------------------------------------------------


class TestCounterIncrementsBeforeGate:
    def test_side_released_on_second_call_after_exit_with_cooldown_1(self):
        """cooldown=1: 1st call reads post-increment 1 (blocked), 2nd reads 2 (released).

        Backtest convention: counter reset to 0 on exit (_close_trade), then
        incremented at the START of each bar before the strategy gate reads it.
        Current (buggy) live code increments AFTER the gate, so the gate reads
        0 then 1 and only releases on the THIRD call — one bar late.
        """
        strat = _make_strategy(CFG_CD1)
        strat.on_exit(1, "SL_HIT", 5)  # LONG exit -> long counter reset to 0
        assert strat._last_exit_bars_ago_long == 0

        # Call 1: counter advances 0 -> 1 first, gate reads 1 <= 1 -> blocked
        sig1 = _evaluate(strat)
        assert strat._last_exit_bars_ago_long == 1
        assert sig1.buy_prob == 0.0, "1st call after exit must still be gated"
        assert sig1.sell_prob == pytest.approx(SELL_PROB), (
            "Opposite (short) side must not be gated by a LONG exit"
        )

        # Call 2: counter advances 1 -> 2, gate reads 2 > 1 -> RELEASED
        sig2 = _evaluate(strat)
        assert strat._last_exit_bars_ago_long == 2
        assert sig2.buy_prob == pytest.approx(BUY_PROB), (
            "2nd call after exit must be released (gate reads post-increment "
            "value 2 > cooldown 1) — backtest start-of-bar convention"
        )

    def test_short_release_exact_bar_with_sl_cooldown_7(self):
        """SHORT SL exit, sl_cooldown_bars=7: sell gated on calls 1-7, released on call 8.

        Deterministic scripted-bar regression for the SHORT->cooldown-release
        boundary: the first bar the gated side's probability survives
        non-zeroed must be exactly the 8th evaluate() after the exit
        (counter reads 8 > 7), not one bar earlier or later.
        """
        strat = _make_strategy(CFG_SL7)
        strat.on_exit(-1, "SL_HIT", 4)  # SHORT exit -> short counter 0
        assert strat._last_exit_bars_ago_short == 0

        sell_probs = []
        buy_probs = []
        for _ in range(9):
            sig = _evaluate(strat)
            sell_probs.append(sig.sell_prob)
            buy_probs.append(sig.buy_prob)

        # Calls 1..7: blocked (post-increment counter values 1..7, each <= 7)
        assert sell_probs[:7] == [0.0] * 7, (
            f"Sell side must be zeroed for exactly 7 bars, got {sell_probs}"
        )
        # Call 8: counter reads 8 > 7 -> released
        assert sell_probs[7] == pytest.approx(SELL_PROB), (
            f"Sell side must be released on the 8th call after exit "
            f"(backtest convention); got sell_probs={sell_probs}"
        )
        # The LONG side is never gated by a SHORT exit (per-side counters)
        assert all(bp == pytest.approx(BUY_PROB) for bp in buy_probs), (
            "Follow-on LONG must remain available throughout the SHORT cooldown"
        )


# ---------------------------------------------------------------------------
# 2. Sentinel neutralizes the downstream TieredEnsembleStrategy re-gate
# ---------------------------------------------------------------------------


class TestEngineStateSentinel:
    def test_engine_state_carries_neutralizing_sentinel_when_flat(self):
        """EngineState passed to on_bar must carry 9999/9999, not real counters."""
        strat = _make_strategy(CFG_SL7)
        # Mid-cooldown real counters on both sides
        strat._last_exit_bars_ago_long = 3
        strat._last_exit_reason_long = "SL_HIT"
        strat._last_exit_bars_ago_short = 5
        strat._last_exit_reason_short = "SL_HIT"

        _evaluate(strat, current_position=0)

        state = strat._exec_strategy.on_bar.call_args.kwargs["state"]
        assert state.last_exit_bars_ago_long == SENTINEL, (
            f"EngineState.last_exit_bars_ago_long must be the neutralizing "
            f"sentinel {SENTINEL}, got {state.last_exit_bars_ago_long} — "
            f"cooldown must be enforced ONLY inside evaluate()"
        )
        assert state.last_exit_bars_ago_short == SENTINEL, (
            f"EngineState.last_exit_bars_ago_short must be the neutralizing "
            f"sentinel {SENTINEL}, got {state.last_exit_bars_ago_short}"
        )

    def test_engine_state_carries_sentinel_while_in_position(self):
        """Sentinel must also be fed while holding a position."""
        strat = _make_strategy(CFG_SL7)
        strat._last_exit_bars_ago_long = 2
        strat._last_exit_reason_long = "SL_HIT"
        strat._last_exit_bars_ago_short = 4
        strat._last_exit_reason_short = "TP_HIT"

        _evaluate(strat, current_position=1)

        state = strat._exec_strategy.on_bar.call_args.kwargs["state"]
        assert state.last_exit_bars_ago_long == SENTINEL
        assert state.last_exit_bars_ago_short == SENTINEL


# ---------------------------------------------------------------------------
# 3. Boundary parity against the REAL TieredEnsembleStrategy (no mocks on the
#    downstream re-gate — proves single-authority end to end)
# ---------------------------------------------------------------------------


class TestSingleCooldownAuthorityEndToEnd:
    def test_short_boundary_release_emits_sell_on_exact_bar(self):
        """HS14B-like config (sl_cooldown_bars=7, per-side cooldown_bars=1):
        after a SHORT SL exit, the SELL entry must be emitted on exactly the
        8th evaluate() — the double-enforcement path (real counter passed into
        TieredEnsembleStrategy) shifts this by one bar.
        """
        exec_strat = TieredEnsembleStrategy(CFG_TIERED_HS14B_LIKE)
        # buy below the 0.99 long tier so only the SHORT side can ever fire
        strat = _make_strategy(
            CFG_TIERED_HS14B_LIKE,
            buy_prob=0.50,
            sell_prob=0.80,
            exec_strategy=exec_strat,
        )
        strat.on_exit(-1, "SL_HIT", 4)

        actions = [_evaluate(strat).action for _ in range(9)]

        assert actions[:7] == ["HOLD"] * 7, (
            f"SHORT side must stay gated for exactly 7 bars, got {actions}"
        )
        assert actions[7] == "SELL", (
            f"SHORT entry must be released on the 8th bar after the exit "
            f"(backtest convention — not one bar earlier or later); "
            f"got actions={actions}"
        )

    def test_downstream_regate_cannot_block_a_released_side(self):
        """evaluate()'s gate says GO (sl_cooldown_bars=0) while the nested
        per-side cooldown_bars=5 would block via the EngineState re-gate.
        With the sentinel in place the very FIRST evaluate() after the exit
        must emit SELL — a HOLD proves cooldown is still enforced twice.
        """
        exec_strat = TieredEnsembleStrategy(CFG_REGATE_ONLY_DOWNSTREAM)
        strat = _make_strategy(
            CFG_REGATE_ONLY_DOWNSTREAM,
            buy_prob=0.50,
            sell_prob=0.80,
            exec_strategy=exec_strat,
        )
        strat.on_exit(-1, "SL_HIT", 3)

        sig = _evaluate(strat)
        assert sig.action == "SELL", (
            f"With evaluate() the sole cooldown authority (cooldown 0, counter "
            f"reads 1 > 0) the first bar after exit must enter SHORT; got "
            f"action={sig.action!r} skip_reason={sig.skip_reason!r} — the "
            f"TieredEnsembleStrategy re-gate must be neutralized"
        )


# ---------------------------------------------------------------------------
# 4. Per-side advance semantics preserved (regression guard)
# ---------------------------------------------------------------------------


class TestPerSideAdvanceSemanticsPreserved:
    def test_flat_advances_both_long_only_short_short_only_long(self):
        strat = _make_strategy(CFG_SL7)
        strat._last_exit_bars_ago_long = 100
        strat._last_exit_bars_ago_short = 200

        _evaluate(strat, current_position=0)  # flat: both advance
        assert strat._last_exit_bars_ago_long == 101
        assert strat._last_exit_bars_ago_short == 201

        _evaluate(strat, current_position=2)  # long position: only short advances
        assert strat._last_exit_bars_ago_long == 101
        assert strat._last_exit_bars_ago_short == 202

        _evaluate(strat, current_position=-1)  # short position: only long advances
        assert strat._last_exit_bars_ago_long == 102
        assert strat._last_exit_bars_ago_short == 202


# ---------------------------------------------------------------------------
# 5. Livetest harness compensating monkey-patch must be deleted
# ---------------------------------------------------------------------------


class TestLivetestHarnessMonkeyPatchRemoved:
    def test_parity_on_exit_monkeypatch_deleted_from_livetest_engine(self):
        """Once evaluate() is the sole cooldown authority, the _parity_on_exit
        compensator would re-introduce a one-bar skew — it must be gone.
        (Source scan: the harness is a script, per blueprint this is acceptable.)
        """
        src_path = (
            Path(__file__).resolve().parent.parent / "scripts" / "livetest_engine.py"
        )
        source = src_path.read_text(encoding="utf-8")
        # Boolean flag keeps the failure diff concise (no full-source dump)
        patch_present = "_parity_on_exit" in source
        assert patch_present is False, (
            "scripts/livetest_engine.py still contains the _parity_on_exit "
            "compensating monkey-patch — it must be deleted (Phenomenon A #3)"
        )
