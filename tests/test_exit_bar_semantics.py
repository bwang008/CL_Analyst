"""
TDD-TESTER AUTHORIZATION
Target Implementation File: agent/backtest_engine.py
Target Class/Function: BacktestEngine._on_in_position (same-bar exit precedence)
Secondary Targets: src/live_execution/strategies/configurable_strategy.py (on_exit reset value),
                   src/live_execution/live_trader.py (_on_new_bar exit-bar evaluation),
                   scripts/livetest_engine.py (deferred-callback flush ordering)
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)
Ticket: bb-f-exit-bar-semantics_07032026_2045

HUMAN AUTHORIZATION (2026-07-03): "Authorize B(b)+F: backtest adopts live's
intrabar SL/TP-before-barrier precedence, live adopts backtest's exit-bar
evaluation semantics, accepting that historical backtest metrics shift and
ensembles may need re-scoring."

B(b) — same-bar exit precedence. IBKR reality: resting SL/TP orders fill
INTRABAR; the time-barrier check runs at bar close only if still in position.
The backtest must therefore evaluate TP/SL breach BEFORE the time barrier on
the barrier bar. Pessimistic SL-wins-over-TP on the same bar is preserved.

re-adjudicated: cooldown-single-authority-wiring_07222026_1051 — the cooldown
gate is now flavor-blind per-side cooldown_bars (the flavored
sl/tp_cooldown_bars vocabulary is dead; the engine dropped it in 3d95040).
Config shapes below re-expressed; counter semantics (reset -1, exit-bar
reads 0, release at exit+N+1) are UNCHANGED.

F — exit-bar evaluation semantics. The backtest reads counter value 0 on the
exit bar (blocked for any cooldown >= 0) and releases at exit+N+1 reading N+1.
With the exit-bar evaluation now always running in live (F3) and its pre-gate
increment, ConfigurableStrategy.on_exit must reset the exited side to -1 so
the exit-bar evaluate reads 0 (F1). The harness must deliver exit-fill
callbacks BEFORE the bar's evaluation (F2), and LiveTrader._on_new_bar must
NOT skip evaluation on time-barrier exit bars (F3).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from agent.backtest_engine import BacktestEngine, ExitReason, TradeState
from src.live_execution.strategies.configurable_strategy import ConfigurableStrategy


REPO_ROOT = Path(__file__).resolve().parents[1]

ENTRY_PRICE = 70.003
ATR = 0.5537
TICK = 0.01
ENTRY_DT = pd.Timestamp("2026-05-26 06:00:00")
# With tp_atr_mult=2.0 / sl_atr_mult=1.0 and B(a) fill-basis rounding:
TP = 71.12   # round(70.013 + 2*0.5537, 2)
SL = 69.46   # round(70.013 - 1*0.5537, 2)


def _make_engine(**overrides) -> BacktestEngine:
    kwargs = dict(
        tp_atr_mult=2.0,
        sl_atr_mult=1.0,
        slippage_per_side=TICK,
        commission_per_side=2.50,
        contract_multiplier=1000.0,
        max_horizon=2,
    )
    kwargs.update(overrides)
    engine = BacktestEngine(**kwargs)
    engine._reset_state()  # initialize FSM/ledger state as the run loop does
    return engine


def _bar(close: float) -> SimpleNamespace:
    return SimpleNamespace(
        exec_Close=close, exec_High=close + 0.05, exec_Low=close - 0.05,
    )


def _enter_long_and_ride_to_barrier(engine: BacktestEngine) -> pd.Timestamp:
    """Enter LONG then hold through max_horizon neutral bars; returns the
    timestamp for the barrier bar (bars_held will exceed the horizon there)."""
    engine._on_flat(ENTRY_DT, _bar(ENTRY_PRICE), signal_side=1, atr=ATR)
    assert engine._state == TradeState.IN_POSITION
    dt = ENTRY_DT
    for _ in range(engine.max_horizon):
        dt = dt + pd.Timedelta(hours=1)
        engine._on_in_position(dt, 70.00, 70.10, 69.90)  # no TP/SL breach
        assert engine._state == TradeState.IN_POSITION
    return dt + pd.Timedelta(hours=1)


# ---------------------------------------------------------------------------
# B(b) — backtest same-bar precedence: SL/TP fill beats the time barrier
# ---------------------------------------------------------------------------


class TestSameBarExitPrecedence:
    def test_tp_beats_time_barrier_on_same_bar(self):
        """Barrier bar whose high reaches TP must exit TP at the TP price —
        NOT TIME_BARRIER at bar open (live/IBKR fills the resting TP intrabar
        before any bar-close barrier check)."""
        engine = _make_engine()
        barrier_dt = _enter_long_and_ride_to_barrier(engine)

        engine._on_in_position(barrier_dt, 70.50, TP + 0.40, 70.30)

        trade = engine._trades[-1]
        assert trade.exit_reason == ExitReason.TP, (
            f"same-bar TP + barrier must exit TP (intrabar fill first); "
            f"got {trade.exit_reason}"
        )
        assert trade.exit_price == pytest.approx(TP, abs=1e-9)

    def test_sl_beats_time_barrier_on_same_bar(self):
        """Barrier bar whose low reaches SL must exit SL — not TIME_BARRIER."""
        engine = _make_engine()
        barrier_dt = _enter_long_and_ride_to_barrier(engine)

        engine._on_in_position(barrier_dt, 69.80, 69.90, SL - 0.30)

        trade = engine._trades[-1]
        assert trade.exit_reason == ExitReason.SL, (
            f"same-bar SL + barrier must exit SL (intrabar fill first); "
            f"got {trade.exit_reason}"
        )
        assert trade.exit_price == pytest.approx(SL, abs=1e-9)

    def test_time_barrier_still_fires_when_no_breach(self):
        """Preservation: a barrier bar with no TP/SL breach exits TIME_BARRIER
        at bar open, exactly as before."""
        engine = _make_engine()
        barrier_dt = _enter_long_and_ride_to_barrier(engine)

        engine._on_in_position(barrier_dt, 70.20, 70.30, 70.10)

        trade = engine._trades[-1]
        assert trade.exit_reason == ExitReason.TIME_BARRIER
        assert trade.exit_price == pytest.approx(70.20, abs=1e-9)

    def test_sl_still_beats_tp_on_same_bar(self):
        """Preservation: pessimistic same-bar SL-over-TP is unchanged, and it
        also applies on the barrier bar."""
        engine = _make_engine()
        barrier_dt = _enter_long_and_ride_to_barrier(engine)

        engine._on_in_position(barrier_dt, 70.00, TP + 0.10, SL - 0.10)

        trade = engine._trades[-1]
        assert trade.exit_reason == ExitReason.SL


# ---------------------------------------------------------------------------
# F(1) — ConfigurableStrategy.on_exit resets to -1 (exit-bar reads 0 like BT)
# ---------------------------------------------------------------------------


def _make_strategy(config: dict) -> ConfigurableStrategy:
    strat = object.__new__(ConfigurableStrategy)
    strat.config = config
    strat._nickname = "ExitBarSemanticsTest"
    strat._direction = "BOTH"
    strat.allow_concurrent = False
    strat._is_tiered = True
    strat._is_ensemble = False
    strat._long_learner = object()
    strat._short_learner = object()
    long_sentinel = strat._long_learner
    strat._run_inference = (
        lambda learner, features: 0.90 if learner is long_sentinel else 0.80
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
    exec_strategy = MagicMock()
    from src.live_execution.strategies.execution_models import Order
    exec_strategy.on_bar.return_value = [Order(action="HOLD", side=0, lots=0, reason="no_signal")]
    strat._exec_strategy = exec_strategy
    return strat


CFG_CD0 = {
    "nickname": "cd0",
    "long": {"cooldown_bars": 0},
    "short": {"cooldown_bars": 0},
}

CFG_CD1 = {
    "nickname": "cd1",
    "long": {"cooldown_bars": 1},
    "short": {"cooldown_bars": 1},
}


class TestOnExitResetValue:
    @pytest.mark.parametrize("reason", ["SL_HIT", "TP_HIT", "TIME_BARRIER", "CLOSED_OOB"])
    def test_on_exit_resets_counter_to_minus_one(self, reason):
        strat = _make_strategy(CFG_CD0)
        strat.on_exit(1, reason, 5)
        assert strat._last_exit_bars_ago_long == -1, (
            "on_exit must reset to -1 so the exit-bar evaluate's pre-gate "
            "increment yields 0 — the value the backtest gate reads on the "
            "exit bar"
        )

    def test_exit_bar_blocked_even_with_cooldown_zero(self):
        """Backtest convention: the exit bar itself is ALWAYS blocked
        (counter 0 <= cooldown 0); release is the next bar (reads 1 > 0)."""
        strat = _make_strategy(CFG_CD0)
        strat.on_exit(1, "TP_HIT", 5)

        sig1 = strat.evaluate(pd.DataFrame(), 70.0, 0.5, 0)  # exit bar
        sig2 = strat.evaluate(pd.DataFrame(), 70.0, 0.5, 0)  # exit bar + 1

        assert sig1.buy_prob == 0.0, (
            "exit-bar evaluate must read 0 and block (0 <= cooldown 0) — no "
            "same-bar re-entry after a TP with cooldown_bars=0"
        )
        assert sig2.buy_prob > 0.0, "next bar reads 1 > 0 and must release"

    def test_cooldown_one_releases_on_third_call(self):
        """cd=1: exit-bar reads 0 (blocked), +1 reads 1 (blocked), +2 reads 2
        (released) — matching the backtest's exit+2 release for cooldown 1."""
        strat = _make_strategy(CFG_CD1)
        strat.on_exit(1, "SL_HIT", 5)

        probs = [strat.evaluate(pd.DataFrame(), 70.0, 0.5, 0).buy_prob for _ in range(3)]

        assert probs[0] == 0.0 and probs[1] == 0.0, f"calls 1-2 must be blocked; got {probs}"
        assert probs[2] > 0.0, f"call 3 (reads 2 > 1) must release; got {probs}"

    def test_per_side_cooldown_bars_participates_in_the_gate(self):
        """The backtest enforces exactly ONE cooldown: the flavor-blind
        per-side `cooldown_bars` (TieredEnsemble re-gate reading REAL
        counters, armed by _close_trade for EVERY exit reason — TP included).
        Live's re-gate is sentinel-neutralized, so evaluate()'s gate must
        enforce the same per-side value: after a TP exit with
        long.cooldown_bars=2, re-entry releases only when the counter
        exceeds 2 (exit bar reads 0; released on the 4th call reading 3).
        (re-adjudicated: cooldown-single-authority-wiring_07222026_1051)"""
        cfg = {
            "nickname": "per_side",
            "long": {"cooldown_bars": 2},
            "short": {"cooldown_bars": 2},
        }
        strat = _make_strategy(cfg)
        strat.on_exit(1, "TP_HIT", 5)  # TP exits arm cooldown_bars too

        probs = [strat.evaluate(pd.DataFrame(), 70.0, 0.5, 0).buy_prob for _ in range(4)]

        assert probs[0] == 0.0, f"exit bar (reads 0) must be blocked; got {probs}"
        assert probs[1] == 0.0, (
            f"exit+1 (reads 1 <= cooldown_bars 2) must be blocked — the "
            f"backtest's TieredEnsemble re-gate blocks it with the real "
            f"counter; got {probs}"
        )
        assert probs[2] == 0.0, f"exit+2 (reads 2 <= 2) must be blocked; got {probs}"
        assert probs[3] > 0.0, f"exit+3 (reads 3 > 2) must release; got {probs}"


# ---------------------------------------------------------------------------
# F(3) — live evaluates the exit bar after a time-barrier exit
# ---------------------------------------------------------------------------


@patch('src.live_execution.live_trader.build_live_features')
def test_time_barrier_exit_bar_still_evaluates_signal(mock_build_live_features):
    """When _check_time_barrier exits the position, _on_new_bar must CONTINUE
    to signal evaluation on that same bar (backtest evaluates every bar,
    including exit bars) instead of returning early."""
    from src.live_execution.live_trader import LiveTrader

    trader = LiveTrader.__new__(LiveTrader)
    trader.exec_client = MagicMock()
    trader.exec_client.get_account_summary.return_value = {
        "cl_unrealized_pnl": 0.0, "cl_avg_cost": 70000.0,
    }
    trader.exec_client.get_position.return_value = 0  # flat after barrier exit
    trader.strategy = MagicMock()
    trader.strategy.evaluate.return_value = MagicMock(
        action="HOLD", buy_prob=0.0, sell_prob=0.0,
    )
    trader.telemetry = MagicMock()
    trader.data_manager_1h = MagicMock()
    trader._open_orders = {}
    trader._tp_order_ids = []
    trader._sl_order_id = None
    trader._max_position_size = 1
    trader._position_bars_held = 0
    trader._data_mute = False
    trader._virtual_ledger = {"5m": 0, "1h": 0}
    trader._last_virtual_ledger_log = ""
    trader._position_side = 0
    trader._execution_symbol = "CL"
    trader._emergency_halt = False
    trader._check_trailing_stop = MagicMock()
    trader._front_month_last_close = 70.0
    trader._atr_period_long = 14
    trader._atr_period_short = 14
    trader._atr_period = 14
    trader._rollover_in_progress = False
    trader._check_time_barrier = MagicMock(return_value=True)  # barrier EXITED
    trader._pending_entry_order_id = None
    trader._needs_macro = False
    trader.feature_names = ["Open", "High", "Low", "Close", "Volume"]
    trader._lean_features = False
    trader._front_month_str = "202607"

    features = pd.DataFrame(
        [{"Open": 70.0, "High": 70.0, "Low": 70.0, "Close": 70.0, "Volume": 100}]
    )
    mock_build_live_features.return_value = features

    trader._on_new_bar(
        bar_time=pd.Timestamp("2026-05-26 13:00:20", tz="UTC"),
        rolling_df=features,
        stream="1h",
    )

    assert trader.strategy.evaluate.called, (
        "_on_new_bar returned early after the time-barrier exit — the exit "
        "bar must still be evaluated (backtest convention; consecutive-signal "
        "and opposite-side entry parity depend on it)"
    )


# ---------------------------------------------------------------------------
# F(2) — harness delivers exit-fill callbacks BEFORE the bar's evaluation
# ---------------------------------------------------------------------------


def test_harness_flushes_exit_fills_before_evaluation():
    """run_simulation must flush deferred callbacks between the bar feed
    (which matches resting TP/SL) and updateEvent.fire (the evaluation), so
    on_exit/cooldown state is current when the exit bar is evaluated —
    mirroring production's real-time fill callbacks."""
    import importlib.util
    src = (REPO_ROOT / "scripts" / "livetest_engine.py").read_text(
        encoding="utf-8", errors="replace"
    )
    idx_feed = src.index("sim_exec.on_bar_feed(")
    idx_fire = src.index("updateEvent.fire(", idx_feed)
    between = src[idx_feed:idx_fire]
    assert "flush_deferred_callbacks()" in between, (
        "no flush_deferred_callbacks() between on_bar_feed and "
        "updateEvent.fire — exit fills are delivered AFTER the exit-bar "
        "evaluation, so evaluate() sees a flat sim position with stale "
        "cooldown counters"
    )
    # The post-fire flush (same-bar ENTRY fills placed during evaluation)
    # must be preserved.
    after_fire = src[idx_fire:]
    assert "flush_deferred_callbacks()" in after_fire, (
        "the post-evaluation flush (same-bar entry fills) must be kept"
    )
