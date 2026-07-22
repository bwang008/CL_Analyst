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
   bar, backtest_engine.py:1200-1201; reset to 0 in _close_trade).
   UPDATED 2026-07-03 under the human-authorized B(b)+F ticket
   (bb-f-exit-bar-semantics_07032026_2045): on_exit now resets the counter
   to -1 because the exit-bar evaluate() always runs and must read 0 —
   exactly what the backtest gate reads on the exit bar. The FIRST
   evaluate() after on_exit IS the exit bar (reads 0, always blocked);
   release happens at exit+N+1 reading N+1 for cooldown N.
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

re-adjudicated: cooldown-single-authority-wiring_07222026_1051 (human-
authorized 2026-07-22): the gate is now FLAVOR-BLIND per-side cooldown_bars
(resolution side_cfg -> top-level -> 0), exactly mirroring the backtest's
TieredEnsemble re-gate — the ONLY cooldown the engine has enforced since
3d95040 (2026-05-12) removed the flavored sl/tp_cooldown_bars params. The
old flavored-union gate made live stricter than the backtest wherever the
hand-template sl_cooldown_bars=7 exceeded the Optuna-searched per-side
value. Config shapes below re-expressed with per-side cooldown_bars only;
counter-increment semantics, sentinel neutralization, and per-side advance
rules are UNCHANGED from the B(b)+F convention.
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


# Fleet-style key shapes (re-adjudicated: cooldown-single-authority-
# wiring_07222026_1051): per-side cooldown_bars is the ONLY cooldown key —
# it drives both evaluate()'s gate (live) and TieredEnsembleStrategy's
# re-gate (backtest).
CFG_CD1 = {
    "nickname": "cd1",
    "long": {"cooldown_bars": 1},
    "short": {"cooldown_bars": 1},
}

CFG_CD7_SHORT = {
    "nickname": "cd7_short",
    "long": {"cooldown_bars": 1},
    "short": {"cooldown_bars": 7},
}

CFG_TIERED_FLEET_LIKE = {
    "nickname": "fleet_like",
    "execution_class": "TieredEnsembleStrategy",
    "conflict_resolution": "hold",
    "long": {"cooldown_bars": 1, "tiers": [{"min_prob": 0.99, "lots": 1}]},
    "short": {"cooldown_bars": 7, "tiers": [{"min_prob": 0.55, "lots": 1}]},
}

CFG_REGATE_ONLY_DOWNSTREAM = {
    "nickname": "regate",
    "execution_class": "TieredEnsembleStrategy",
    "conflict_resolution": "hold",
    "long": {"cooldown_bars": 5, "tiers": [{"min_prob": 0.99, "lots": 1}]},
    "short": {"cooldown_bars": 5, "tiers": [{"min_prob": 0.55, "lots": 1}]},
}


# ---------------------------------------------------------------------------
# 1. Counter increments BEFORE the gate (backtest start-of-bar convention)
# ---------------------------------------------------------------------------


class TestCounterIncrementsBeforeGate:
    def test_side_released_on_third_call_after_exit_with_cooldown_1(self):
        """cooldown=1 (B(b)+F convention): exit-bar call reads 0 (blocked),
        +1 reads 1 (blocked, 1 <= 1), +2 reads 2 (RELEASED).

        Backtest convention: counter reset on exit, incremented at the START
        of each bar before the gate reads it — the exit bar itself reads 0.
        on_exit resets to -1 so the exit-bar evaluate()'s pre-gate increment
        yields exactly that 0.
        """
        strat = _make_strategy(CFG_CD1)
        strat.on_exit(1, "SL_HIT", 5)  # LONG exit -> long counter reset to -1
        assert strat._last_exit_bars_ago_long == -1

        # Call 1 (the exit bar): counter advances -1 -> 0, gate reads 0 <= 1 -> blocked
        sig1 = _evaluate(strat)
        assert strat._last_exit_bars_ago_long == 0
        assert sig1.buy_prob == 0.0, "exit-bar call must be gated (reads 0)"
        assert sig1.sell_prob == pytest.approx(SELL_PROB), (
            "Opposite (short) side must not be gated by a LONG exit"
        )

        # Call 2: reads 1 <= 1 -> still blocked
        sig2 = _evaluate(strat)
        assert strat._last_exit_bars_ago_long == 1
        assert sig2.buy_prob == 0.0, "exit+1 must still be gated (reads 1 <= 1)"

        # Call 3: reads 2 > 1 -> RELEASED
        sig3 = _evaluate(strat)
        assert strat._last_exit_bars_ago_long == 2
        assert sig3.buy_prob == pytest.approx(BUY_PROB), (
            "exit+2 must be released (gate reads post-increment value 2 > "
            "cooldown 1) — backtest exit-bar convention"
        )

    def test_short_release_exact_bar_with_cooldown_bars_7(self):
        """SHORT exit, short.cooldown_bars=7 (B(b)+F convention): the exit-bar
        call plus 7 cooldown bars are gated (reads 0..7), released on call 9.

        Deterministic scripted-bar regression for the SHORT->cooldown-release
        boundary: the first bar the gated side's probability survives
        non-zeroed must be exactly the 9th evaluate() counting from the exit
        bar (counter reads 8 > 7), not one bar earlier or later.
        """
        strat = _make_strategy(CFG_CD7_SHORT)
        strat.on_exit(-1, "SL_HIT", 4)  # SHORT exit -> short counter -1
        assert strat._last_exit_bars_ago_short == -1

        sell_probs = []
        buy_probs = []
        for _ in range(9):
            sig = _evaluate(strat)
            sell_probs.append(sig.sell_prob)
            buy_probs.append(sig.buy_prob)

        # Calls 1..8 (exit bar + 7 cooldown bars): blocked (reads 0..7, each <= 7)
        assert sell_probs[:8] == [0.0] * 8, (
            f"Sell side must be zeroed for the exit bar + 7 bars, got {sell_probs}"
        )
        # Call 9: counter reads 8 > 7 -> released
        assert sell_probs[8] == pytest.approx(SELL_PROB), (
            f"Sell side must be released on the 9th call counting from the "
            f"exit bar (backtest convention); got sell_probs={sell_probs}"
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
        strat = _make_strategy(CFG_CD7_SHORT)
        # Mid-cooldown real counters on both sides
        strat._last_exit_bars_ago_long = 3
        strat._last_exit_bars_ago_short = 5

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
        strat = _make_strategy(CFG_CD7_SHORT)
        strat._last_exit_bars_ago_long = 2
        strat._last_exit_bars_ago_short = 4

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
        """Fleet-like config (short.cooldown_bars=7): after a SHORT SL exit,
        the SELL entry must be emitted on exactly the 9th evaluate() — the
        double-enforcement path (real counter passed into
        TieredEnsembleStrategy) shifts this by one bar.
        """
        exec_strat = TieredEnsembleStrategy(CFG_TIERED_FLEET_LIKE)
        # buy below the 0.99 long tier so only the SHORT side can ever fire
        strat = _make_strategy(
            CFG_TIERED_FLEET_LIKE,
            buy_prob=0.50,
            sell_prob=0.80,
            exec_strategy=exec_strat,
        )
        strat.on_exit(-1, "SL_HIT", 4)

        actions = [_evaluate(strat).action for _ in range(9)]

        assert actions[:8] == ["HOLD"] * 8, (
            f"SHORT side must stay gated for the exit bar + 7 cooldown bars, "
            f"got {actions}"
        )
        assert actions[8] == "SELL", (
            f"SHORT entry must be released on the 9th evaluate counting from "
            f"the exit bar (B(b)+F backtest convention — not one bar earlier "
            f"or later); got actions={actions}"
        )

    def test_cooldown_bars_enforced_once_with_backtest_release_bar(self):
        """Per-side cooldown_bars=5: the backtest enforces it via the
        TieredEnsemble re-gate reading REAL counters; live's sole-authority
        gate enforces the SAME per-side value (re-adjudicated:
        cooldown-single-authority-wiring_07222026_1051). Against the REAL
        TieredEnsembleStrategy (sentinel keeps its re-gate inert), SELL must
        be emitted on exactly the 7th evaluate counting from the exit bar
        (reads 6 > 5) — not earlier, and not later (which would prove double
        enforcement via an un-neutralized re-gate).
        """
        exec_strat = TieredEnsembleStrategy(CFG_REGATE_ONLY_DOWNSTREAM)
        strat = _make_strategy(
            CFG_REGATE_ONLY_DOWNSTREAM,
            buy_prob=0.50,
            sell_prob=0.80,
            exec_strategy=exec_strat,
        )
        strat.on_exit(-1, "SL_HIT", 3)

        actions = [_evaluate(strat).action for _ in range(7)]

        assert actions[:6] == ["HOLD"] * 6, (
            f"SHORT must stay gated for the exit bar + 5 cooldown_bars "
            f"(reads 0..5, each <= 5); got {actions}"
        )
        assert actions[6] == "SELL", (
            f"SHORT must release on the 7th evaluate (reads 6 > 5), matching "
            f"the backtest's cooldown_bars re-gate timeline exactly once; "
            f"got {actions}"
        )


# ---------------------------------------------------------------------------
# 4. Per-side advance semantics preserved (regression guard)
# ---------------------------------------------------------------------------


class TestPerSideAdvanceSemanticsPreserved:
    def test_flat_advances_both_long_only_short_short_only_long(self):
        strat = _make_strategy(CFG_CD7_SHORT)
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
