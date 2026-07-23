"""
TDD-TESTER AUTHORIZATION
Target Implementation File: src/live_execution/live_trader.py
Target Class/Function: LiveTrader._check_time_barrier, LiveTrader._on_standard_execution_event
Secondary Targets: src/live_execution/strategies/configurable_strategy.py (cooldown flavor
                   vocabulary), scripts/livetest_engine.py (duplicate child placement removal)
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)
Ticket: exit-fill-routing-cooldown_07032026_0930

Defects D and E from the 2026-07-03 ledger-parity replay (see
.agents/collab/tickets/exit-fill-routing-cooldown_07032026_0930/blueprint.md):

D — exit-reason vocabulary gap:
  * The time-barrier exit must call _reset_position_state(reason="TIME_BARRIER")
    (not the default "CLOSED") — truthful ledger/telemetry vocabulary.
  * The out-of-band close path must pass reason="CLOSED_OOB".
  * re-adjudicated: cooldown-single-authority-wiring_07222026_1051 — the
    cooldown gate is now flavor-blind per-side cooldown_bars (any exit
    reason arms it, matching the backtest's TieredEnsemble re-gate); the
    SL-flavored vocabulary tuple is gone. CLOSED-family reasons still arm
    the per-side cooldown like every other close.

E — fill misrouting:
  * _on_standard_execution_event must NOT treat an unrecognized fill as an
    entry (today the else-branch places bracket children around an exit fill,
    because decision context is also stored under child order IDs).
    Entries must be identified via an explicit registry populated at order
    submission (self._entry_order_ids).
  * scripts/livetest_engine.py must not call trader._place_bracket_children_on_fill
    itself — live_trader's entry branch already places children on fill; the
    harness duplicate overwrote _tp_order_ids/_sl_order_id and orphaned the
    first child set.

Deterministic, no real I/O: LiveTrader/ConfigurableStrategy built via
object.__new__ stub pattern (see tests/test_live_trader_bugs.py,
tests/test_parity_cooldown_single_authority.py).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

from src.live_execution.live_trader import LiveTrader
from src.live_execution.interfaces.execution_interface import StandardExecutionEvent
from src.live_execution.strategies.configurable_strategy import ConfigurableStrategy


REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_trader() -> LiveTrader:
    """Minimal LiveTrader stub with the attributes the tested paths read."""
    t = LiveTrader.__new__(LiveTrader)
    t.exec_client = MagicMock()
    t.telemetry = MagicMock()
    t._telegram = MagicMock()
    t._execution_symbol = "CL"
    t._front_month_str = "202607"
    t._exit_mode = "MKT"
    t._open_orders = {}
    t._processed_exit_order_ids = set()
    t._processed_entry_order_ids = set()
    t._last_decision_context_by_order_id = {}
    t._tp_order_ids = []
    t._sl_order_id = None
    t._active_trade_id = None
    t._position_side = 0
    t._position_bars_held = 0
    t._position_entry_bar_time = None
    t._trade_max_hold_bars = None
    t._max_hold_bars = 240
    t._atr_at_entry = 0.5
    t._trade_trailing_atr_mult = 1.0
    t._pending_entry_order_id = None
    # TIME BARRIER submit-and-defer state (settle-confirm-event-loop_07202026_0713):
    # the re-entrancy guard at the top of _check_time_barrier reads this.
    t._pending_exit_order_id = None
    # re-adjudicated: oca-stage4-exit-ordering_07222026_0155 (retire-then-submit)
    # — mechanical fixture repair only: Stage-4 state the barrier re-entrancy
    # guard, the reconciler retiring branch, and the fill routing now read;
    # plus the retry-budget counter the deferral paths advance.
    t._retiring_leg_ids = []
    t._time_barrier_exit_attempts = 0
    t._build_event_id = MagicMock(return_value="evt-test")
    t._base_tradebook_fields = MagicMock(return_value={})
    return t


def _filled_event(order_id: str, action: str = "SELL", price: float = 88.26) -> StandardExecutionEvent:
    raw_order = MagicMock()
    raw_order.action = action
    raw_order.permId = None
    raw_order.parentId = None
    raw_order.account = None
    raw = MagicMock()
    raw.order = raw_order
    raw.contract = MagicMock(symbol="CL")
    return StandardExecutionEvent(
        order_id=order_id,
        symbol="CL",
        status="Filled",
        filled_qty=1,
        remaining_qty=0,
        avg_price=price,
        raw_event=raw,
    )


def _make_strategy(config: dict) -> ConfigurableStrategy:
    """ConfigurableStrategy stub (pattern from test_parity_cooldown_single_authority)."""
    strat = object.__new__(ConfigurableStrategy)
    strat.config = config
    strat._nickname = "ExitReasonTest"
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


# re-adjudicated: cooldown-single-authority-wiring_07222026_1051 — per-side
# cooldown_bars only (flavored sl/tp keys are dead vocabulary).
CFG_CD1 = {
    "nickname": "cd1",
    "long": {"cooldown_bars": 1},
    "short": {"cooldown_bars": 1},
}


# ---------------------------------------------------------------------------
# D — exit-reason vocabulary
# ---------------------------------------------------------------------------


class TestExitReasonVocabulary:
    def test_time_barrier_exit_passes_time_barrier_reason(self):
        """The time-barrier exit must reset with reason="TIME_BARRIER" (not
        the default "CLOSED") — truthful ledger vocabulary; the cooldown gate
        itself is flavor-blind per-side cooldown_bars."""
        t = _make_trader()
        t.exec_client.get_position.return_value = 1
        t.exec_client.cancel_open_orders.return_value = 2
        t.exec_client.close_position.return_value = MagicMock(order=MagicMock(orderId=71))
        # re-adjudicated: oca-stage4-exit-ordering_07222026_0155 (retire-then-submit)
        # The barrier tick now only RETIRES the tracked protective legs (armed
        # below) and defers the exit submission to the idle reconciler:
        # reconcile tick 1 confirms the legs are gone (settled 1 = still
        # holding) and submits the exit; tick 2 runs the UNCHANGED
        # pending-exit decision (settled 0 = the exit filled, execution
        # supplies the proven price) and resets with reason="TIME_BARRIER" —
        # the same completed-exit outcome, submitted one idle tick later.
        _settled_seq = [1, 0]
        t.exec_client.get_position_settled.side_effect = (
            lambda *a, **k: _settled_seq.pop(0) if _settled_seq else 0
        )
        t.exec_client.get_executions.return_value = [{"order_id": "71", "price": 90.0}]
        t.exec_client.get_open_trades.return_value = []
        t._active_trade_id = "trade_1"
        t._position_side = 1
        t._tp_order_ids = [65]
        t._sl_order_id = 66
        t._tracked_tp_price = 92.0
        t._tracked_sl_price = 88.0
        t.rolling_df_5m = pd.DataFrame(
            {"Close": [90.0]},
            index=pd.DatetimeIndex([pd.Timestamp("2026-06-01 06:55:00")]),
        )
        t.rolling_df_1h = None
        t._position_entry_bar_time = pd.Timestamp("2026-06-01 00:00:00")
        t._position_bars_held = 6
        t._trade_max_hold_bars = 6
        t._reset_position_state = MagicMock()

        submit_result = t._check_time_barrier(
            bar_time=pd.Timestamp("2026-06-01 07:00:00"),
            current_price=90.0,
            atr_value=0.9,
        )
        assert submit_result is False  # retire-then-submit: no in-tick exit
        assert t._pending_exit_order_id is None  # no exit exists yet
        handoff = t._reconcile_pending_position_state()
        assert handoff is False  # tick 1: legs retired, exit submitted
        exited = t._reconcile_pending_position_state()

        assert exited is True
        t._reset_position_state.assert_called_once_with(reason="TIME_BARRIER")

    def test_oob_close_passes_closed_oob_reason(self):
        """The out-of-band close path must pass reason="CLOSED_OOB" (its
        telemetry already uses CLOSED_OOB) instead of the bare default."""
        t = _make_trader()
        t.exec_client.get_position.return_value = 0
        t.exec_client.get_position_settled.return_value = 0  # settled CONFIRMS flat
        t.exec_client.cancel_open_orders.return_value = 4
        t._active_trade_id = "trade_1015"
        t._position_bars_held = 6
        t._reset_position_state = MagicMock()

        # Submit-and-defer (settle-confirm-event-loop_07202026_0713): a flat cache
        # read for a tracked trade no longer books/resets in-callback — it DEFERS
        # (returns False, NO inline settled confirm). The confirmed-flat OOB book +
        # reset(reason="CLOSED_OOB") run BYTE-FOR-BYTE in the idle-loop reconciler.
        exited = t._check_time_barrier(
            bar_time=pd.Timestamp("2026-05-26 13:00:00"),
            current_price=94.2,
            atr_value=1.0,
        )
        assert exited is False  # in-callback defer: no inline OOB book/reset
        t._reset_position_state.assert_not_called()

        t._reconcile_pending_position_state()

        # The idle-loop reconciler's flat-read branch confirmed settled==0 and
        # booked the OOB close, resetting with reason="CLOSED_OOB".
        t._reset_position_state.assert_called_once_with(reason="CLOSED_OOB")

    @pytest.mark.parametrize("reason", ["CLOSED", "CLOSED_OOB"])
    def test_closed_reasons_do_not_arm_per_side_cooldown(self, reason):
        """re-adjudicated: trailing-sl-no-cooldown_07222026_2050 — only an
        ORIGINAL SL arms the cooldown. A CLOSED-family (OOB/unknown) close
        must leave the gate un-armed: exit-bar evaluate() shows the raw
        prob, not a zeroed one."""
        strat = _make_strategy(CFG_CD1)
        strat.on_exit(1, reason, 5)

        sig = strat.evaluate(pd.DataFrame(), 70.0, 0.5, 0)

        assert sig.buy_prob > 0.0, (
            f"reason={reason!r} must NOT arm the per-side cooldown_bars "
            f"gate under the only-original-SL rule; got zeroed "
            f"buy_prob={sig.buy_prob}"
        )


# ---------------------------------------------------------------------------
# E — fill routing
# ---------------------------------------------------------------------------


class TestFillRouting:
    def test_unrecognized_fill_is_not_treated_as_entry(self, caplog):
        """A Filled event whose order id is neither a registered entry nor a
        tracked TP/SL order must NOT enter the entry branch — even when
        decision context exists under that id (context is also stored under
        child order ids for telemetry, live_trader.py:1742)."""
        t = _make_trader()
        t._entry_order_ids = {"100"}
        # Simulate an orphaned SL child: context present (parent's ctx copied
        # under the child id), but the id is not in _tp_order_ids/_sl_order_id.
        t._last_decision_context_by_order_id["555"] = {
            "tp_offset": 2.3, "sl_offset": 2.3, "entry_action": "BUY", "lots": 1,
        }
        t._place_bracket_children_on_fill = MagicMock()

        with caplog.at_level(logging.ERROR):
            t._on_standard_execution_event(_filled_event("555", action="SELL"))

        t._place_bracket_children_on_fill.assert_not_called()
        assert t._active_trade_id is None, (
            "unrecognized fill was booked as a new entry (misrouting)"
        )
        assert any("UNRECOGNIZED FILL" in rec.message for rec in caplog.records), (
            "expected a loud [TRADE] UNRECOGNIZED FILL error log"
        )

    def test_registered_entry_fill_still_processed_as_entry(self):
        """A fill for an order id registered at submission must run the entry
        branch: trade id booked and bracket children placed."""
        t = _make_trader()
        t._entry_order_ids = {"100"}
        t._last_decision_context_by_order_id[100] = {
            "tp_offset": 2.5, "sl_offset": 2.5, "entry_action": "BUY", "lots": 1,
        }
        t._place_bracket_children_on_fill = MagicMock()

        t._on_standard_execution_event(_filled_event("100", action="BUY", price=89.50))

        assert t._active_trade_id == "trade_100"
        t._place_bracket_children_on_fill.assert_called_once()

    def test_sl_fill_still_routes_to_exit_path(self):
        """Regression guard: a tracked SL fill keeps routing to the exit branch
        (software OCA + _reset_position_state with the real reason)."""
        t = _make_trader()
        t._entry_order_ids = {"100"}
        t._sl_order_id = 777
        t._active_trade_id = "trade_100"
        t._reset_position_state = MagicMock()

        t._on_standard_execution_event(_filled_event("777", action="SELL"))

        t.exec_client.cancel_open_orders.assert_called_once_with(symbol="CL")
        t._reset_position_state.assert_called_once_with(reason="SL_HIT")

    def test_entry_submission_registers_order_id_source_scan(self):
        """live_trader.py must maintain the _entry_order_ids registry: initialized
        and populated at entry-order submission (next to the decision-context store)."""
        src = (REPO_ROOT / "src" / "live_execution" / "live_trader.py").read_text(
            encoding="utf-8", errors="replace"
        )
        assert re.search(r"self\._entry_order_ids\s*(?::\s*[Ss]et\S*\s*)?=\s*set\(\)", src), (
            "self._entry_order_ids must be initialized to a set()"
        )
        assert re.search(r"self\._entry_order_ids\.add\(", src), (
            "entry submission must register the order id in self._entry_order_ids"
        )

    def test_livetest_engine_has_no_duplicate_child_placement(self):
        """The harness must not call trader._place_bracket_children_on_fill —
        live_trader's entry branch places children on fill; the harness
        duplicate orphaned the first child set (root cause of misrouted fills)."""
        src = (REPO_ROOT / "scripts" / "livetest_engine.py").read_text(
            encoding="utf-8", errors="replace"
        )
        assert "._place_bracket_children_on_fill(" not in src, (
            "scripts/livetest_engine.py still places bracket children itself "
            "(duplicate of live_trader.py:4047)"
        )
