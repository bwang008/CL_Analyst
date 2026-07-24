"""
Ticket: rollover-close-fill-registration_07232026_1920
Target Implementation File: src/live_execution/live_trader.py
Target Class/Function: LiveTrader._check_contract_rollover (force-close branch
                       registers the close order id + reason with the
                       pending-exit machinery instead of dropping it),
                       LiveTrader._register_pending_exit (NEW: atomic
                       order-id/close-reason registration authority),
                       LiveTrader._book_time_barrier_flat (reason threaded
                       explicitly — no silent TIME_BARRIER default),
                       LiveTrader._recover_oob_close (rollover-cancelled legs
                       are ACCOUNTED, never "possible live orphan")

Live incident 2026-07-23 ~17:00 PT (NG NGQ26->NGU26 roll, short -1): the
ROLLOVER FORCE-CLOSE worked at the broker (brackets 116/117 cancelled, MARKET
BUY order 120 filled 2.90, flat confirmed) but the bookkeeping failed: order
120 was never registered with the fill router, so its fill logged
"[TRADE] UNRECOGNIZED FILL ... ignoring" (2x ERROR), the :15 sweep found
trade_115 OPEN vs broker-flat, and the OOB recovery — which matches broker
executions against the tracked leg ids only (tp=116/sl=117, both CANCELLED so
no executions) — closed the row CLOSED_OOB_UNRECOVERED exit_price=None and
paged "UNACCOUNTED - possible live orphan. Verify and cancel manually in TWS."
The operator repaired the row by hand.

Required behavior (approved blueprint):
  1. The rollover close order's fill books the close DIRECTLY and truthfully:
     exit_price=<proven fill>, close_reason="ROLLOVER_FORCE_CLOSE", position
     state reset via the normal close path (_reset_position_state with that
     reason — the cooldown/on_exit machinery treats it per cooldown_arming:
     NOT SL-family, so "sl_only" sides do not arm; "all" sides do).
  2. No UNRECOGNIZED FILL lines for the close order.
  3. The sweep/OOB recovery treats rollover-cancelled legs as ACCOUNTED
     (cancelled-by-rollover), not "possible live orphan" — while a GENUINELY
     unknown resting order still warns (asymmetry preserved).
  4. A roll with NO position stays byte-identical.

Stub pattern: object.__new__ LiveTrader with only the seams each method reads
(mirrors tests/test_live_trader_bugs.py and tests/test_oob_entry_state_recovery.py).
Every I/O boundary is a MagicMock — no broker, no DB, no fleet artifacts.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from src.live_execution.live_trader import LiveTrader
from src.live_execution.interfaces.execution_interface import (
    StandardExecutionEvent,
)
from src.live_execution.strategies.execution_models import (
    exit_reason_arms_cooldown,
)


# ---------------------------------------------------------------------------
# Incident constants (NG NGQ26->NGU26 roll, 2026-07-23)
# ---------------------------------------------------------------------------

_TRADE_ID = "trade_115"
_TP_OID = 116
_SL_OID = 117
_CLOSE_OID = 120
_CLOSE_FILL = 2.90


def _execution_record(order_id, price, *, symbol="NG"):
    """A broker execution record per the get_executions contract
    (execution_interface.py): order_id is a str on purpose."""
    return {
        "order_id": str(order_id),
        "perm_id": 8842,
        "exec_id": f"exec-{order_id}",
        "price": price,
        "qty": 1,
        "side": "BOT",
        "time": "2026-07-23T17:00:02",
        "symbol": symbol,
        "commission_report": None,
    }


def _close_fill_event(order_id=_CLOSE_OID, price=_CLOSE_FILL):
    """The Filled event IBKR fires for the rollover market close order."""
    raw = SimpleNamespace(
        order=SimpleNamespace(
            action="BUY", permId=7301, parentId=0, account="DU1",
        ),
    )
    return StandardExecutionEvent(
        order_id=str(order_id),
        symbol="NGQ26",
        status="Filled",
        filled_qty=1,
        remaining_qty=0,
        avg_price=price,
        raw_event=raw,
    )


def _rollover_trader(position=-1):
    """A LiveTrader mid-session holding ``position`` on the expiring contract,
    one rollover check away from detecting the NGQ26->NGU26 flip."""
    t = object.__new__(LiveTrader)

    # Rollover detection seams.
    t._last_rollover_check_date = None
    t._rollover_in_progress = False
    t.data_client = MagicMock()
    t.data_client.get_front_month_contract.return_value = ("NGU6", "202609")
    t._front_month_local_symbol = "NGQ6"
    t._front_month_str = "202608"
    t._front_month_last_close = 2.95
    t._front_month_bars = None
    t._subscribe_front_month = MagicMock()
    t.data_manager_5m = None
    t.data_manager_1h = None
    t._execution_symbol = "NG"
    t._exit_mode = "marketable_limit"

    # Broker seams.
    t.exec_client = MagicMock()
    t.exec_client.get_position.return_value = position
    t.exec_client.cancel_open_orders.return_value = 2
    t.exec_client.close_position.return_value = SimpleNamespace(
        order=SimpleNamespace(orderId=_CLOSE_OID),
    )
    t.exec_client.get_open_trades.return_value = []
    t.exec_client.get_executions.return_value = []

    t.telemetry = MagicMock()
    t._telegram = MagicMock()
    t.strategy = MagicMock()
    t._emit_health_event = MagicMock()

    # Tracked in-position state (the incident: short -1, legs 116/117).
    holding = position != 0
    t._active_trade_id = _TRADE_ID if holding else None
    t._position_side = -1 if position < 0 else (1 if position > 0 else 0)
    t._position_entry_bar_time = pd.Timestamp("2026-07-23 10:00:00")
    t._position_bars_held = 7
    t._trailing_activated = False
    t._entry_price = 2.95
    t._atr_at_entry = 0.03
    t._highest_high = 2.97
    t._lowest_low = 2.88
    t._trade_trailing_atr_mult = None
    t._trade_max_hold_bars = None
    t._tp_order_ids = [_TP_OID] if holding else []
    t._sl_order_id = _SL_OID if holding else None
    t._tracked_tp_price = 2.80 if holding else None
    t._tracked_sl_price = 3.05 if holding else None

    # Entry / fill-router seams.
    t._pending_entry_order_id = None
    t._pending_entry_bar_time = None
    t._open_orders = {}
    t._last_decision_context_by_order_id = {}
    t._processed_exit_order_ids = set()
    t._processed_entry_order_ids = set()
    t._entry_order_ids = set()
    t._recently_closed_legs = None
    t._partial_fill_signatures = set()

    # Pending-exit machinery state.
    t._time_barrier_exit_attempts = 0
    t._pending_exit_order_id = None
    t._pending_exit_reason = None
    t._retiring_leg_ids = []
    t._retiring_sl_id = None
    t._kill_switch_cancel_confirm_attempts = 0

    t.rolling_df_5m = None
    t.rolling_df_1h = None

    # Deterministic identity seams.
    t._utc_iso_now = MagicMock(return_value="2026-07-23T17:00:05")
    t._build_event_id = MagicMock(return_value="evt-roll")
    t._base_tradebook_fields = MagicMock(return_value={})
    return t


# ---------------------------------------------------------------------------
# Case 1 — the close order is registered; its fill books the close truthfully
# ---------------------------------------------------------------------------


class TestRolloverCloseRegistration:
    def test_close_order_is_registered_with_pending_exit_machinery(self):
        """The force-close must REGISTER the returned close order id + reason
        ROLLOVER_FORCE_CLOSE with the pending-exit machinery (the TIME BARRIER
        mechanism, reused) instead of dropping it and resetting inline.

        RED today: the close order id is dropped, _reset_position_state runs
        immediately (reason='ROLLOVER'), and nothing is registered."""
        t = _rollover_trader(position=-1)

        t._check_contract_rollover()

        # The market close was submitted...
        t.exec_client.close_position.assert_called_once()
        assert (
            t.exec_client.close_position.call_args.kwargs.get("exit_mode")
            == "market"
        )
        # ...and REGISTERED: id + reason paired, fill recognizable.
        assert t._pending_exit_order_id == _CLOSE_OID
        assert t._pending_exit_reason == "ROLLOVER_FORCE_CLOSE"
        assert str(_CLOSE_OID) in t._processed_exit_order_ids
        # The trade stays TRACKED until the fill is proven — no inline reset,
        # no premature strategy.on_exit (cooldown fires at booking).
        assert t._active_trade_id == _TRADE_ID
        assert not t.strategy.on_exit.called
        t.telemetry.close_position.assert_not_called()
        # The cancelled bracket ids no longer masquerade as live protection.
        assert t._sl_order_id is None
        assert t._tp_order_ids == []
        # The rollover continuation still ran (contract references updated).
        assert t._front_month_local_symbol == "NGU6"

    def test_rollover_records_cancelled_legs_for_accounting(self):
        """The roll must record the leg ids it cancels (reason 'ROLLOVER') in
        _recently_closed_legs BEFORE clearing them, so the sweep's
        unaccounted-leg check and the Stage-2 residual branch both recognize
        them. RED today: the reset-time snapshot carries reason='ROLLOVER'
        only as a side effect and the recovery never consults it."""
        t = _rollover_trader(position=-1)

        t._check_contract_rollover()

        legs = t._recently_closed_legs
        assert legs is not None
        assert legs["reason"] == "ROLLOVER"
        assert legs["trade_id"] == _TRADE_ID
        assert legs["leg_ids"] == {str(_TP_OID), str(_SL_OID)}

    def test_close_fill_is_recognized_no_unrecognized_fill_log(self, caplog):
        """The close order's Filled event must be RECOGNIZED (registered exit
        id) — never '[TRADE] UNRECOGNIZED FILL ... ignoring'. Booking stays
        with the idle reconciler (settled proof), so the fill callback books
        nothing itself.

        RED today: order 120 is unknown to the router and dies in the
        UNRECOGNIZED FILL error path (the incident's 2x ERROR)."""
        t = _rollover_trader(position=-1)
        t._check_contract_rollover()

        with caplog.at_level(logging.DEBUG):
            t._on_standard_execution_event(_close_fill_event())

        unrecognized = [
            r for r in caplog.records if "UNRECOGNIZED FILL" in r.message
        ]
        assert not unrecognized, (
            f"rollover close fill hit the UNRECOGNIZED FILL path: "
            f"{[r.message for r in unrecognized]!r}"
        )
        # Booking belongs to the reconciler (proven settled + execution).
        t.telemetry.close_position.assert_not_called()
        assert t._active_trade_id == _TRADE_ID

    def test_reconciler_books_proven_fill_with_rollover_reason(self):
        """The idle reconciler's pending-exit branch books the registered
        close: exit_price = the PROVEN execution price, close reason
        ROLLOVER_FORCE_CLOSE, position state reset via the normal close path
        (strategy.on_exit with the same reason).

        RED today: nothing is pending after a rollover — the reconciler's
        flat-read branch sees no tracked trade (already reset) and books
        nothing; the sweep later writes CLOSED_OOB_UNRECOVERED/None."""
        t = _rollover_trader(position=-1)
        t._check_contract_rollover()

        # Post-fill broker truth: flat, with the close's execution on record.
        t.exec_client.get_position.return_value = 0
        t.exec_client.get_position_settled.return_value = 0
        t.exec_client.get_executions.return_value = [
            _execution_record(_CLOSE_OID, _CLOSE_FILL),
        ]

        result = t._reconcile_pending_position_state()

        assert result is True, "a confirmed flat is a completed exit"
        t.telemetry.close_position.assert_called_once()
        call = t.telemetry.close_position.call_args
        assert call.args[0] == _TRADE_ID
        assert call.kwargs.get("reason") == "ROLLOVER_FORCE_CLOSE"
        assert call.kwargs.get("exit_price") == pytest.approx(_CLOSE_FILL)
        # Normal close path: strategy notified with the truthful reason.
        t.strategy.on_exit.assert_called_once()
        side, reason, bars = t.strategy.on_exit.call_args.args
        assert side == -1
        assert reason == "ROLLOVER_FORCE_CLOSE"
        assert bars == 7
        # Full reset ran; the pending registration is consumed.
        assert t._active_trade_id is None
        assert t._position_side == 0
        assert t._pending_exit_order_id is None
        assert t._pending_exit_reason is None
        # The rollover leg record SURVIVES the reset (no legs tracked at
        # reset time) so the sweep's accounting still recognizes 116/117.
        assert t._recently_closed_legs is not None
        assert t._recently_closed_legs["reason"] == "ROLLOVER"


# ---------------------------------------------------------------------------
# Case 2 — sweep/OOB recovery: rollover-cancelled legs are ACCOUNTED
# ---------------------------------------------------------------------------


class TestRolloverLegAccounting:
    def _swept_trader(self):
        """Post-rollover trader as the :15 sweep would find it if the fill
        booking had not landed yet: legs 116/117 rollover-cancelled (recorded),
        broker flat, only the close order's execution on record."""
        t = _rollover_trader(position=-1)
        t._check_contract_rollover()
        t.exec_client.cancel_orders_by_ids.return_value = 0
        t.exec_client.cancel_open_orders.return_value = 0
        t.exec_client.get_executions.return_value = [
            _execution_record(_CLOSE_OID, _CLOSE_FILL),
        ]
        t._telegram.reset_mock()
        t.telemetry.reset_mock()
        return t

    def test_rollover_cancelled_legs_are_accounted_no_orphan_page(
        self, caplog,
    ):
        """The incident's false alarm: both legs were provably cancelled by
        the roll, yet the recovery paged 'UNACCOUNTED ... possible live
        orphan. Verify and cancel manually in TWS.' Rollover-recorded legs
        must count as accounted — no ERROR, no Telegram page.

        RED today: _recover_oob_close never consults the rollover record."""
        t = self._swept_trader()

        with caplog.at_level(logging.DEBUG):
            reason, price = t._recover_oob_close(
                trade_id=_TRADE_ID,
                tp_order_id=_TP_OID,
                sl_order_id=_SL_OID,
            )

        unaccounted = [
            r for r in caplog.records if "UNACCOUNTED" in r.message
        ]
        assert not unaccounted, (
            f"rollover-cancelled legs paged as live orphans: "
            f"{[r.message for r in unaccounted]!r}"
        )
        t._telegram.send.assert_not_called()
        # The defense-in-depth close itself is unchanged (honest unknown).
        assert reason == "CLOSED_OOB_UNRECOVERED"
        assert price is None

    def test_genuinely_unknown_leg_still_warns_asymmetry_preserved(
        self, caplog,
    ):
        """FENCE + asymmetry: with one rollover-accounted leg (116) and one
        GENUINELY unknown order (999, absent from the rollover record), the
        orphan hazard must still page — the accounting is narrow, never a
        blanket suppression."""
        t = self._swept_trader()

        with caplog.at_level(logging.DEBUG):
            t._recover_oob_close(
                trade_id=_TRADE_ID,
                tp_order_id=_TP_OID,
                sl_order_id=999,
            )

        unaccounted = [
            r
            for r in caplog.records
            if "UNACCOUNTED" in r.message and r.levelno >= logging.ERROR
        ]
        assert unaccounted, (
            "a genuinely unknown resting order must still page as a possible "
            "live orphan"
        )
        assert t._telegram.send.called

    def test_non_rollover_record_does_not_account(self, caplog):
        """FENCE: a recently-closed-legs record from a NORMAL close (reason
        TP_HIT) must not account for anything — only cancelled-by-rollover
        legs are recognized."""
        t = self._swept_trader()
        t._recently_closed_legs = dict(
            t._recently_closed_legs, reason="TP_HIT",
        )

        with caplog.at_level(logging.DEBUG):
            t._recover_oob_close(
                trade_id=_TRADE_ID,
                tp_order_id=_TP_OID,
                sl_order_id=_SL_OID,
            )

        unaccounted = [
            r
            for r in caplog.records
            if "UNACCOUNTED" in r.message and r.levelno >= logging.ERROR
        ]
        assert unaccounted, (
            "only reason='ROLLOVER' records may account for missing legs"
        )


# ---------------------------------------------------------------------------
# Case 3 — flat roll: byte-identical (no close order, no registrations)
# ---------------------------------------------------------------------------


class TestFlatRollUnchanged:
    def test_flat_roll_makes_no_close_and_no_registrations(self):
        """A roll with NO open position must not submit a close, register
        nothing with the pending-exit machinery, record no legs, and fire no
        cooldown — the clean-transition path stays as it was."""
        t = _rollover_trader(position=0)

        t._check_contract_rollover()

        t.exec_client.close_position.assert_not_called()
        assert t._pending_exit_order_id is None
        assert t._pending_exit_reason is None
        assert t._processed_exit_order_ids == set()
        assert t._recently_closed_legs is None
        assert not t.strategy.on_exit.called
        t.telemetry.close_position.assert_not_called()
        # Transition still completes.
        assert t._front_month_local_symbol == "NGU6"
        assert t._telegram.send.called


# ---------------------------------------------------------------------------
# Case 4 — reason threading (no silent defaults; cooldown family semantics)
# ---------------------------------------------------------------------------


class TestReasonThreading:
    def test_rollover_force_close_is_not_sl_family(self):
        """The booked close reason is NOT SL-family: 'sl_only' sides must not
        arm the re-entry cooldown on a rollover force-close; 'all' sides do
        (the existing predicate's semantics, no special case)."""
        assert (
            exit_reason_arms_cooldown("ROLLOVER_FORCE_CLOSE", "sl_only")
            is False
        )
        assert (
            exit_reason_arms_cooldown("ROLLOVER_FORCE_CLOSE", "all") is True
        )

    def test_register_pending_exit_pairs_id_and_reason(self):
        """The registration authority pairs the order id with its close
        reason atomically. RED today: _register_pending_exit does not exist."""
        t = _rollover_trader(position=-1)

        t._register_pending_exit(71, reason="TIME_BARRIER")

        assert t._pending_exit_order_id == 71
        assert t._pending_exit_reason == "TIME_BARRIER"
        assert "71" in t._processed_exit_order_ids

    def test_register_pending_exit_rejects_missing_reason(self):
        """No silent defaults: registering a pending exit without an explicit
        close reason is a programming error and must raise."""
        t = _rollover_trader(position=-1)

        with pytest.raises(ValueError):
            t._register_pending_exit(71, reason="")
        with pytest.raises(ValueError):
            t._register_pending_exit(71, reason=None)

    def test_booking_without_registered_reason_raises(self):
        """No silent defaults at the booking site either: a pending exit that
        reaches booking without a registered reason must raise loudly, never
        silently book TIME_BARRIER."""
        t = _rollover_trader(position=-1)

        with pytest.raises(ValueError):
            t._book_time_barrier_flat(_CLOSE_OID, None)
