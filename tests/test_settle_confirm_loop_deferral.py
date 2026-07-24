"""
TDD-TESTER AUTHORIZATION
Target Implementation File: src/live_execution/live_trader.py
Target Class/Function: LiveTrader._reconcile_pending_position_state (NEW idle-loop
                       reconciler), LiveTrader._check_time_barrier (now
                       submit-and-defer), LiveTrader._event_loop (reconciler wiring)
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)
Ticket: settle-confirm-event-loop_07202026_0713

Loop-aware regression coverage (Binding Condition 4) for the settled-confirm
event-loop bug.

THE BUG (verified, from a1464d2 2026-07-16):
    _check_time_barrier is reached ONLY via the ib_insync updateEvent bar-update
    callback chain (_on_bar_update_* -> _on_new_bar -> _check_time_barrier), which
    fires from INSIDE the already-running asyncio event loop. Its a1464d2
    confirm-gate calls _confirm_settled_position -> get_position_settled ->
    IBKRClient.get_position_settled, whose body does
    `self.ib.run(asyncio.wait_for(self.ib.reqPositionsAsync(), ...))`.
    ib.run() == loop.run_until_complete(), which REQUIRES an IDLE loop. Re-entering
    a running loop raises `RuntimeError: This event loop is already running`.
    _confirm_settled_position catches it (except Exception -> None), so the gate
    FAIL-CLOSES on every single TIME BARRIER exit and can never confirm — the
    position is left to hourly housekeeping, which OOB-closes it with a NULL price.

WHY THE EXISTING SUITE MISSED IT:
    tests/test_time_barrier_exit_fill_confirmation.py sets
    exec_client.get_position_settled.return_value = 1/0/None — a plain int mock that
    MOCKS AWAY the ib.run() call that raises. A plain-return mock can never surface
    a "loop already running" failure. The tests below reproduce the real failure by
    driving _check_time_barrier from inside a genuinely RUNNING asyncio loop with a
    settled read that RAISES exactly like ib.run() does in that context.

THE FIX (blueprint Direction A + 4 binding conditions):
    _confirm_settled_position (and get_position_settled / self.ib.run()) is NEVER
    called from inside the bar-update callback. _check_time_barrier becomes
    submit-and-defer (it may only submit orders + set _pending_exit_order_id, then
    return). ALL settled-based confirmation moves to a new idle-loop reconciler
    _reconcile_pending_position_state(), invoked from _event_loop immediately BEFORE
    _run_hourly_housekeeping().

These tests FAIL against current code (RED) for the RIGHT reason — the in-callback
path still calls get_position_settled inline (and there is no reconciler) — and pass
only once the blueprint's fix lands.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from src.live_execution.live_trader import LiveTrader


# ---------------------------------------------------------------------------
# The incident constants (mirroring test_time_barrier_exit_fill_confirmation.py)
# ---------------------------------------------------------------------------

_EXIT_OID = 71
_CURRENT_PRICE = 2.911   # the stale bar-close price the buggy path would fabricate
_PROVEN_PRICE = 2.905    # the price the exit actually traded at, per get_executions


def _execution_record(order_id, price, *, symbol="NG"):
    """A broker execution (fill) record per the get_executions contract — order_id
    is a str on purpose (ledger ids are ints; the join must be str-robust)."""
    return {
        "order_id": str(order_id),
        "perm_id": 9931,
        "exec_id": f"exec-{order_id}",
        "price": price,
        "qty": 1,
        "side": "SLD",
        "time": "2026-07-19T23:00:05",
        "symbol": symbol,
        "commission_report": None,
    }


def _exit_trade(order_id: int = _EXIT_OID):
    """close_position() return value: a *submitted* order (carries an orderId),
    NOT a fill — exactly what ibkr_client.close_cl_position returns."""
    return SimpleNamespace(order=SimpleNamespace(orderId=order_id))


# ---------------------------------------------------------------------------
# The faithful "loop already running" reproduction
# ---------------------------------------------------------------------------

def _loop_hostile_settled(*args, **kwargs):
    """A get_position_settled stand-in that mirrors IBKRClient.get_position_settled:
    its body does `self.ib.run(...)` == loop.run_until_complete(), which raises
    `RuntimeError: This event loop is already running` when a loop is already
    running, and works normally when the loop is idle.

    Reproducing the failure THIS way (rather than a plain return_value) is the whole
    point of Binding Condition 4 — a plain-int mock cannot catch the bug class.

    Idle context -> returns 0 (flat: the settled read succeeds and reports the exit
    filled), exactly as the real settled read behaves once the loop has turned.
    """
    try:
        running = asyncio.get_running_loop().is_running()
    except RuntimeError:
        running = False
    if running:
        raise RuntimeError("This event loop is already running")
    return 0


def _run_in_running_loop(fn):
    """Execute the sync callable `fn` from INSIDE a running asyncio event loop —
    faithfully mirroring the ib_insync bar-update callback context (updateEvent
    fires while loop.run_until_complete / run_forever is executing, so
    loop.is_running() is True during _check_time_barrier)."""
    loop = asyncio.new_event_loop()
    try:
        async def _driver():
            # asyncio.get_running_loop().is_running() is True in here — this is
            # exactly the context the bar-update callback runs _check_time_barrier in.
            return fn()
        return loop.run_until_complete(_driver())
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _base_trader() -> LiveTrader:
    """A bare LiveTrader with the seams the exit / reconciler paths touch stubbed
    (object.__new__, no real I/O), mirroring
    tests/test_time_barrier_exit_fill_confirmation.py conventions."""
    t = LiveTrader.__new__(LiveTrader)

    t.exec_client = MagicMock()
    t.telemetry = MagicMock()
    t._telegram = MagicMock()
    t._emit_health_event = MagicMock()
    # re-adjudicated: cooldown-single-authority-wiring_07222026_1051 — the
    # real _reset_position_state now notifies self.strategy.on_exit (mock sink)
    t.strategy = MagicMock()

    t._execution_symbol = "NG"
    t._front_month_local_symbol = "NGQ6"
    t._exit_mode = "marketable_limit"

    # In-position: +1 long.
    t._active_trade_id = "trade_71"
    t._position_side = 1
    t._position_entry_bar_time = pd.Timestamp("2026-07-18 23:00:00")
    t._position_bars_held = 5
    t._trade_max_hold_bars = 5     # per-trade override; bars_held becomes 6 > 5
    t._max_hold_bars = 288
    t._entry_price = 2.90
    t._atr_at_entry = 0.02
    t._highest_high = 2.93
    t._lowest_low = 2.88
    t._trailing_activated = False
    t._trade_trailing_atr_mult = None

    # Protective legs live at the barrier.
    t._tp_order_ids = [65]
    t._sl_order_id = 66
    t._tracked_tp_price = 2.96
    t._tracked_sl_price = 2.86

    t._pending_entry_order_id = None
    t._pending_entry_bar_time = None
    t._processed_exit_order_ids = set()

    # TIME BARRIER confirmation state.
    t._time_barrier_exit_attempts = 0
    t._pending_exit_order_id = None
    # re-adjudicated: oca-stage4-exit-ordering_07222026_0155 (retire-then-submit)
    # — mechanical fixture repair only: the Stage-4 attrs that
    # _check_time_barrier / _reconcile_pending_position_state now read on an
    # object.__new__ stub.
    t._retiring_leg_ids = []
    t._kill_switch_cancel_confirm_attempts = 0

    # Deterministic seams.
    t._utc_iso_now = MagicMock(return_value="2026-07-19T23:00:05")
    t._build_event_id = MagicMock(return_value="evt-tb")
    t._base_tradebook_fields = MagicMock(return_value={})

    t.rolling_df_5m = None
    t.rolling_df_1h = None

    # Broker seams — safe defaults; each test pins the ones it exercises.
    t.exec_client.get_position.return_value = 1
    t.exec_client.cancel_open_orders.return_value = 2
    t.exec_client.close_position.return_value = _exit_trade()
    t.exec_client.get_open_trades.return_value = []
    t.exec_client.cancel_orders_by_ids.return_value = 0
    t.exec_client.get_executions.return_value = []
    return t


def _pre_exit_trader() -> LiveTrader:
    """State the very next in-callback _check_time_barrier() sees: holding, one bar
    PAST the barrier, no pending exit yet."""
    t = _base_trader()
    t.exec_client.get_position.return_value = 1     # broker still shows +1
    return t


def _post_submit_trader() -> LiveTrader:
    """State AFTER _check_time_barrier has SUBMITTED the exit and deferred (blueprint
    Change 2): the protective legs are cancelled (_sl_order_id/_tp_order_ids
    cleared so the 5-min kill switch covers the gap), the exit id is recorded, and
    _pending_exit_order_id is set. This is exactly what the idle-loop reconciler
    inherits."""
    t = _base_trader()
    t._sl_order_id = None                 # cancelled during submission (:1752)
    t._tp_order_ids = []                  # cancelled during submission (:1753)
    t._processed_exit_order_ids = {str(_EXIT_OID)}
    t._pending_exit_order_id = _EXIT_OID  # the exit awaiting confirmation
    # re-adjudicated: rollover-close-fill-registration_07232026_1920 —
    # mechanical fixture repair only: registration now pairs the pending
    # order id with the close reason it must book under
    # (_register_pending_exit; no silent TIME_BARRIER default at booking),
    # so a stub that arms the id directly must declare the paired reason
    # the production registration would have recorded. No assertion changes.
    t._pending_exit_reason = "TIME_BARRIER"
    t._position_bars_held = 6
    return t


def _run_barrier(t):
    return t._check_time_barrier(
        bar_time=pd.Timestamp("2026-07-19 23:00:00"),
        current_price=_CURRENT_PRICE,
        atr_value=0.02,
    )


# ===========================================================================
# Harness fidelity — prove the reproduction is genuine (guards against a
# tautological mock that could never surface the bug).
# ===========================================================================

class TestHarnessReproducesTheRealFailure:
    def test_settled_read_raises_only_inside_a_running_loop(self):
        """The loop-hostile settled read must behave like ib.run(): raise
        'This event loop is already running' from inside a running loop, and
        succeed when the loop is idle. If this ever stopped raising, every
        loop-aware assertion below would be vacuous."""
        # Idle: the settled read succeeds (returns flat).
        assert _loop_hostile_settled(symbol="NG") == 0
        # Inside a running loop: it raises exactly like loop.run_until_complete().
        with pytest.raises(RuntimeError, match="This event loop is already running"):
            _run_in_running_loop(lambda: _loop_hostile_settled(symbol="NG"))


# ===========================================================================
# THE CRUX (Binding Condition 4): in-callback submit-and-defer, then idle-loop
# reconcile books the PROVEN price.
# ===========================================================================

class TestInCallbackDefersThenReconcilerBooks:
    def test_check_time_barrier_makes_no_settled_call_inside_running_loop(self):
        """Driven from inside a RUNNING loop (the real callback context) with a
        settled read that RAISES like ib.run(), _check_time_barrier must SUBMIT the
        exit but make NO settled call inline: no crash, no NULL-price book, no
        reset — it defers (sets _pending_exit_order_id, returns).

        RED today: current _check_time_barrier calls _confirm_settled_position ->
        get_position_settled INLINE (A1 at :1806). The RuntimeError is swallowed by
        _confirm_settled_position's `except Exception -> None`, so the gate
        fail-closes futilely on EVERY TIME BARRIER exit. The discriminating failure
        is get_position_settled.assert_not_called() — current code calls it, the
        fixed submit-and-defer path must not.
        """
        t = _pre_exit_trader()
        t.exec_client.get_position_settled.side_effect = _loop_hostile_settled
        t._reset_position_state = MagicMock()

        result = _run_in_running_loop(lambda: _run_barrier(t))

        # THE INVARIANT: no settled read from inside the callback.
        t.exec_client.get_position_settled.assert_not_called()
        # re-adjudicated: oca-stage4-exit-ordering_07222026_0155 (retire-then-submit)
        # The in-callback barrier no longer submits the exit either: it arms
        # _retiring_leg_ids (legs cancelled, exit submission deferred to the
        # idle reconciler) and records NO pending exit this tick.
        assert t._retiring_leg_ids == ["65", "66"]
        assert t._pending_exit_order_id is None
        t.exec_client.close_position.assert_not_called()
        # No inline booking / reset on the unconfirmed submission.
        t.telemetry.close_position.assert_not_called()
        t._reset_position_state.assert_not_called()
        assert t._active_trade_id == "trade_71", "position stays tracked (deferred)"
        assert result is False, "a submit-and-defer reports no completed exit"

    def test_reconciler_in_idle_context_books_proven_price(self):
        """Continuation of the crux flow: after the in-callback defer, the idle-loop
        reconciler runs the settled decision. Loop idle -> the settled read succeeds
        (returns 0 = flat: the exit filled) and a matching execution record is
        present => the ledger books the PROVEN execution price (never current_price),
        position state resets, and _pending_exit_order_id is cleared.

        RED today: _reconcile_pending_position_state does not exist (AttributeError).
        """
        t = _post_submit_trader()
        # Idle context (no running loop) -> the loop-hostile read returns 0 (flat).
        t.exec_client.get_position_settled.side_effect = _loop_hostile_settled
        t.exec_client.get_executions.return_value = [
            _execution_record(_EXIT_OID, _PROVEN_PRICE)
        ]
        t._verify_and_heal_protective_legs = MagicMock()

        t._reconcile_pending_position_state()

        # Proven price booked, never the fabricated bar close.
        t.telemetry.close_position.assert_called_once()
        kwargs = t.telemetry.close_position.call_args.kwargs
        assert kwargs.get("reason") == "TIME_BARRIER"
        assert kwargs.get("exit_price") == pytest.approx(_PROVEN_PRICE), (
            "the ledger must carry the PROVEN execution price (2.905), never the "
            f"fabricated current_price ({_CURRENT_PRICE})"
        )
        assert kwargs.get("exit_price") != pytest.approx(_CURRENT_PRICE)
        # Real _reset_position_state ran -> state cleared and pending cleared.
        assert t._active_trade_id is None
        assert t._position_side == 0
        assert t._pending_exit_order_id is None
        # A confirmed flat has nothing to re-arm.
        t._verify_and_heal_protective_legs.assert_not_called()


# ===========================================================================
# Reconciler — fail-closed on an unconfirmed (None) settled read.
# ===========================================================================

class TestReconcilerFailsClosedOnNone:
    def test_reconciler_none_settled_makes_no_state_changes(self):
        """settled -> None (settle timeout/error) in the reconciler => FAIL CLOSED:
        no ledger write, no reset, no re-arm, the trade stays tracked and the exit
        stays pending (retry next idle tick). Never books or re-arms on an
        unconfirmed / guessed value (the blueprint's hard constraint).

        RED today: _reconcile_pending_position_state does not exist.
        """
        t = _post_submit_trader()
        t.exec_client.get_position_settled.return_value = None  # unconfirmed
        t._verify_and_heal_protective_legs = MagicMock()
        t._reset_position_state = MagicMock()

        t._reconcile_pending_position_state()

        t.telemetry.close_position.assert_not_called()
        t._reset_position_state.assert_not_called()
        t._verify_and_heal_protective_legs.assert_not_called()
        assert t._active_trade_id == "trade_71", "position stays tracked"
        assert t._pending_exit_order_id == _EXIT_OID, "exit stays pending (deferred)"
        # The exit may still fill -> must not be cancelled away on an unconfirmed read.
        t.exec_client.cancel_orders_by_ids.assert_not_called()


# ===========================================================================
# Binding Condition 1 — never re-arm while the exit could still fill.
# ===========================================================================

class TestBindingCondition1NeverRearmWhileExitLive:
    def test_settled_nonzero_then_cancel_zero_books_proven_and_does_not_rearm(self):
        """settled != 0 (still holding) but cancel_orders_by_ids -> 0 proves the
        exit had ALREADY filled and left the open book. The re-confirm reads flat,
        so the reconciler books the PROVEN price and MUST NOT re-arm a stop on a
        flat book (that would open a naked reversal).

        RED today: _reconcile_pending_position_state does not exist.
        """
        t = _post_submit_trader()
        # A1 read = 1 (stale, pre-fill); the STRICTLY-AFTER re-confirm reads 0.
        settled_seq = [1, 0]
        t.exec_client.get_position_settled.side_effect = (
            lambda *a, **k: settled_seq.pop(0) if settled_seq else 0
        )
        t.exec_client.cancel_orders_by_ids.return_value = 0   # exit already gone
        t.exec_client.get_executions.return_value = [
            _execution_record(_EXIT_OID, _PROVEN_PRICE)
        ]
        t._verify_and_heal_protective_legs = MagicMock()

        t._reconcile_pending_position_state()

        t.exec_client.cancel_orders_by_ids.assert_called_once_with([_EXIT_OID])
        kwargs = t.telemetry.close_position.call_args.kwargs
        assert kwargs.get("reason") == "TIME_BARRIER"
        assert kwargs.get("exit_price") == pytest.approx(_PROVEN_PRICE)
        assert kwargs.get("exit_price") != pytest.approx(_CURRENT_PRICE)
        assert t._active_trade_id is None            # real reset ran
        assert t._pending_exit_order_id is None
        # CRITICAL: a flat book must NOT get a re-armed stop.
        t._verify_and_heal_protective_legs.assert_not_called()

    def test_settled_nonzero_then_cancel_requested_but_exit_still_open_defers(self):
        """settled != 0 and cancel_orders_by_ids -> 1 (cancel only REQUESTED, not
        confirmed) with the exit STILL present in get_open_trades => the exit can
        still cross at the exchange. The reconciler MUST defer: no re-arm (would
        double-fill against a re-armed stop), no book, position stays tracked with
        _sl_order_id None so the kill switch covers the gap.

        RED today: _reconcile_pending_position_state does not exist.
        """
        t = _post_submit_trader()
        t.exec_client.get_position_settled.return_value = 1
        t.exec_client.cancel_orders_by_ids.return_value = 1   # only requested
        t.exec_client.get_open_trades.return_value = [
            SimpleNamespace(order_id=_EXIT_OID, symbol="NG", status="Submitted")
        ]
        t._verify_and_heal_protective_legs = MagicMock()
        t._reset_position_state = MagicMock()

        t._reconcile_pending_position_state()

        t.telemetry.close_position.assert_not_called()
        t._reset_position_state.assert_not_called()
        # No re-arm while the exit can still fill (BINDING CONDITION 1).
        t._verify_and_heal_protective_legs.assert_not_called()
        assert t._active_trade_id == "trade_71", "position stays tracked"
        assert t._pending_exit_order_id == _EXIT_OID, "exit stays pending"
        assert t._sl_order_id is None, "no stop re-armed while the exit can fill"


# ===========================================================================
# Binding Condition 2 — re-entrancy guard on _check_time_barrier.
# ===========================================================================

class TestBindingCondition2ReentrancyGuard:
    def test_second_callback_while_pending_submits_nothing_and_reads_no_settled(self):
        """While _pending_exit_order_id is set (the reconciler is resolving the
        exit), a subsequent bar callback must NOT submit a second exit and must NOT
        read settled (a repeat settled read would re-crash in-loop). It returns
        immediately.

        Driven from inside a running loop with the loop-hostile settled read so that
        any errant settled call would raise exactly as it does in production.

        RED today: _check_time_barrier has no re-entrancy guard — with the barrier
        already breached it cancels legs, submits a SECOND close_position, and calls
        get_position_settled again. Both close_position AND get_position_settled are
        asserted un-called.
        """
        t = _pre_exit_trader()
        t._pending_exit_order_id = _EXIT_OID   # an exit is already outstanding
        t.exec_client.get_position_settled.side_effect = _loop_hostile_settled

        result = _run_in_running_loop(lambda: _run_barrier(t))

        t.exec_client.close_position.assert_not_called()      # no second exit
        t.exec_client.get_position_settled.assert_not_called()  # no repeat settled
        assert t._pending_exit_order_id == _EXIT_OID, "still awaiting the reconciler"
        # The guard reports no completed exit (blueprint pins the early return, not
        # its exact value; -> bool means False, but accept any non-True falsy).
        assert result is not True


# ===========================================================================
# Binding Condition 3 — the :1657 flat-read gap.
# ===========================================================================

class TestBindingCondition3FlatReadGap:
    def test_check_time_barrier_defers_on_flat_read_with_no_pending_exit(self):
        """A tracked trade whose cached get_position() reads FLAT with NO pending
        exit is the :1657 reconnect-false-flat case. In-callback, _check_time_barrier
        must DEFER: make NO inline settled confirm and book NO out-of-band close off
        an unconfirmed flat — the reconciler owns that decision on an idle tick.

        RED today: _check_time_barrier calls _confirm_settled_position INLINE at
        :1657 and, on a (mocked) settled 0, books the OOB close and resets. The
        discriminating failures: get_position_settled is called, close_position is
        booked, and the position is reset — all forbidden in-callback now.
        """
        t = _base_trader()
        t.exec_client.get_position.return_value = 0   # flat cache read
        t._pending_exit_order_id = None
        t.exec_client.get_position_settled.return_value = 0  # what the old inline path used
        t._reset_position_state = MagicMock()

        result = _run_barrier(t)

        t.exec_client.get_position_settled.assert_not_called()  # no inline confirm
        t.telemetry.close_position.assert_not_called()          # no OOB book in-callback
        t._reset_position_state.assert_not_called()
        assert t._active_trade_id == "trade_71", "position retained (deferred)"
        assert result is False

    def test_reconciler_flat_read_branch_confirms_and_books_oob_close(self):
        """The reconciler's flat-read branch (triggered by a flat cache read for a
        tracked trade with NO pending exit — NOT gated on _pending_exit_order_id)
        confirms with a settled snapshot and, on a CONFIRMED flat, books the
        out-of-band close and resets.

        RED today: _reconcile_pending_position_state does not exist.
        """
        t = _base_trader()
        t.exec_client.get_position.return_value = 0    # flat cache read
        t._pending_exit_order_id = None
        t.exec_client.get_position_settled.return_value = 0   # CONFIRMED flat
        t.exec_client.cancel_open_orders.return_value = 1

        t._reconcile_pending_position_state()

        # Settled confirm happened in the idle reconciler, and the OOB close booked.
        t.exec_client.get_position_settled.assert_called()
        t.telemetry.close_position.assert_called_once()
        assert (
            t.telemetry.close_position.call_args.kwargs.get("reason") == "CLOSED_OOB"
        )
        assert t._active_trade_id is None, "confirmed OOB close resets the position"

    def test_reconciler_flat_read_none_settled_fails_closed(self):
        """Flat-read branch, settled -> None (unconfirmed): FAIL CLOSED — no book,
        no reset, the position (and its protective legs) is RETAINED, deferred to a
        later tick. Exactly the :1658-1666 precedent, relocated to the idle context.

        RED today: _reconcile_pending_position_state does not exist.
        """
        t = _base_trader()
        t.exec_client.get_position.return_value = 0    # flat cache read
        t._pending_exit_order_id = None
        t.exec_client.get_position_settled.return_value = None  # unconfirmed
        t._reset_position_state = MagicMock()

        t._reconcile_pending_position_state()

        t.telemetry.close_position.assert_not_called()
        t._reset_position_state.assert_not_called()
        assert t._active_trade_id == "trade_71", "unconfirmed flat retains the position"

    def test_reconciler_is_inert_when_position_reads_healthy(self):
        """A tracked trade with NO pending exit whose cached get_position() reads
        NON-flat is healthy: the reconciler must NOT take a settled snapshot every
        idle tick and must NOT book anything. Guards against the reconciler hammering
        get_position_settled (an ib.run per poll) on every healthy position.

        RED today: _reconcile_pending_position_state does not exist.
        """
        t = _base_trader()
        t.exec_client.get_position.return_value = 1    # healthy, still holding
        t._pending_exit_order_id = None

        t._reconcile_pending_position_state()

        t.exec_client.get_position_settled.assert_not_called()
        t.telemetry.close_position.assert_not_called()
        assert t._active_trade_id == "trade_71"

    def test_reconciler_is_a_noop_when_flat_and_untracked(self):
        """No tracked trade and no pending exit -> the reconciler is a complete
        no-op (no settled read, no book).

        RED today: _reconcile_pending_position_state does not exist.
        """
        t = _base_trader()
        t._active_trade_id = None
        t._pending_exit_order_id = None
        t._position_side = 0

        t._reconcile_pending_position_state()

        t.exec_client.get_position_settled.assert_not_called()
        t.telemetry.close_position.assert_not_called()


# ===========================================================================
# Binding Condition 1 (ordering) — the reconciler runs BEFORE housekeeping
# each poll (wired in _event_loop immediately above _run_hourly_housekeeping).
# ===========================================================================

class TestReconcilerRunsBeforeHousekeeping:
    def test_event_loop_calls_reconciler_before_hourly_housekeeping(self):
        """The reconciler must run BEFORE _run_hourly_housekeeping() each poll: if
        housekeeping's OOB-closer (or the 5-min kill switch) acts on the pending
        exit first, the NULL-price row persists — just sooner. Drive ONE iteration
        of the real _event_loop and assert the call order.

        RED today: _event_loop never calls _reconcile_pending_position_state, so
        'reconcile' is absent from the recorded call order.
        """
        t = LiveTrader.__new__(LiveTrader)
        t._running = True
        t._heartbeat_offset = 0.0

        t.data_client = MagicMock()
        t.data_client.is_connected.return_value = True
        t.data_client.sleep = MagicMock()
        t.exec_client = MagicMock()
        t.exec_client.is_connected.return_value = True
        t._telegram = MagicMock()

        calls: list[str] = []

        def _reconcile():
            calls.append("reconcile")

        def _housekeeping():
            calls.append("housekeeping")
            t._running = False   # stop after exactly one full iteration

        def _roll():
            calls.append("roll")

        t._reconcile_pending_position_state = MagicMock(side_effect=_reconcile)
        t._run_hourly_housekeeping = MagicMock(side_effect=_housekeeping)
        t._attempt_pending_roll_resolution = MagicMock(side_effect=_roll)
        # Heartbeat block is harmless if it fires; stub its side effects.
        t._log_heartbeat = MagicMock()
        t._check_contract_rollover = MagicMock()
        t._check_stale_bars = MagicMock(return_value=False)

        t._event_loop()

        assert "reconcile" in calls, (
            "_event_loop must invoke _reconcile_pending_position_state each poll"
        )
        assert calls.index("reconcile") < calls.index("housekeeping"), (
            "the reconciler must run BEFORE _run_hourly_housekeeping (Binding "
            "Condition 1 ordering) — else the NULL-price OOB row persists"
        )
