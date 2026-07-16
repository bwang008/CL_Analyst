"""
TDD-TESTER AUTHORIZATION
Target Implementation File: src/live_execution/live_trader.py
Target Class/Function: LiveTrader._check_time_barrier (the TIME BARRIER exit
                       branch, live_trader.py:1679-1725)
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)
Ticket: exit-fill-unverified_07152026_1855

Regression suite for the fire-and-forget TIME BARRIER exit that left a NAKED +
UNTRACKED live position (2026-07-14 NG incident, client_id 3000).

The defect: the TIME BARRIER exit at live_trader.py:1679-1725 cancels the
protective SL/TP, books the ledger CLOSED with a FABRICATED price
(exit_price=current_price), and resets position state — all WITHOUT confirming
the exit order actually filled. When the exit (a GTC limit priced off a stale
bar close) never fills, the position is left naked, and BOTH safety nets are
disarmed by the same reset (kill switch needs _active_trade_id; housekeeping
heal needs an OPEN ledger row).

The invariant this ticket establishes (blueprint):
    Never book a close, never reset position state, and never re-arm protection
    until the broker has been asked and has answered. Book only on a *confirmed*
    flat with a *proven* fill price; re-arm the stop only once no exit order
    that could still fill is live AND the position is confirmed still open.

Four cases (deterministic, no real I/O — LiveTrader built via object.__new__,
exec_client/telemetry stubbed, mirroring tests/test_exit_reason_and_fill_routing.py
and tests/test_reconnect_false_flat_recovery.py):

  1. Reproduce the incident: settled shows STILL HOLDING -> do NOT book, do NOT
     reset, keep tracked, retire the stranded exit by id BEFORE re-arming, and
     the (unchanged) kill switch fires for free on the next poll.
  2. Confirmed fill books the *proven* execution price, never current_price.
  3. BINDING CONDITION 2 (the race): settled=1 but cancel_orders_by_ids -> 0
     (the exit had already filled) => book the proven price, do NOT re-arm.
  4. Unconfirmed (settled -> None) fails closed: no book, no reset, stays
     tracked, no re-arm.

These tests FAIL against the current fire-and-forget code (Red phase) and pass
only once the blueprint's Site A fix is implemented.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from src.live_execution.live_trader import LiveTrader


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

        result = _run_barrier(t)

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

    def test_kill_switch_fires_for_free_because_trade_stays_tracked(self):
        """Free consequence (blueprint): because the fixed exit keeps
        _active_trade_id set while _sl_order_id is None during the deferral
        window, the UNCHANGED kill switch's guards (:5776/:5782) both pass and
        it flattens the naked position on the next 5-minute poll.

        RED today: the fire-and-forget reset nulls _active_trade_id (:1724),
        so _check_naked_position returns at its first guard and NEVER flattens
        (grep 'KILL SWITCH' fleet_20260714.log -> 0)."""
        t = _make_time_barrier_trader()
        t.exec_client.get_position_settled.return_value = 1
        t.exec_client.cancel_orders_by_ids.return_value = 1
        # The exit rests forever (never leaves the open book) -> deferral:
        # stay tracked, _sl_order_id None, no re-arm, retry next bar.
        t.exec_client.get_open_trades.return_value = [
            SimpleNamespace(order_id=_EXIT_OID, symbol="NG", status="Submitted")
        ]

        result = _run_barrier(t)

        assert result is False
        # The exit path left the trade tracked with no live stop — exactly the
        # state the kill switch is designed to catch.
        assert t._active_trade_id == "trade_71", (
            "the fix must keep _active_trade_id set so the kill switch can "
            "re-arm for free"
        )
        assert t._sl_order_id is None, (
            "no stop may be re-armed while the exit can still fill "
            "(BINDING CONDITION 1)"
        )
        t.telemetry.close_position.assert_not_called()

        # Next poll: the kill switch (unchanged) must now FIRE and flatten.
        t.exec_client.reset_mock(return_value=True, side_effect=True)
        t.telemetry.reset_mock()
        t.exec_client.get_position.return_value = 1  # broker still shows +1
        t._reset_position_state = MagicMock()

        t._check_naked_position()

        assert t.exec_client.close_position.called, (
            "kill switch must flatten the naked position with a market order"
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

        result = _run_barrier(t)

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

        result = _run_barrier(t)

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

        result = _run_barrier(t)

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

        result = _run_barrier(t)

        assert result is False, "an unconfirmed read must not report an exit"
        t.telemetry.close_position.assert_not_called()
        t._reset_position_state.assert_not_called()
        assert t._active_trade_id == "trade_71", "position stays tracked"
        # Exit may still fill -> never re-arm, never cancel it away.
        t._verify_and_heal_protective_legs.assert_not_called()
        t.exec_client.cancel_orders_by_ids.assert_not_called()
