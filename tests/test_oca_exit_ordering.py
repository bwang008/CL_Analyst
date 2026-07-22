"""
TDD-TESTER AUTHORIZATION
Target Implementation File: src/live_execution/live_trader.py
Target Class/Function:
    LiveTrader._check_time_barrier          (R1: retire-then-submit — arm
                                             _retiring_leg_ids BEFORE the leg
                                             cancels are transmitted; NO
                                             in-tick close_position; signal row
                                             action_taken=TIME_BARRIER_EXIT_
                                             PENDING with order_id=None;
                                             re-entrancy guard widened to
                                             _retiring_leg_ids)
    LiveTrader._reconcile_pending_position_state (R2: NEW retiring-legs branch
                                             that runs BEFORE the pending-exit
                                             branch — resting-leg defer under
                                             the EXISTING budget, fail-closed
                                             settled None, settled==0 truthful
                                             booking from executions
                                             (SL_HIT/TP_HIT proven price, else
                                             CLOSED_OOB with NULL price),
                                             reversed-sign Stage-2 flatten,
                                             sign-OK exit submission handoff to
                                             the UNCHANGED pending-exit branch,
                                             A0 no-oid settled-sized re-arm)
    LiveTrader._check_naked_position        (R3: deferral guard widened to
                                             `_pending_exit_order_id is not
                                             None or _retiring_leg_ids` under
                                             the SAME _time_barrier_exit_
                                             attempts budget/release; NEW
                                             cancel-confirm before the flatten:
                                             transmit cancel_open_orders,
                                             re-scan get_open_trades, defer up
                                             to _kill_switch_cancel_confirm_
                                             attempts==3 then proceed LOUDLY)
    LiveTrader._on_standard_execution_event (R4: a Filled event for an id in
                                             _retiring_leg_ids logs WARNING
                                             ("retirement"), NOT the [TRADE]
                                             UNRECOGNIZED FILL ERROR, changes
                                             no state, and never trips the
                                             Stage-2 reversal branch)
    LiveTrader.__init__ / LiveTrader._reset_position_state
                                            (R5: _retiring_leg_ids = [] and
                                             _kill_switch_cancel_confirm_
                                             attempts = 0 initialized and
                                             cleared)
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)
Ticket: oca-stage4-exit-ordering_07222026_0155
Parent: oco-leg-race-audit_07212026_1935 Stage 4 (Stage 1 = 7795e1a,
        Stage 2 = oca-stage2-residual-detection_07222026_0141 / be44ca5).

Phase: RED — against current source these FAIL on the missing features:
  - _check_time_barrier still cancels the legs fire-and-forget AND submits the
    closing order IN THE SAME callback tick (live_trader.py:1782-1851): the R1
    ordering pin records _retiring_leg_ids == [] at cancel-transmit time, the
    in-tick close_position/pending-exit assertions see the old submission, and
    log_signal carries action_taken="TIME_BARRIER_EXIT" (order_id=71), never
    the required TIME_BARRIER_EXIT_PENDING/None row;
  - _reconcile_pending_position_state has NO retiring-legs branch: with
    _retiring_leg_ids armed and no pending exit it falls into the flat-read
    branch (healthy read -> silent no-op; flat read -> generic CLOSED_OOB via
    _book_out_of_band_close, returning False and leaving the retiring ids) —
    every R2 test fails on its discriminator (budget not advanced, exit never
    submitted, SL_HIT/True/cleared-ids never produced, flatten never invoked,
    settled-sized re-arm never called);
  - _check_naked_position's deferral guard keys ONLY on
    _pending_exit_order_id, so a retiring-legs window flattens immediately
    (R3 guard test), and its flatten body has no cancel-confirm re-scan: the
    resting-order deferral tests fail on close_position being called on tick 1
    and _kill_switch_cancel_confirm_attempts never advancing;
  - a Filled event for a retiring leg id lands in the UNRECOGNIZED-FILL ERROR
    branch (no WARNING containing "retirement") — R4 fails on both;
  - __init__ does not initialize the Stage-4 attrs and _reset_position_state
    does not clear them (R5 source-scan + clear tests fail).
Intentionally GREEN today (fences the fix must PRESERVE):
  - kill switch still releases at _MAX_TIME_BARRIER_EXIT_ATTEMPTS with the
    retiring window open (existing 291a9fd release semantics, unchanged);
  - a genuinely-naked position with a CLEAR book after the cancel still
    flattens in a SINGLE tick (the ultimate net is not slowed down);
  - stub-safety: pre-Stage-4 object.__new__ stubs that do NOT declare
    _retiring_leg_ids / _kill_switch_cancel_confirm_attempts (the strict-locked
    builders in tests/test_oca_residual_detection.py,
    tests/test_reconnect_false_flat_recovery.py,
    tests/test_hourly_order_housekeeping.py, tests/test_live_trader_bugs.py)
    must keep working: every read of the new attrs OUTSIDE __init__ must be
    getattr-stub-safe with the empty/zero default (the existing
    _recently_closed_legs / _enable_5m_stream convention). Four fences pin
    this for the barrier guard, the reconciler, the fill callback, and the
    kill switch.

LIVE-FLEET SAFETY: every I/O boundary is a MagicMock or SimpleNamespace — no
broker, no network, no real DB, no data files, no sleeps. No stub sets
_health_events_enabled; health events are asserted by mocking
LiveTrader._emit_health_event and inspecting kinds.

Construction mirrors tests/test_time_barrier_exit_fill_confirmation.py (the
barrier-due NG +1 long with legs [TP 65, SL 66]) and
tests/test_oca_residual_detection.py (object.__new__ stub + MagicMock
exec_client/telemetry; StandardExecutionEvent builder). Rolling frames are
tiny pd.DataFrames with a Close column, pandas 1.5.3 compatible.
"""

from __future__ import annotations

import inspect
import logging
import re
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from src.live_execution.live_trader import (
    LiveTrader,
    _MAX_TIME_BARRIER_EXIT_ATTEMPTS,
)
from src.live_execution.interfaces.execution_interface import (
    StandardExecutionEvent,
)

TICKET_ID = "oca-stage4-exit-ordering_07222026_0155"

# The incident shape (mirroring test_time_barrier_exit_fill_confirmation.py):
# NG +1 long, one bar past the barrier, protective legs TP=65 / SL=66 live.
_EXIT_OID = 71
_TP_OID = 65
_SL_OID = 66
_CURRENT_PRICE = 2.911    # stale bar-callback price (never booked)
_LAST_CLOSE_5M = 2.907    # the REAL last 5m close the reconciler must use
_LAST_CLOSE_1H = 2.999    # decoy: 1h close — 5m must be preferred
_PROVEN_EXIT_PRICE = 2.905  # execution price of the reconciler-submitted exit
_PROVEN_SL_PRICE = 2.8607   # execution price of the SL leg that filled


# ===========================================================================
# Helpers — builders and event constructors
# ===========================================================================


def _bar_frame(last_close: float) -> pd.DataFrame:
    idx = pd.DatetimeIndex([
        pd.Timestamp("2026-07-21 22:50:00"),
        pd.Timestamp("2026-07-21 22:55:00"),
    ])
    return pd.DataFrame({
        "Open": [last_close - 0.010, last_close - 0.005],
        "High": [last_close + 0.010, last_close + 0.010],
        "Low": [last_close - 0.020, last_close - 0.010],
        "Close": [last_close - 0.004, last_close],
        "Volume": [100.0, 120.0],
    }, index=idx)


def _exit_trade(order_id: int = _EXIT_OID):
    """close_position() return value: a *submitted* order (carries an
    orderId), NOT a fill — exactly what ibkr_client.close_cl_position
    returns."""
    return SimpleNamespace(order=SimpleNamespace(orderId=order_id))


def _execution_record(order_id, price, *, symbol="NG"):
    """A broker execution (fill) record per the get_executions contract —
    order_id is a str on purpose (str/int-robust join)."""
    return {
        "order_id": str(order_id),
        "perm_id": 9931,
        "exec_id": f"exec-{order_id}",
        "price": price,
        "qty": 1,
        "side": "SLD",
        "time": "2026-07-21T23:00:05",
        "symbol": symbol,
        "commission_report": None,
    }


def _make_barrier_trader() -> LiveTrader:
    """A LiveTrader holding +1 long NG, one bar PAST the time barrier, with
    the protective legs [TP 65, SL 66] tracked — the state the very next
    _check_time_barrier() enters the barrier-due path from."""
    t = LiveTrader.__new__(LiveTrader)

    t.exec_client = MagicMock()
    t.telemetry = MagicMock()
    t._telegram = MagicMock()
    t._emit_health_event = MagicMock()
    # re-adjudicated: cooldown-single-authority-wiring_07222026_1051 — the
    # real _reset_position_state now notifies self.strategy.on_exit (mock sink)
    t.strategy = MagicMock()

    t._execution_symbol = "NG"
    t._front_month_str = "202608"
    t._front_month_local_symbol = "NGQ6"
    t._exit_mode = "marketable_limit"

    # In-position state: +1 long, held one bar past the barrier.
    t._active_trade_id = "trade_71"
    t._position_side = 1
    t._position_entry_bar_time = pd.Timestamp("2026-07-20 23:00:00")
    t._position_bars_held = 5
    t._trade_max_hold_bars = 5     # per-trade override; bars_held becomes 6 > 5
    t._max_hold_bars = 288
    t._entry_price = 2.90
    t._atr_at_entry = 0.02
    t._highest_high = 2.93
    t._lowest_low = 2.88
    t._trailing_activated = False
    t._trade_trailing_atr_mult = None

    # Protective legs live at the barrier; the tracked PRICES survive.
    t._tp_order_ids = [_TP_OID]
    t._sl_order_id = _SL_OID
    t._tracked_tp_price = 2.96
    t._tracked_sl_price = 2.86

    t._pending_entry_order_id = None
    t._pending_entry_bar_time = None
    t._processed_exit_order_ids = set()
    t._processed_entry_order_ids = set()
    t._entry_order_ids = set()
    t._last_decision_context_by_order_id = {}
    t._open_orders = {}
    t._partial_fill_signatures = set()
    t._last_filled_entry_order_id = None

    # TIME BARRIER confirmation state.
    t._time_barrier_exit_attempts = 0
    t._pending_exit_order_id = None
    # Stage-4 state — stub-declared like every new attr (R5 owns the real
    # __init__ initialization; tests that pin stub-safety delete these).
    t._retiring_leg_ids = []
    t._kill_switch_cancel_confirm_attempts = 0

    # Deterministic seams.
    t._utc_iso_now = MagicMock(return_value="2026-07-21T23:00:05")
    t._build_event_id = MagicMock(return_value="evt-oca4")
    t._base_tradebook_fields = MagicMock(return_value={})

    # Rolling frames: 5m present AND 1h present with a DIFFERENT close so the
    # 5m-preference of the reconciler's submit price is pinned.
    t.rolling_df_5m = _bar_frame(_LAST_CLOSE_5M)
    t.rolling_df_1h = _bar_frame(_LAST_CLOSE_1H)

    # Broker seams — safe defaults; each test overrides the ones it pins.
    t.exec_client.get_position.return_value = 1
    t.exec_client.cancel_open_orders.return_value = 2
    t.exec_client.close_position.return_value = _exit_trade()
    t.exec_client.get_open_trades.return_value = []
    t.exec_client.cancel_orders_by_ids.return_value = 0
    t.exec_client.get_executions.return_value = []
    t.exec_client.get_cached_position.return_value = 0
    return t


def _retiring_trader() -> LiveTrader:
    """State AFTER the R1 barrier tick: legs cancelled (ids cleared, prices
    kept), _retiring_leg_ids armed, NO exit submitted yet — exactly what the
    idle reconciler's retiring-legs branch inherits."""
    t = _make_barrier_trader()
    t._sl_order_id = None
    t._tp_order_ids = []
    t._retiring_leg_ids = [str(_TP_OID), str(_SL_OID)]
    t._pending_exit_order_id = None
    t._position_bars_held = 6
    return t


def _naked_window_trader(retiring, attempts) -> LiveTrader:
    """A kill-switch poll during the retiring window: tracked trade, no
    stop, no pending exit, retiring ids as given."""
    t = _make_barrier_trader()
    t._sl_order_id = None
    t._tp_order_ids = []
    t._retiring_leg_ids = list(retiring)
    t._pending_exit_order_id = None
    t._time_barrier_exit_attempts = attempts
    return t


def _genuinely_naked_trader() -> LiveTrader:
    """A GENUINELY naked position: tracked trade, no stop, no pending exit,
    no retiring window — the kill switch must act (via cancel-confirm)."""
    t = _naked_window_trader([], attempts=0)
    t.exec_client.get_position.return_value = 2
    return t


def _run_barrier(t):
    return t._check_time_barrier(
        bar_time=pd.Timestamp("2026-07-21 23:00:00"),
        current_price=_CURRENT_PRICE,
        atr_value=0.02,
    )


def _fill_event(order_id: str, *, action: str = "SELL", price: float = 2.86,
                qty: int = 1) -> StandardExecutionEvent:
    raw_order = MagicMock()
    raw_order.action = action
    raw_order.permId = None
    raw_order.parentId = None
    raw_order.account = None
    raw = MagicMock()
    raw.order = raw_order
    raw.contract = MagicMock(symbol="NG")
    return StandardExecutionEvent(
        order_id=order_id,
        symbol="NG",
        status="Filled",
        filled_qty=qty,
        remaining_qty=0,
        avg_price=price,
        raw_event=raw,
    )


def _health_kinds(t) -> list:
    out = []
    for c in t._emit_health_event.call_args_list:
        out.append(c.args[0] if c.args else c.kwargs.get("kind"))
    return out


def _ledger_close_calls(t) -> list:
    """(trade_id, reason, exit_price) per telemetry.close_position call —
    robust to positional-or-keyword trade_id."""
    out = []
    for c in t.telemetry.close_position.call_args_list:
        trade_id = c.kwargs.get(
            "trade_id", c.args[0] if c.args else None,
        )
        out.append((trade_id, c.kwargs.get("reason"),
                    c.kwargs.get("exit_price")))
    return out


def _signal_actions(t) -> list:
    return [c.kwargs.get("action_taken")
            for c in t.telemetry.log_signal.call_args_list]


# ===========================================================================
# R1 — the barrier tick retires, never submits
# ===========================================================================


class TestR1RetireThenSubmit:
    def test_retiring_ids_armed_before_cancel_transmit(self):
        """ORDER-OF-OPERATIONS PIN: _retiring_leg_ids must already hold the
        tracked leg ids (str) at the moment cancel_open_orders is
        TRANSMITTED — the deferral guard must be armed before the cancels go
        out, else a kill-switch poll in the gap sees an unguarded naked
        window. RED today: the attr is still [] when the cancel fires."""
        t = _make_barrier_trader()
        seen = {}

        def _cancel(symbol=None, **kwargs):
            seen["retiring_at_cancel"] = [
                str(x) for x in getattr(t, "_retiring_leg_ids", [])
            ]
            return 2

        t.exec_client.cancel_open_orders.side_effect = _cancel

        result = _run_barrier(t)

        assert result is False
        assert t.exec_client.cancel_open_orders.called, (
            "the barrier tick must still transmit the bulk leg cancel"
        )
        assert seen["retiring_at_cancel"] == ["65", "66"], (
            f"_retiring_leg_ids must be armed with the tracked leg ids "
            f"(['65', '66']) BEFORE cancel_open_orders is transmitted; at "
            f"cancel time it held {seen['retiring_at_cancel']!r}"
        )
        assert t._retiring_leg_ids == ["65", "66"], (
            "the armed ids must survive the tick for the idle reconciler"
        )
        # The in-memory leg ids are cleared as today; the prices survive.
        assert t._sl_order_id is None and t._tp_order_ids == []
        assert t._tracked_sl_price == pytest.approx(2.86)
        assert t._tracked_tp_price == pytest.approx(2.96)

    def test_no_exit_submission_in_the_barrier_tick(self):
        """The barrier tick must NOT call close_position and must NOT record
        a pending exit — submission belongs to the idle reconciler. RED
        today: close_position is called in-tick and _pending_exit_order_id
        is set to its oid."""
        t = _make_barrier_trader()

        result = _run_barrier(t)

        assert result is False
        t.exec_client.close_position.assert_not_called()
        assert t._pending_exit_order_id is None, (
            f"no exit exists yet — _pending_exit_order_id must stay None, "
            f"got {t._pending_exit_order_id!r}"
        )
        assert t._active_trade_id == "trade_71"
        t.telemetry.close_position.assert_not_called()

    def test_barrier_tick_logs_pending_signal_with_no_order_id(self):
        """The signal row must say what actually happened: action_taken=
        'TIME_BARRIER_EXIT_PENDING' with order_id=None (no order exists).
        RED today: the row says TIME_BARRIER_EXIT with the submitted oid."""
        t = _make_barrier_trader()

        _run_barrier(t)

        pending_rows = [
            c for c in t.telemetry.log_signal.call_args_list
            if c.kwargs.get("action_taken") == "TIME_BARRIER_EXIT_PENDING"
        ]
        assert len(pending_rows) == 1, (
            f"expected exactly one TIME_BARRIER_EXIT_PENDING signal row; "
            f"actions logged: {_signal_actions(t)!r}"
        )
        assert pending_rows[0].kwargs.get("order_id") is None, (
            f"no exit order exists in the barrier tick — order_id must be "
            f"None, got {pending_rows[0].kwargs.get('order_id')!r}"
        )
        assert "TIME_BARRIER_EXIT" not in _signal_actions(t), (
            "the barrier tick must not claim a submitted TIME_BARRIER_EXIT "
            "— no order was placed this tick"
        )

    def test_second_barrier_bar_while_retiring_defers(self):
        """Re-entrancy: a second barrier-due bar with _retiring_leg_ids
        non-empty must submit NOTHING (no cancel, no close) — the reconciler
        owns the lifecycle. RED today: no retiring guard exists, so the
        barrier path cancels and submits again."""
        t = _make_barrier_trader()
        t._sl_order_id = None
        t._tp_order_ids = []
        t._retiring_leg_ids = ["65", "66"]

        result = _run_barrier(t)

        assert result is False
        t.exec_client.close_position.assert_not_called()
        t.exec_client.cancel_open_orders.assert_not_called()
        assert t._retiring_leg_ids == ["65", "66"], (
            "the retiring window must be left intact for the reconciler"
        )


# ===========================================================================
# R2 — reconciler retiring-legs branch
# ===========================================================================


class TestR2RetiringLegsBranch:
    def test_retiring_branch_precedes_pending_exit_branch(self):
        """STRUCTURAL ORDER PIN: with BOTH a retiring window and a pending
        exit recorded, a still-resting retiring leg must defer WITHOUT
        entering the pending-exit branch (whose signature move is
        cancel_orders_by_ids). RED today: the pending branch runs first and
        drives the old cancel/route machinery."""
        t = _retiring_trader()
        t._pending_exit_order_id = _EXIT_OID
        t.exec_client.get_open_trades.return_value = [
            SimpleNamespace(order_id=_SL_OID, symbol="NG",
                            status="PendingCancel")
        ]
        t.exec_client.get_position_settled.return_value = 1
        t._verify_and_heal_protective_legs = MagicMock()

        result = t._reconcile_pending_position_state()

        assert result is False
        t.exec_client.cancel_orders_by_ids.assert_not_called()
        t._verify_and_heal_protective_legs.assert_not_called()
        assert t._time_barrier_exit_attempts == 1, (
            "the retiring-leg defer must advance the EXISTING budget by one"
        )
        assert t._retiring_leg_ids == ["65", "66"]

    def test_resting_retiring_leg_defers_with_budget_and_no_settled_read(
        self,
    ):
        """A retiring id still in get_open_trades (int order_id vs str
        retiring id — str/int-robust match) is cancel-requested, NOT dead:
        defer, advance _time_barrier_exit_attempts, take NO settled read
        (BINDING CONDITION 1 applied to the legs), submit nothing. RED
        today: the reconciler has no retiring branch — the healthy-read
        no-op leaves the budget at 0."""
        t = _retiring_trader()
        t.exec_client.get_open_trades.return_value = [
            SimpleNamespace(order_id=_SL_OID, symbol="NG",
                            status="PendingCancel")
        ]

        result = t._reconcile_pending_position_state()

        assert result is False
        assert t._time_barrier_exit_attempts == 1, (
            "the resting-leg defer must advance the retry budget exactly "
            "once (the existing _note_time_barrier_deferral arithmetic)"
        )
        t.exec_client.get_position_settled.assert_not_called()
        t.exec_client.close_position.assert_not_called()
        t.telemetry.close_position.assert_not_called()
        assert t._retiring_leg_ids == ["65", "66"], (
            "the retiring ids must be retained while a leg still rests"
        )
        assert t._active_trade_id == "trade_71"

    def test_legs_gone_settled_open_submits_exit_with_exit_mode_and_bar_close(
        self,
    ):
        """Legs gone + settled same-sign nonzero -> the reconciler submits
        the exit NOW: close_position with exit_mode=self._exit_mode (never a
        hardcoded market) and current_price = the REAL last 5m Close (never
        the stale barrier-tick price); the returned oid is registered in
        _processed_exit_order_ids and recorded as _pending_exit_order_id;
        _retiring_leg_ids cleared; returns False (the pending-exit branch
        owns it from the next tick). RED today: no branch — the exit is
        never submitted."""
        t = _retiring_trader()
        t.exec_client.get_open_trades.return_value = []
        t.exec_client.get_position_settled.return_value = 2

        result = t._reconcile_pending_position_state()

        assert result is False
        t.exec_client.close_position.assert_called_once()
        c = t.exec_client.close_position.call_args
        sym = c.kwargs.get("symbol", c.args[0] if c.args else None)
        assert sym == "NG"
        assert c.kwargs.get("exit_mode") == "marketable_limit", (
            f"the reconciler submit must use self._exit_mode "
            f"('marketable_limit'), got {c.kwargs.get('exit_mode')!r}"
        )
        assert c.kwargs.get("current_price") == pytest.approx(
            _LAST_CLOSE_5M
        ), (
            f"the submit price must be the REAL last 5m bar close "
            f"({_LAST_CLOSE_5M}), preferred over the 1h close "
            f"({_LAST_CLOSE_1H}) and never the stale barrier-tick price "
            f"({_CURRENT_PRICE}); got {c.kwargs.get('current_price')!r}"
        )
        assert t._pending_exit_order_id == _EXIT_OID
        assert "71" in {str(x) for x in t._processed_exit_order_ids}, (
            "the submitted exit oid must be registered so its fill is "
            "recognised (not a PHANTOM FILL)"
        )
        assert t._retiring_leg_ids == []
        assert t._active_trade_id == "trade_71", "trade stays tracked"
        t.telemetry.close_position.assert_not_called()

    def test_handoff_then_existing_pending_branch_books_proven_price(self):
        """FLOW: tick 1 retires the legs and submits the exit (settled 2 =
        still holding); tick 2 flows into the EXISTING pending-exit branch
        unchanged — settled 0 (the exit filled) books reason TIME_BARRIER at
        the PROVEN execution price and concludes (True). RED today: tick 1
        is a healthy-read no-op and nothing is ever booked."""
        t = _retiring_trader()
        t.exec_client.get_open_trades.return_value = []
        settled_seq = [2, 0]
        t.exec_client.get_position_settled.side_effect = (
            lambda *a, **k: settled_seq.pop(0) if settled_seq else 0
        )
        t.exec_client.get_executions.return_value = [
            _execution_record(_EXIT_OID, _PROVEN_EXIT_PRICE)
        ]
        t._verify_and_heal_protective_legs = MagicMock()

        first = t._reconcile_pending_position_state()
        assert first is False
        assert t._pending_exit_order_id == _EXIT_OID, (
            "tick 1 must have handed the lifecycle to the pending-exit "
            "branch"
        )

        second = t._reconcile_pending_position_state()

        assert second is True
        assert _ledger_close_calls(t) == [
            ("trade_71", "TIME_BARRIER", pytest.approx(_PROVEN_EXIT_PRICE)),
        ], (
            f"the pending-exit branch must book TIME_BARRIER at the proven "
            f"execution price ({_PROVEN_EXIT_PRICE}); got "
            f"{_ledger_close_calls(t)!r}"
        )
        assert t._active_trade_id is None       # real reset ran
        assert t._pending_exit_order_id is None
        assert t._retiring_leg_ids == []
        t._verify_and_heal_protective_legs.assert_not_called()

    def test_settled_none_fails_closed_retaining_retiring_ids(self):
        """Legs gone + settled read returns None -> FAIL CLOSED: defer,
        advance the budget, RETAIN the retiring ids, book/reset/submit
        nothing. RED today: the healthy-read no-op leaves the budget at 0."""
        t = _retiring_trader()
        t.exec_client.get_open_trades.return_value = []
        t.exec_client.get_position_settled.return_value = None

        result = t._reconcile_pending_position_state()

        assert result is False
        assert t._time_barrier_exit_attempts == 1, (
            "the fail-closed defer must advance the retry budget"
        )
        assert t._retiring_leg_ids == ["65", "66"], (
            "an unconfirmed settled read must RETAIN the retiring ids "
            "(fail-closed) so the branch is re-entered next tick"
        )
        t.exec_client.close_position.assert_not_called()
        t.telemetry.close_position.assert_not_called()
        assert t._active_trade_id == "trade_71"


class TestR2FlatDuringRetirement:
    def test_flat_with_matching_sl_execution_books_sl_hit_proven_price(self):
        """Legs gone + settled==0 + a broker execution matching the retiring
        SL id: a leg filled during the cancel-in-flight window. The close
        must be booked TRUTHFULLY — reason SL_HIT at the PROVEN execution
        price, never TIME_BARRIER, never a fabricated bar price. Returns
        True, state reset, retiring cleared. RED today: the flat-read branch
        books a generic CLOSED_OOB (NULL price) and returns False."""
        t = _retiring_trader()
        t.exec_client.get_open_trades.return_value = []
        t.exec_client.get_position.return_value = 0
        t.exec_client.get_position_settled.return_value = 0
        t.exec_client.get_executions.return_value = [
            _execution_record(_SL_OID, _PROVEN_SL_PRICE)
        ]

        result = t._reconcile_pending_position_state()

        assert result is True, (
            "a leg-filled-during-retirement close is CONCLUDED — the "
            "reconciler must return True"
        )
        closes = _ledger_close_calls(t)
        assert len(closes) == 1
        trade_id, reason, exit_price = closes[0]
        assert trade_id == "trade_71"
        assert reason == "SL_HIT", (
            f"the retiring SL id matched a broker execution — the close "
            f"must be booked SL_HIT (never TIME_BARRIER, never CLOSED_OOB); "
            f"got {reason!r}"
        )
        assert exit_price == pytest.approx(_PROVEN_SL_PRICE), (
            f"the proven execution price ({_PROVEN_SL_PRICE}) must be "
            f"booked — never a fabricated bar price; got {exit_price!r}"
        )
        assert t._active_trade_id is None        # real reset ran
        assert t._retiring_leg_ids == []
        t.exec_client.close_position.assert_not_called()

    def test_flat_with_no_matching_execution_books_closed_oob_null(self):
        """Legs gone + settled==0 + NO execution matching any retiring id:
        the true exit is unknown — book CLOSED_OOB with exit_price=None (an
        honest unknown), return True, clear the retiring ids. RED today: the
        flat-read branch books the same row but returns False and leaves
        _retiring_leg_ids armed."""
        t = _retiring_trader()
        t.exec_client.get_open_trades.return_value = []
        t.exec_client.get_position.return_value = 0
        t.exec_client.get_position_settled.return_value = 0
        t.exec_client.get_executions.return_value = [
            _execution_record(999999, 2.80)  # someone else's fill — no match
        ]

        result = t._reconcile_pending_position_state()

        assert result is True, (
            "the retirement is CONCLUDED (book confirmed flat) — the "
            "reconciler must return True"
        )
        assert t._retiring_leg_ids == [], (
            "every R2 terminal path must clear _retiring_leg_ids"
        )
        closes = _ledger_close_calls(t)
        assert len(closes) == 1
        trade_id, reason, exit_price = closes[0]
        assert trade_id == "trade_71"
        assert reason == "CLOSED_OOB"
        assert exit_price is None, (
            f"no execution evidence -> exit_price must be NULL (honest "
            f"unknown), got {exit_price!r}"
        )
        assert t._active_trade_id is None


class TestR2ReversedSettled:
    def test_reversed_settled_flattens_via_stage2_helper(self):
        """Legs gone + settled REVERSED vs the tracked side (-2 against +1):
        NEVER re-arm — invoke the Stage-2 un-gated flatten with reason
        REVERSED_POSITION_KILL_SWITCH and the tracked ledger trade id, emit
        the rearm-sign-mismatch health event, clear the retiring ids, return
        True. RED today: the healthy-read no-op does nothing."""
        t = _retiring_trader()
        t.exec_client.get_open_trades.return_value = []
        t.exec_client.get_position_settled.return_value = -2
        t._flatten_book_and_reset = MagicMock()
        t._verify_and_heal_protective_legs = MagicMock()

        result = t._reconcile_pending_position_state()

        assert result is True, (
            "a reversed-and-flattened trade is CONCLUDED — return True"
        )
        t._flatten_book_and_reset.assert_called_once()
        kw = t._flatten_book_and_reset.call_args.kwargs
        assert kw.get("reason") == "REVERSED_POSITION_KILL_SWITCH", (
            f"the flatten must carry reason REVERSED_POSITION_KILL_SWITCH; "
            f"got {kw.get('reason')!r}"
        )
        assert kw.get("ledger_trade_id") == "trade_71"
        assert "telegram_text" in kw
        t._verify_and_heal_protective_legs.assert_not_called()
        assert "rearm-sign-mismatch" in _health_kinds(t), (
            f"expected a 'rearm-sign-mismatch' health event; got kinds="
            f"{_health_kinds(t)}"
        )
        assert t._retiring_leg_ids == [], (
            "the reversal terminal path must clear _retiring_leg_ids even "
            "with the (mocked) flatten helper not running the reset"
        )


class TestR2A0Relocated:
    def test_no_oid_submit_rearms_settled_sized_and_stays_tracked(self):
        """A0 relocated: the reconciler's close_position returns None (or an
        order with no oid) — the exit was NEVER SUBMITTED. Re-arm protection
        sized from the SETTLED position, clear the retiring ids, keep the
        trade tracked, note the deferral, return False. RED today: no
        branch — nothing is re-armed."""
        t = _retiring_trader()
        t.exec_client.get_open_trades.return_value = []
        t.exec_client.get_position_settled.return_value = 2
        t.exec_client.close_position.return_value = None  # A0: never submitted
        t._rearm_time_barrier_protection = MagicMock()

        result = t._reconcile_pending_position_state()

        assert result is False
        t._rearm_time_barrier_protection.assert_called_once()
        c = t._rearm_time_barrier_protection.call_args
        sized = c.args[0] if c.args else c.kwargs.get("current_position")
        assert sized == 2, (
            f"the re-arm must be sized from the SETTLED position (2); got "
            f"{sized!r}"
        )
        assert t._retiring_leg_ids == []
        assert t._active_trade_id == "trade_71", (
            "the trade must stay tracked to retry the exit"
        )
        assert t._pending_exit_order_id is None, (
            "no exit exists — nothing may be recorded as pending"
        )
        assert t._time_barrier_exit_attempts == 1, (
            "the A0 path must note the deferral (budget advances)"
        )
        t.telemetry.close_position.assert_not_called()


# ===========================================================================
# R3 — kill-switch guard widening + cancel-confirm
# ===========================================================================


class TestR3KillSwitchGuardWidening:
    def test_defers_while_retiring_under_budget(self):
        """_active_trade_id set, _sl_order_id None, _pending_exit_order_id
        None, _retiring_leg_ids NON-EMPTY, attempts < _MAX: the kill switch
        must return WITHOUT flattening — the reconciler owns the retiring
        window under the SAME existing budget. RED today: the guard keys
        only on _pending_exit_order_id, so this window flattens a healthy
        mid-retirement position immediately."""
        t = _naked_window_trader(["65", "66"], attempts=1)
        t.exec_client.get_position.return_value = 1

        t._check_naked_position()

        t.exec_client.close_position.assert_not_called()
        t.telemetry.close_position.assert_not_called()
        t.exec_client.cancel_open_orders.assert_not_called()

    def test_releases_at_budget_exhaustion(self):
        """FENCE (green today, must stay green): at attempts ==
        _MAX_TIME_BARRIER_EXIT_ATTEMPTS the kill switch RELEASES with the
        retiring window still open — the existing 291a9fd release semantics
        are unchanged (same budget, same release, no new arithmetic)."""
        t = _naked_window_trader(
            ["65", "66"], attempts=_MAX_TIME_BARRIER_EXIT_ATTEMPTS,
        )
        t.exec_client.get_position.return_value = 1
        t.exec_client.get_open_trades.return_value = []

        t._check_naked_position()

        assert t.exec_client.close_position.called, (
            "at _MAX the kill switch must release and flatten (ultimate net)"
        )
        assert (
            t.exec_client.close_position.call_args.kwargs.get("exit_mode")
            == "market"
        )
        assert (
            t.telemetry.close_position.call_args.kwargs.get("reason")
            == "NAKED_POSITION_KILL_SWITCH"
        )


class TestR3CancelConfirm:
    def test_resting_order_after_cancel_defers_without_flatten(self):
        """Genuinely-naked path: after transmitting cancel_open_orders the
        re-scan of get_open_trades still shows a resting order — NO
        close_position this tick; the bounded
        _kill_switch_cancel_confirm_attempts counter advances to 1. RED
        today: no re-scan exists — the flatten fires on tick 1."""
        t = _genuinely_naked_trader()
        t.exec_client.get_open_trades.return_value = [
            SimpleNamespace(order_id=88, symbol="NG", status="Submitted")
        ]

        t._check_naked_position()

        t.exec_client.close_position.assert_not_called()
        t.telemetry.close_position.assert_not_called()
        assert t.exec_client.cancel_open_orders.called, (
            "the cancel must still be transmitted before the re-scan"
        )
        assert t._kill_switch_cancel_confirm_attempts == 1, (
            "the cancel-confirm deferral must advance its bounded counter"
        )
        assert t._active_trade_id == "trade_71", "no reset on a deferral"

    def test_three_deferrals_then_proceeds_loudly(self, caplog):
        """The cancel-confirm bound is 3: three deferral ticks (counter 1, 2,
        3) while the order keeps resting, then the FOURTH tick proceeds with
        the flatten anyway, LOUDLY (log.critical present) — the ultimate net
        must never be permanently suppressed. The counter resets on the
        successful flatten. RED today: the flatten fires on tick 1."""
        t = _genuinely_naked_trader()
        t.exec_client.get_open_trades.return_value = [
            SimpleNamespace(order_id=88, symbol="NG", status="Submitted")
        ]

        for i in range(3):
            t._check_naked_position()
            assert t.exec_client.close_position.call_count == 0, (
                f"tick {i + 1}: the flatten must be deferred while the "
                f"cancel is unconfirmed and the bound is unspent"
            )
            assert t._kill_switch_cancel_confirm_attempts == i + 1, (
                f"tick {i + 1}: the bounded counter must advance by one"
            )

        with caplog.at_level(logging.INFO):
            t._check_naked_position()

        assert t.exec_client.close_position.called, (
            "at cancel-confirm exhaustion the kill switch must PROCEED with "
            "the flatten (the resting order gets the helper's own "
            "idempotent cancel)"
        )
        assert any(
            r.levelno == logging.CRITICAL for r in caplog.records
        ), "the forced flatten must be LOUD (log.critical)"
        assert (
            t.telemetry.close_position.call_args.kwargs.get("reason")
            == "NAKED_POSITION_KILL_SWITCH"
        )
        assert t._kill_switch_cancel_confirm_attempts == 0, (
            "the counter must reset on a successful flatten/state reset"
        )

    def test_clear_book_flattens_in_a_single_tick(self):
        """FENCE (green today, must stay green): when the re-scan shows a
        CLEAR book after the cancel, the flatten completes in a SINGLE tick
        — cancel-confirm must not slow down the clean case."""
        t = _genuinely_naked_trader()
        t.exec_client.get_open_trades.return_value = []

        t._check_naked_position()

        assert t.exec_client.close_position.called, (
            "clear book after the cancel -> immediate flatten (one tick)"
        )
        assert (
            t.exec_client.close_position.call_args.kwargs.get("exit_mode")
            == "market"
        )
        assert (
            t.telemetry.close_position.call_args.kwargs.get("reason")
            == "NAKED_POSITION_KILL_SWITCH"
        )
        assert t._kill_switch_cancel_confirm_attempts == 0


# ===========================================================================
# R4 — retiring-leg fill events stop being noise
# ===========================================================================


class TestR4RetiringLegFill:
    def test_retiring_leg_fill_warns_and_reconciler_books(self, caplog):
        """A Filled event for an id in _retiring_leg_ids (the SL filled
        during the cancel-in-flight window) must log a WARNING containing
        'retirement' — NOT the [TRADE] UNRECOGNIZED FILL ERROR — change no
        state, place no orders, and NOT trip the Stage-2 reversal branch.
        Booking stays with the reconciler, which then books the truthful
        SL_HIT from settled/executions. RED today: the event lands in the
        UNRECOGNIZED-FILL ERROR branch."""
        t = _retiring_trader()

        with caplog.at_level(logging.INFO):
            t._on_standard_execution_event(
                _fill_event(str(_SL_OID), action="SELL",
                            price=_PROVEN_SL_PRICE)
            )

        warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING
            and "retirement" in r.getMessage().lower()
        ]
        assert len(warnings) == 1, (
            "a retiring-leg fill must log exactly ONE warning mentioning "
            "'retirement' — today it dies in the UNRECOGNIZED-FILL ERROR "
            "branch"
        )
        assert not any(
            "UNRECOGNIZED FILL" in r.getMessage() for r in caplog.records
        ), "the retiring-leg fill must NOT take the UNRECOGNIZED-FILL path"
        assert "oca-race-reversal" not in _health_kinds(t), (
            "a retiring-leg fill is NOT the Stage-2 residual-reversal case "
            "(the trade is still tracked; no registry snapshot exists)"
        )
        t.exec_client.close_position.assert_not_called()
        t.telemetry.close_position.assert_not_called()
        assert t._active_trade_id == "trade_71", "no state change"
        assert t._position_side == 1, "no state change"
        assert t._retiring_leg_ids == ["65", "66"], (
            "booking stays with the reconciler — the event is informational"
        )

        # The reconciler then books the truthful close (settled + executions).
        t.exec_client.get_open_trades.return_value = []
        t.exec_client.get_position.return_value = 0
        t.exec_client.get_position_settled.return_value = 0
        t.exec_client.get_executions.return_value = [
            _execution_record(_SL_OID, _PROVEN_SL_PRICE)
        ]

        result = t._reconcile_pending_position_state()

        assert result is True
        closes = _ledger_close_calls(t)
        assert closes == [
            ("trade_71", "SL_HIT", pytest.approx(_PROVEN_SL_PRICE)),
        ], (
            f"the reconciler must book the truthful SL_HIT at the proven "
            f"price; got {closes!r}"
        )
        assert t._active_trade_id is None
        assert t._retiring_leg_ids == []


# ===========================================================================
# R5 — hygiene (+ stub-safety fences protecting strict-locked sibling suites)
# ===========================================================================


class TestR5Hygiene:
    def test_init_declares_retiring_and_cancel_confirm_state(self):
        """__init__ must initialize _retiring_leg_ids = [] and
        _kill_switch_cancel_confirm_attempts = 0 (R5). Source-scan on
        LiveTrader.__init__ (the tests/test_exit_reason_and_fill_routing.py
        registry-scan convention — constructing a real LiveTrader needs live
        I/O)."""
        src = inspect.getsource(LiveTrader.__init__)
        assert re.search(
            r"self\._retiring_leg_ids\s*(?::[^=\n]*)?=\s*\[\]", src,
        ), (
            "LiveTrader.__init__ must initialize self._retiring_leg_ids = []"
        )
        assert re.search(
            r"self\._kill_switch_cancel_confirm_attempts\s*(?::[^=\n]*)?=\s*0",
            src,
        ), (
            "LiveTrader.__init__ must initialize "
            "self._kill_switch_cancel_confirm_attempts = 0"
        )

    def test_reset_position_state_clears_retiring_state(self):
        """_reset_position_state must clear _retiring_leg_ids and zero
        _kill_switch_cancel_confirm_attempts — a fresh position starts with
        no stale retirement window."""
        t = _retiring_trader()
        t._kill_switch_cancel_confirm_attempts = 2

        t._reset_position_state(reason="CLOSED")

        assert t._retiring_leg_ids == [], (
            "_reset_position_state must clear _retiring_leg_ids"
        )
        assert t._kill_switch_cancel_confirm_attempts == 0, (
            "_reset_position_state must zero "
            "_kill_switch_cancel_confirm_attempts"
        )


class TestStage4StubSafetyFences:
    """FENCES (green today, must stay green): pre-Stage-4 object.__new__
    stubs in STRICT-LOCKED sibling suites (tests/test_oca_residual_
    detection.py, tests/test_reconnect_false_flat_recovery.py,
    tests/test_hourly_order_housekeeping.py, tests/test_live_trader_bugs.py)
    do NOT declare the Stage-4 attrs. Every read of _retiring_leg_ids /
    _kill_switch_cancel_confirm_attempts OUTSIDE __init__ must therefore be
    getattr-stub-safe with the empty/zero default (the existing
    _recently_closed_legs convention) — an AttributeError inside the
    reconciler's never-raise boundary would silently break those suites'
    pins with no way to repair the locked files."""

    def _without_stage4_attrs(self) -> LiveTrader:
        t = _make_barrier_trader()
        del t._retiring_leg_ids
        del t._kill_switch_cancel_confirm_attempts
        return t

    def test_barrier_flat_read_defer_without_stage4_attrs(self):
        """The barrier's flat-read defer (tracked trade, cached flat) must
        work on a stub without the Stage-4 attrs (the housekeeping suite's
        :1666 shape)."""
        t = self._without_stage4_attrs()
        t.exec_client.get_position.return_value = 0

        result = _run_barrier(t)

        assert result is False
        t.telemetry.close_position.assert_not_called()
        assert t._active_trade_id == "trade_71"

    def test_reconciler_flat_read_books_oob_without_stage4_attrs(self):
        """The reconciler's flat-read branch must still confirm + book the
        OOB close on a stub without the Stage-4 attrs (the
        reconnect-false-flat suite's shape)."""
        t = self._without_stage4_attrs()
        t.exec_client.get_position.return_value = 0
        t.exec_client.get_position_settled.return_value = 0

        t._reconcile_pending_position_state()

        closes = _ledger_close_calls(t)
        assert len(closes) == 1 and closes[0][1] == "CLOSED_OOB", (
            f"the flat-read OOB booking must survive a stub without the "
            f"Stage-4 attrs; ledger closes={closes!r}"
        )

    def test_fill_callback_unrecognized_path_without_stage4_attrs(
        self, caplog,
    ):
        """An unrecognized fill on a stub without the Stage-4 attrs must
        still take the loud UNRECOGNIZED-FILL path (no AttributeError)."""
        t = self._without_stage4_attrs()

        with caplog.at_level(logging.ERROR):
            t._on_standard_execution_event(
                _fill_event("999", action="SELL", price=2.90)
            )

        assert any(
            "UNRECOGNIZED FILL" in r.getMessage() for r in caplog.records
        ), "the unknown-id fill must keep the loud UNRECOGNIZED FILL log"
        assert t._active_trade_id == "trade_71"

    def test_kill_switch_genuine_naked_without_stage4_attrs(self):
        """The kill switch must still flatten a genuinely-naked position on
        a stub without the Stage-4 attrs (clear book -> single tick)."""
        t = self._without_stage4_attrs()
        t._sl_order_id = None
        t._tp_order_ids = []
        t.exec_client.get_position.return_value = 2
        t.exec_client.get_open_trades.return_value = []

        t._check_naked_position()

        assert t.exec_client.close_position.called, (
            "a genuinely-naked position must still flatten on a "
            "pre-Stage-4 stub"
        )
        assert (
            t.telemetry.close_position.call_args.kwargs.get("reason")
            == "NAKED_POSITION_KILL_SWITCH"
        )
