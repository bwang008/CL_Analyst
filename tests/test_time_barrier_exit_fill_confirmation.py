"""
TDD-TESTER AUTHORIZATION
Target Implementation File: src/live_execution/live_trader.py
Target Class/Function: LiveTrader._reconcile_pending_position_state (the relocated
                       settled decision + its never-raise except boundary now
                       advances the retry budget) + LiveTrader._check_time_barrier
                       (submit-and-defer) + LiveTrader._check_naked_position
                       (bounded pending-exit guard)
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)
Ticket: exit-fill-unverified_07152026_1855 (original)
        + settle-confirm-event-loop_07202026_0713 (control-flow adaptation)
        + killswitch-pending-exit-guard_07202026_1805 (contract CORRECTION:
          the naked-position kill switch must DEFER to the idle-loop reconciler
          while a TIME BARRIER exit is pending AND the reconciler's bounded retry
          budget is not yet spent, then flatten as the ultimate net once
          _time_barrier_exit_attempts reaches _MAX_TIME_BARRIER_EXIT_ATTEMPTS.
          This CORRECTS the earlier claim that the kill switch fires "for free"
          on the FIRST deferral poll — that immediate-fire raced the reconciler
          and prematurely flattened a healthy mid-exit MES position on 2026-07-20.
          The rewrite of test_kill_switch_fires_for_free_because_trade_stays_tracked
          and the new TestKillSwitchBoundedPendingExitGuard class below assert the
          bounded behavior; NO assertion is loosened — flatten-at-_MAX replaces
          flatten-on-poll-1, and a NEW persistent-broker-failure case proves the
          bound is monotonic per poll (advances even when a broker call throws) so
          the kill switch can never be suppressed forever.)

Regression suite for the fire-and-forget TIME BARRIER exit that left a NAKED +
UNTRACKED live position (2026-07-14 NG incident, client_id 3000).

The original defect: the TIME BARRIER exit cancelled the protective SL/TP, booked
the ledger CLOSED with a FABRICATED price (exit_price=current_price), and reset
position state — all WITHOUT confirming the exit order actually filled. When the
exit (a GTC limit priced off a stale bar close) never fills, the position is left
naked, and BOTH safety nets are disarmed by the same reset (kill switch needs
_active_trade_id; housekeeping heal needs an OPEN ledger row).

The invariant the original ticket (a1464d2) established:
    Never book a close, never reset position state, and never re-arm protection
    until the broker has been asked and has answered. Book only on a *confirmed*
    flat with a *proven* fill price; re-arm the stop only once no exit order
    that could still fill is live AND the position is confirmed still open.

CONTROL-FLOW ADAPTATION (settle-confirm-event-loop_07202026_0713):
    a1464d2 ran that confirm/book/re-arm decision INLINE in _check_time_barrier —
    but _check_time_barrier is reached only from inside the ib_insync bar-update
    callback (a RUNNING asyncio loop), and the settled read it depends on does
    `self.ib.run()` == loop.run_until_complete(), which raises
    `RuntimeError: This event loop is already running` in that context. The fix
    moves the settled read OUT of the callback: _check_time_barrier now only
    SUBMITS the exit and DEFERS (sets _pending_exit_order_id, returns False), and
    the BYTE-FOR-BYTE-identical A1/A2/route decision logic runs in the new
    idle-loop reconciler _reconcile_pending_position_state().

    These tests are adapted to the new control flow WITHOUT weakening any
    assertion (fake-fidelity/flow adaptation — same precedent as a1464d2). Every
    decision assertion below is now driven through _submit_then_reconcile(), which
    performs the in-callback submit-and-defer and then the idle-loop reconcile.
    The decision logic — and thus every assertion's strength — is unchanged; only
    WHERE it runs moved. The reconciler returns the same True/False verdict
    _check_time_barrier used to (True = exit booked/completed, False = deferred), a
    testability contract that follows directly from the byte-for-byte relocation.
    Loop-aware coverage of the RuntimeError itself lives in the companion file
    tests/test_settle_confirm_loop_deferral.py.

Four cases (deterministic, no real I/O — LiveTrader built via object.__new__,
exec_client/telemetry stubbed, mirroring tests/test_exit_reason_and_fill_routing.py
and tests/test_reconnect_false_flat_recovery.py):

  1. Reproduce the incident: settled shows STILL HOLDING -> do NOT book, do NOT
     reset, keep tracked, retire the stranded exit by id BEFORE re-arming, and
     the (unchanged) kill switch fires for free on the next poll.
  2. Confirmed fill books the *proven* execution price, never current_price.
  3. BINDING CONDITION (the race): settled=1 but cancel_orders_by_ids -> 0
     (the exit had already filled) => book the proven price, do NOT re-arm.
  4. Unconfirmed (settled -> None) fails closed: no book, no reset, stays
     tracked, no re-arm.

These tests FAIL until the reconciler exists (Red for the settle-confirm-event-loop
ticket) and pass only once its fix is implemented.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from src.live_execution.live_trader import (
    LiveTrader,
    _MAX_TIME_BARRIER_EXIT_ATTEMPTS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# The incident symbol/prices: NG, GTC exit limit priced off a stale bar close
# (2.911) that rested forever while the market drifted to the true fill (2.905).
_EXIT_OID = 71
_CURRENT_PRICE = 2.911   # the stale limit price the OLD code fabricated into the ledger
_PROVEN_PRICE = 2.905    # the price the exit actually traded at, per get_executions


def _exit_trade(order_id: int = _EXIT_OID):
    """A close_position() return value: a *submitted* order (carries an
    orderId), NOT a fill — exactly what ibkr_client.close_cl_position returns."""
    return SimpleNamespace(order=SimpleNamespace(orderId=order_id))


def _execution_record(order_id, price, *, symbol="NG"):
    """A broker execution (fill) record per the get_executions contract
    (execution_interface.py:172): order_id is a str on purpose."""
    return {
        "order_id": str(order_id),
        "perm_id": 9931,
        "exec_id": f"exec-{order_id}",
        "price": price,
        "qty": 1,
        "side": "SLD",
        "time": "2026-07-14T23:00:05",
        "symbol": symbol,
        "commission_report": None,
    }


def _make_time_barrier_trader() -> LiveTrader:
    """A LiveTrader holding +1 long, one bar PAST the time barrier, positioned
    so the very next _check_time_barrier() enters the exit branch (:1679)."""
    t = LiveTrader.__new__(LiveTrader)

    t.exec_client = MagicMock()
    t.telemetry = MagicMock()
    t._telegram = MagicMock()
    t._emit_health_event = MagicMock()

    t._execution_symbol = "NG"
    t._front_month_local_symbol = "NGQ6"
    t._exit_mode = "marketable_limit"

    # In-position state: +1 long, held one bar past the barrier.
    t._active_trade_id = "trade_71"
    t._position_side = 1
    t._position_entry_bar_time = pd.Timestamp("2026-07-13 23:00:00")
    t._position_bars_held = 5
    t._trade_max_hold_bars = 5     # per-trade override; bars_held becomes 6 > 5
    t._max_hold_bars = 288
    t._entry_price = 2.90
    t._atr_at_entry = 0.02
    t._highest_high = 2.93
    t._lowest_low = 2.88
    t._trailing_activated = False
    t._trade_trailing_atr_mult = None

    # Protective legs: SL/TP are live at the barrier; :1679 cancels them, so
    # _sl_order_id starts populated and is what a correct re-arm must restore.
    t._tp_order_ids = [65]
    t._sl_order_id = 66
    t._tracked_tp_price = 2.96
    t._tracked_sl_price = 2.86

    t._pending_entry_order_id = None
    t._pending_entry_bar_time = None
    t._processed_exit_order_ids = set()

    # Site B tracked state the fix declares (blueprint ~:643-647).
    t._time_barrier_exit_attempts = 0
    t._pending_exit_order_id = None

    # Deterministic seams.
    t._utc_iso_now = MagicMock(return_value="2026-07-14T23:00:05")
    t._build_event_id = MagicMock(return_value="evt-tb")
    t._base_tradebook_fields = MagicMock(return_value={})

    # rolling frames (only the kill-switch poll reads these).
    t.rolling_df_5m = None
    t.rolling_df_1h = None

    # Broker seams the fixed exit path exercises — safe defaults; each test
    # overrides the ones it pins.
    t.exec_client.get_position.return_value = 1
    t.exec_client.cancel_open_orders.return_value = 2
    t.exec_client.close_position.return_value = _exit_trade()
    t.exec_client.get_open_trades.return_value = []
    t.exec_client.cancel_orders_by_ids.return_value = 0
    t.exec_client.get_executions.return_value = []
    return t


def _run_barrier(t):
    return t._check_time_barrier(
        bar_time=pd.Timestamp("2026-07-14 23:00:00"),
        current_price=_CURRENT_PRICE,
        atr_value=0.02,
    )


def _submit_then_reconcile(t):
    """Drive the NEW control flow (settle-confirm-event-loop_07202026_0713).

    The in-callback ``_check_time_barrier`` may no longer call the settled read
    (it runs inside the ib_insync bar-update callback, a RUNNING asyncio loop,
    where ``self.ib.run()`` raises ``RuntimeError: This event loop is already
    running``). It now only SUBMITS the exit and DEFERS (sets
    ``_pending_exit_order_id``, returns ``False``); the a1464d2 A1/A2/route
    settled DECISION — relocated BYTE-FOR-BYTE — runs on a genuinely-idle tick in
    ``_reconcile_pending_position_state()``.

    This helper performs BOTH steps and returns the reconciler's verdict — the
    same ``True`` (exit booked/completed) / ``False`` (deferred) these tests
    asserted on ``_check_time_barrier`` before the relocation. It also pins the
    submit-and-defer contract (the in-callback call must return ``False`` and
    make NO settled read), so the adaptation adds strength rather than loosening.
    """
    submit_result = _run_barrier(t)
    assert submit_result is False, (
        "the in-callback _check_time_barrier must SUBMIT-AND-DEFER (return False) "
        "— the settled decision now belongs to the idle-loop reconciler"
    )
    assert t._pending_exit_order_id == _EXIT_OID, (
        "the submitted exit id must be recorded as pending for the reconciler"
    )
    # The settled read must NOT happen inside the bar-update callback.
    t.exec_client.get_position_settled.assert_not_called()
    return t._reconcile_pending_position_state()


# ---------------------------------------------------------------------------
# Case 1 — reproduce the incident: unconfirmed-non-flat must NOT book/reset
# ---------------------------------------------------------------------------


class TestIncidentReproduction:
    def test_time_barrier_exit_that_did_not_fill_is_not_booked(self):
        """The incident: past the barrier, the exit order NEVER fills and the
        broker still reports the position OPEN (settled -> 1). The fixed path
        MUST NOT book the ledger CLOSED, MUST NOT reset position state, MUST
        keep the trade tracked, MUST retire the stranded exit order by id
        BEFORE re-arming protection, and MUST leave the (unchanged) 5-minute
        kill switch armed to flatten on the next poll.

        RED today: the fire-and-forget branch books telemetry.close_position
        with the fabricated current_price, resets state, returns True, and
        never touches cancel_orders_by_ids / _verify_and_heal_protective_legs.
        """
        t = _make_time_barrier_trader()
        # settled keeps reporting the position OPEN (exit never filled).
        t.exec_client.get_position_settled.return_value = 1
        # The stranded exit is not (or no longer) resting -> count 0 -> the
        # re-confirm below still shows OPEN, so the exit died without filling
        # and protection is safe to re-arm.
        events = []

        def _cancel_by_ids(order_ids):
            events.append(("cancel_by_ids", list(order_ids)))
            return 0

        t.exec_client.cancel_orders_by_ids.side_effect = _cancel_by_ids

        def _rearm(*args, **kwargs):
            events.append(("verify_and_heal", kwargs))
            return "healed"

        t._verify_and_heal_protective_legs = MagicMock(side_effect=_rearm)
        t._reset_position_state = MagicMock()

        result = _submit_then_reconcile(t)

        # 1a. Did NOT exit cleanly — deferred to a later bar.
        assert result is False, (
            "an unfilled exit must NOT report a completed exit (fire-and-forget "
            "returned True and marked the ledger closed on a naked position)"
        )
        # 1b. Ledger NOT closed — the fabricated-price write is the defect.
        t.telemetry.close_position.assert_not_called()
        # 1c. Position state NOT reset — belief that we hold is preserved.
        t._reset_position_state.assert_not_called()
        # 1d. Trade stays tracked (arms the kill switch for free).
        assert t._active_trade_id == "trade_71"
        # 1e. The stranded exit order was retired by id...
        assert any(e[0] == "cancel_by_ids" for e in events), (
            "the stranded GTC exit must be cancelled by id before re-arming — "
            "otherwise it can double-fill against a re-armed stop"
        )
        cancel_ids = next(e[1] for e in events if e[0] == "cancel_by_ids")
        assert cancel_ids == [_EXIT_OID], (
            f"cancel_orders_by_ids must target the exit order id "
            f"{_EXIT_OID}, got {cancel_ids!r}"
        )
        # 1f. ...and it was retired BEFORE protection was re-armed (ordering
        # is load-bearing — a resting exit + a re-armed stop = double fill).
        assert any(e[0] == "verify_and_heal" for e in events), (
            "protection must be re-armed once the exit is confirmed dead"
        )
        names = [e[0] for e in events]
        assert names.index("cancel_by_ids") < names.index("verify_and_heal"), (
            "the exit must be cancelled BEFORE the stop is re-armed"
        )

    def test_kill_switch_defers_to_reconciler_then_fires_at_budget_exhaustion(
        self,
    ):
        """CONTRACT CORRECTION (killswitch-pending-exit-guard_07202026_1805).

        The BOUNDED contract that replaces the buggy "fires for free on the
        first deferral poll" claim. During the deferral window the exit keeps
        _active_trade_id set and _sl_order_id None (BINDING CONDITION 1 — no
        stop may be re-armed while the exit can still fill), and
        _pending_exit_order_id points at the resting exit. The reconciler owns
        that exit's lifecycle, so the naked-position kill switch must:

          * DEFER (NOT flatten) while _pending_exit_order_id is set AND the
            reconciler's retry budget is unspent
            (_time_barrier_exit_attempts < _MAX_TIME_BARRIER_EXIT_ATTEMPTS) —
            flattening here would race the reconciler's cancel-and-defer and
            market-close a healthy mid-exit position (the 2026-07-20 MES
            incident); and
          * FLATTEN as the ultimate net once the budget is exhausted
            (_time_barrier_exit_attempts == _MAX) and the stop is still None —
            the kill switch releases exactly when the reconciler's A4 pages a
            human.

        RED today: with NO pending-exit guard, _check_naked_position sees
        `_sl_order_id is None` + `pos != 0` on the FIRST deferral poll and
        flattens immediately (attempts == 1), racing the reconciler — the exact
        premature-flatten this ticket fixes. (The stale prior assertion —
        `flatten-on-poll-1 is a feature` — was itself the defect.)"""
        t = _make_time_barrier_trader()
        t.exec_client.get_position_settled.return_value = 1
        t.exec_client.cancel_orders_by_ids.return_value = 1
        # The exit rests forever (never leaves the open book) -> BINDING
        # CONDITION 1 defer: stay tracked, _sl_order_id None, no re-arm,
        # _pending_exit_order_id retained, attempts incremented to 1.
        t.exec_client.get_open_trades.return_value = [
            SimpleNamespace(order_id=_EXIT_OID, symbol="NG", status="Submitted")
        ]

        result = _submit_then_reconcile(t)

        assert result is False
        # The exit path left the trade tracked with no live stop, one pending
        # exit, one deferral spent — exactly the mid-exit window.
        assert t._active_trade_id == "trade_71", (
            "the fix must keep _active_trade_id set so the reconciler still "
            "owns the exit and the kill switch can escalate later"
        )
        assert t._sl_order_id is None, (
            "no stop may be re-armed while the exit can still fill "
            "(BINDING CONDITION 1)"
        )
        assert t._pending_exit_order_id == _EXIT_OID, (
            "the pending exit must stay recorded so the kill switch knows the "
            "reconciler still owns this exit"
        )
        assert t._time_barrier_exit_attempts == 1, (
            "the BINDING CONDITION 1 defer must have advanced the retry budget "
            "exactly once"
        )
        assert t._time_barrier_exit_attempts < _MAX_TIME_BARRIER_EXIT_ATTEMPTS, (
            "budget must not be spent after a single deferral"
        )
        t.telemetry.close_position.assert_not_called()

        # First poll (budget unspent): the kill switch must DEFER, NOT flatten
        # — flattening here races the reconciler's cancel-and-defer.
        t.exec_client.reset_mock(return_value=True, side_effect=True)
        t.telemetry.reset_mock()
        t.exec_client.get_position.return_value = 1  # broker still shows +1
        t._reset_position_state = MagicMock()

        t._check_naked_position()

        # Flattening here (racing the reconciler's cancel-and-defer) is the
        # premature-flatten regression this ticket fixes.
        t.exec_client.close_position.assert_not_called()
        t.telemetry.close_position.assert_not_called()

        # Budget exhausted (attempts == _MAX) with the stop still None: the
        # kill switch releases as the ultimate net and flattens.
        t.exec_client.reset_mock(return_value=True, side_effect=True)
        t.telemetry.reset_mock()
        t._time_barrier_exit_attempts = _MAX_TIME_BARRIER_EXIT_ATTEMPTS
        assert t._pending_exit_order_id == _EXIT_OID  # still pending
        assert t._sl_order_id is None                 # still no stop
        t.exec_client.get_position.return_value = 1
        t._reset_position_state = MagicMock()

        t._check_naked_position()

        assert t.exec_client.close_position.called, (
            "at _MAX with no live stop the kill switch must flatten the naked "
            "position with a market order (ultimate net)"
        )
        assert (
            t.exec_client.close_position.call_args.kwargs.get("exit_mode")
            == "market"
        )
        assert t.telemetry.close_position.called, (
            "kill switch must book the ledger close"
        )
        assert (
            t.telemetry.close_position.call_args.kwargs.get("reason")
            == "NAKED_POSITION_KILL_SWITCH"
        )


# ---------------------------------------------------------------------------
# Case 2 — confirmed fill books the PROVEN price, never current_price
# ---------------------------------------------------------------------------


class TestConfirmedFillBooksProvenPrice:
    def test_confirmed_flat_books_execution_price_not_current_price(self):
        """settled -> 0 (flat: the exit filled) plus a matching execution
        record => book the CLOSE with the execution's proven price, NEVER the
        fabricated current_price, then reset with reason='TIME_BARRIER'.

        RED today: telemetry.close_position is booked with
        exit_price=current_price (2.911), not the proven 2.905."""
        t = _make_time_barrier_trader()
        t.exec_client.get_position_settled.return_value = 0  # flat: exit filled
        t.exec_client.get_executions.return_value = [
            _execution_record(_EXIT_OID, _PROVEN_PRICE)
        ]
        t._verify_and_heal_protective_legs = MagicMock()
        t._reset_position_state = MagicMock()

        result = _submit_then_reconcile(t)

        assert result is True, "a confirmed flat is a completed exit"
        t.telemetry.close_position.assert_called_once()
        kwargs = t.telemetry.close_position.call_args.kwargs
        assert kwargs.get("reason") == "TIME_BARRIER"
        assert kwargs.get("exit_price") == pytest.approx(_PROVEN_PRICE), (
            "the ledger must carry the PROVEN execution price (2.905), never "
            f"the fabricated current_price ({_CURRENT_PRICE})"
        )
        assert kwargs.get("exit_price") != pytest.approx(_CURRENT_PRICE)
        t._reset_position_state.assert_called_once_with(reason="TIME_BARRIER")
        # Flat -> nothing to re-arm.
        t._verify_and_heal_protective_legs.assert_not_called()

    def test_confirmed_flat_with_no_matching_execution_books_null(self):
        """Blueprint proven-price/NULL rule (the :2305-2313 precedent — 'never
        a fabricated price'): settled -> 0 but NO execution matches the exit
        order id (e.g. a day-boundary gap) => the exit price is NULL, an
        explicit unknown, never current_price.

        RED today: current_price is written unconditionally."""
        t = _make_time_barrier_trader()
        t.exec_client.get_position_settled.return_value = 0
        # An execution exists, but for a DIFFERENT order id -> no match.
        t.exec_client.get_executions.return_value = [
            _execution_record(999999, 2.80)
        ]
        t._verify_and_heal_protective_legs = MagicMock()
        t._reset_position_state = MagicMock()

        result = _submit_then_reconcile(t)

        assert result is True
        kwargs = t.telemetry.close_position.call_args.kwargs
        assert kwargs.get("exit_price") is None, (
            "with no matching execution the exit price must be NULL, never a "
            f"fabricated current_price ({_CURRENT_PRICE})"
        )
        t._reset_position_state.assert_called_once_with(reason="TIME_BARRIER")


# ---------------------------------------------------------------------------
# Case 3 — BINDING CONDITION 2: the cancel-reveals-the-fill race branch
# ---------------------------------------------------------------------------


class TestRaceBranchBindingCondition2:
    def test_settled_stale_then_cancel_zero_books_and_does_not_rearm(self):
        """BINDING CONDITION 2 (Reviewer, binding). The A1 settled snapshot
        pre-dates a fast fill (reads 1 = still holding), but
        cancel_orders_by_ids -> 0 proves the exit had ALREADY filled (a filled
        order has left openTrades()). The re-confirm then reads flat (0), so
        the path books the PROVEN execution price and MUST NOT re-arm a stop on
        a flat book.

        RED today: current_price is booked and cancel_orders_by_ids is never
        called. Also guards against a naive fix that re-arms on the stale
        settled=1 without the cancel-count re-confirm — that would leave a
        resting stop opening a naked reversal."""
        t = _make_time_barrier_trader()

        # settled: 1 on the first read (stale, pre-fill), 0 on re-confirm.
        settled_seq = [1, 0]

        def _settled(*args, **kwargs):
            return settled_seq.pop(0) if settled_seq else 0

        t.exec_client.get_position_settled.side_effect = _settled
        # cancel finds NOTHING open -> the exit already filled and left the book.
        t.exec_client.cancel_orders_by_ids.return_value = 0
        t.exec_client.get_executions.return_value = [
            _execution_record(_EXIT_OID, _PROVEN_PRICE)
        ]
        t._verify_and_heal_protective_legs = MagicMock()
        t._reset_position_state = MagicMock()

        result = _submit_then_reconcile(t)

        assert result is True, "the race resolves to a completed (flat) exit"
        t.exec_client.cancel_orders_by_ids.assert_called_once_with([_EXIT_OID])
        kwargs = t.telemetry.close_position.call_args.kwargs
        assert kwargs.get("reason") == "TIME_BARRIER"
        assert kwargs.get("exit_price") == pytest.approx(_PROVEN_PRICE), (
            "the flat race branch must book the proven execution price"
        )
        assert kwargs.get("exit_price") != pytest.approx(_CURRENT_PRICE)
        t._reset_position_state.assert_called_once_with(reason="TIME_BARRIER")
        # CRITICAL: a flat book must NOT get a re-armed stop (naked reversal).
        t._verify_and_heal_protective_legs.assert_not_called()


# ---------------------------------------------------------------------------
# Case 4 — unconfirmed settled -> fail closed
# ---------------------------------------------------------------------------


class TestUnconfirmedFailsClosed:
    def test_unconfirmed_settled_none_makes_no_state_changes(self):
        """settled -> None (unconfirmed: settle timeout/error) => FAIL CLOSED,
        mirroring the existing :1593-1601 precedent. No ledger write, no reset,
        the trade stays tracked, and protection is NOT re-armed (the exit is
        still live and can still fill).

        RED today: the fire-and-forget branch books the close, resets state,
        and returns True regardless of any broker confirmation."""
        t = _make_time_barrier_trader()
        t.exec_client.get_position_settled.return_value = None  # unconfirmed
        t._verify_and_heal_protective_legs = MagicMock()
        t._reset_position_state = MagicMock()

        result = _submit_then_reconcile(t)

        assert result is False, "an unconfirmed read must not report an exit"
        t.telemetry.close_position.assert_not_called()
        t._reset_position_state.assert_not_called()
        assert t._active_trade_id == "trade_71", "position stays tracked"
        # Exit may still fill -> never re-arm, never cancel it away.
        t._verify_and_heal_protective_legs.assert_not_called()
        t.exec_client.cancel_orders_by_ids.assert_not_called()


# ---------------------------------------------------------------------------
# Bounded pending-exit kill-switch guard
# (killswitch-pending-exit-guard_07202026_1805)
#
# The naked-position kill switch (_check_naked_position) must DEFER to the
# idle-loop reconciler while a TIME BARRIER exit is pending AND the reconciler's
# retry budget is unspent, then FLATTEN as the ultimate net once
# _time_barrier_exit_attempts reaches _MAX_TIME_BARRIER_EXIT_ATTEMPTS. The bound
# must be MONOTONIC PER POLL — it advances on EVERY unresolved reconciler poll,
# INCLUDING one whose broker calls throw — so the kill switch can never be
# suppressed forever (that was the hole that veto'd the unconditional-skip v1).
# ---------------------------------------------------------------------------


class _BrokerAPIThrow(RuntimeError):
    """A selective broker-API failure: the settled read still succeeds, but a
    downstream call (cancel_orders_by_ids / get_open_trades) keeps throwing. The
    persistent-failure case that must NOT freeze the retry budget."""


class TestKillSwitchBoundedPendingExitGuard:
    def test_kill_switch_defers_while_pending_exit_within_budget(self):
        """CONTRACT 1 — incident regression (false-flatten sentinel).

        MES-shaped defer state: settled -> 1 (still holding), cancel_orders_by_ids
        -> 1 (cancel-requested, not dead), and the exit is STILL in the open book
        (BINDING CONDITION 1 defer). The reconciler advances the budget to 1 and
        keeps the exit pending with _sl_order_id None. With a pending exit and an
        unspent budget the kill switch MUST NOT flatten — doing so races the
        reconciler's cancel-and-defer and market-closes a healthy mid-exit
        position (the exact 2026-07-20 real-money MES incident).

        RED today: with no pending-exit guard, _check_naked_position sees
        `_sl_order_id is None` + `pos != 0` and flattens immediately."""
        t = _make_time_barrier_trader()
        t.exec_client.get_position_settled.return_value = 1
        t.exec_client.cancel_orders_by_ids.return_value = 1
        t.exec_client.get_open_trades.return_value = [
            SimpleNamespace(order_id=_EXIT_OID, symbol="NG", status="Submitted")
        ]

        result = _submit_then_reconcile(t)

        # Post-reconcile state: mid-exit deferral window.
        assert result is False
        assert t._pending_exit_order_id == _EXIT_OID, (
            "the exit must stay recorded as pending for the reconciler"
        )
        assert t._time_barrier_exit_attempts == 1, (
            "the BINDING CONDITION 1 defer must have advanced the budget once"
        )
        assert t._time_barrier_exit_attempts < _MAX_TIME_BARRIER_EXIT_ATTEMPTS
        assert t._sl_order_id is None, (
            "no stop is re-armed while the exit can still fill "
            "(BINDING CONDITION 1)"
        )

        # Now the kill switch runs mid-exit. It MUST defer, not flatten.
        t.exec_client.reset_mock(return_value=True, side_effect=True)
        t.telemetry.reset_mock()
        t.exec_client.get_position.return_value = 1  # IBKR still shows +1
        t._reset_position_state = MagicMock()

        t._check_naked_position()

        t.exec_client.close_position.assert_not_called()
        t.telemetry.close_position.assert_not_called()

    def test_kill_switch_flattens_when_budget_exhausted(self):
        """CONTRACT 2 — ultimate-net preserved (fence).

        With a pending exit but the retry budget SPENT
        (_time_barrier_exit_attempts == _MAX_TIME_BARRIER_EXIT_ATTEMPTS), the stop
        still None, and IBKR reporting a live position, the kill switch MUST still
        flatten — the bounded guard releases exactly when the reconciler's A4
        pages a human, so a genuinely-stuck exit is never left backstopped only by
        human escalation.

        This fence guards the fix against OVER-reaching (skipping forever). It is
        NOT tautological: the _MAX budget is imported from the source module and
        the pending-exit attribute is set, so a fix that made the guard skip
        unconditionally would flip this assertion to a failure."""
        t = _make_time_barrier_trader()
        # Mid-exit deferral window with the budget exhausted.
        t._pending_exit_order_id = _EXIT_OID
        t._time_barrier_exit_attempts = _MAX_TIME_BARRIER_EXIT_ATTEMPTS
        t._sl_order_id = None
        t.exec_client.get_position.return_value = 1  # IBKR shows a live position
        t._reset_position_state = MagicMock()

        t._check_naked_position()

        assert t.exec_client.close_position.called, (
            "at _MAX with no live stop the kill switch must flatten as the "
            "ultimate net"
        )
        assert (
            t.exec_client.close_position.call_args.kwargs.get("exit_mode")
            == "market"
        )
        assert t.telemetry.close_position.called, "the ledger close must be booked"
        assert (
            t.telemetry.close_position.call_args.kwargs.get("reason")
            == "NAKED_POSITION_KILL_SWITCH"
        )

    def test_persistent_broker_failure_still_advances_budget_and_terminates(self):
        """CONTRACT 3 — persistent-broker-failure termination (the hole that
        veto'd v1).

        The settled read keeps succeeding (position still open, settled != 0) but
        the downstream cancel_orders_by_ids RAISES on EVERY reconciler poll. The
        never-raise except boundary must still ADVANCE the retry budget so the
        bound is monotonic per poll — otherwise a selective broker-API failure
        freezes _time_barrier_exit_attempts below _MAX forever, suppressing BOTH
        the kill switch AND the A4 human escalation (strictly worse than status
        quo). This proves the bound cannot freeze and the system always
        terminates.

        RED today: the except boundary logs and returns False WITHOUT advancing
        the budget, so _time_barrier_exit_attempts stays 0 across every poll — the
        counter freezes and the kill switch is suppressed indefinitely."""
        t = _make_time_barrier_trader()
        # Post-submit deferred state (mirror _check_time_barrier: exit pending,
        # protective stop already cleared) established directly so each poll's
        # budget advance is countable from a known zero.
        t._pending_exit_order_id = _EXIT_OID
        t._sl_order_id = None
        assert t._time_barrier_exit_attempts == 0
        # settled read succeeds and reports STILL OPEN so control reaches the
        # cancel call...
        t.exec_client.get_position_settled.return_value = 1
        t.exec_client.get_position.return_value = 1
        # ...which throws on every poll.
        t.exec_client.cancel_orders_by_ids.side_effect = _BrokerAPIThrow(
            "cancelOrder RPC unavailable"
        )
        t._reset_position_state = MagicMock()

        # (a) + (b) + (c): every unresolved poll advances the budget by exactly
        # one DESPITE the throw, never raises out (returns False), and reaches
        # _MAX within _MAX polls.
        for i in range(_MAX_TIME_BARRIER_EXIT_ATTEMPTS):
            result = t._reconcile_pending_position_state()
            assert result is False, (
                "the never-raise boundary must swallow the broker throw and "
                "DEFER (return False), never raise into the event loop"
            )
            assert t._time_barrier_exit_attempts == i + 1, (
                "the retry budget must advance by exactly one per poll even when "
                "the broker call throws — otherwise the bound freezes and the "
                "kill switch is suppressed forever"
            )
            # The pending exit is retained across the throw so the branch is
            # re-entered next poll.
            assert t._pending_exit_order_id == _EXIT_OID

        assert t._time_barrier_exit_attempts == _MAX_TIME_BARRIER_EXIT_ATTEMPTS, (
            "the budget must reach _MAX within _MAX polls (bounded termination)"
        )

        # (d): budget now spent -> the kill switch releases and flattens, so the
        # system TERMINATES in an auto-flatten rather than an infinite quiet loop.
        t.exec_client.reset_mock(return_value=True, side_effect=True)
        t.telemetry.reset_mock()
        t.exec_client.get_position.return_value = 1
        t._reset_position_state = MagicMock()

        t._check_naked_position()

        assert t.exec_client.close_position.called, (
            "once the budget is exhausted the kill switch must flatten — the "
            "bound guarantees termination"
        )
        assert (
            t.telemetry.close_position.call_args.kwargs.get("reason")
            == "NAKED_POSITION_KILL_SWITCH"
        )
