"""Ticket time-barrier-retire-loop_07282026_2150.

Live incident 2026-07-28 18:00-21:4x PT: the pending-exit branch cancelled a
5-second-old TIME BARRIER exit (A2 retire), and the died-without-filling
path never cleared `_pending_exit_order_id` (exit-fill-confirm follow-up
#6) — the reconciler re-processed the dead id every ~6s for 3.5h (~2,090
cycles) while `_note_time_barrier_deferral` alerted on EVERY attempt >= max
(~2,100 Telegram messages). Three fixes pinned here:

1. `_route_retired_time_barrier_exit` still-open path CLEARS the pending
   exit state after re-arming (fail-closed path retains it).
2. Grace window: the pending branch does not retire an exit younger than
   `_PENDING_EXIT_GRACE_SECONDS` (30s) — defer without cancel.
3. `_note_time_barrier_deferral` alerts AT the max and every 120th attempt
   after, never per-tick.
"""

import logging
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import src.live_execution.live_trader as lt_mod
from src.live_execution.live_trader import LiveTrader


def _trader(*, pending_oid=81, submitted_age=None, settled=1):
    lt = object.__new__(LiveTrader)
    lt._execution_symbol = "MES"
    lt._active_trade_id = "trade_76"
    lt._position_side = 1
    lt._retiring_leg_ids = []
    lt._retiring_sl_id = None
    lt._pending_exit_order_id = pending_oid
    lt._pending_exit_reason = "TIME_BARRIER"
    lt._pending_exit_submitted_at = (
        None if submitted_age is None else time.monotonic() - submitted_age
    )
    lt._time_barrier_exit_attempts = 0
    lt._processed_exit_order_ids = {str(pending_oid)}
    lt._confirm_settled_position = MagicMock(return_value=settled)
    lt._rearm_time_barrier_protection = MagicMock()
    lt._note_time_barrier_deferral = MagicMock()
    lt._book_time_barrier_flat = MagicMock(return_value=True)
    lt._route_retired_time_barrier_exit = MagicMock(return_value=False)
    lt.exec_client = MagicMock()
    lt.exec_client.get_position.return_value = 1
    lt.exec_client.cancel_orders_by_ids.return_value = 1
    lt.exec_client.get_open_trades.return_value = []
    lt.telemetry = MagicMock()
    lt._telegram = MagicMock()
    return lt


# ---------------------------------------------------------------------------
# Fix 1 — died-without-filling clears the pending exit (the loop-killer)
# ---------------------------------------------------------------------------


class TestRouteRetiredClearsPendingState:
    def _route_trader(self, settled):
        lt = _trader()
        # exercise the REAL route method; shadow only its collaborators
        del lt.__dict__["_route_retired_time_barrier_exit"]
        lt._confirm_settled_position = MagicMock(return_value=settled)
        return lt

    def test_still_open_rearm_path_clears_pending_state(self):
        lt = self._route_trader(settled=1)
        r = lt._route_retired_time_barrier_exit(81, 1)
        assert r is False
        lt._rearm_time_barrier_protection.assert_called_once_with(1)
        assert lt._pending_exit_order_id is None, (
            "follow-up #6: the died-without-filling path must clear the "
            "pending id so the reconciler stops re-processing the dead exit"
        )
        assert lt._pending_exit_reason is None
        assert lt._pending_exit_submitted_at is None
        lt._note_time_barrier_deferral.assert_called_once()

    def test_fail_closed_path_retains_pending_state(self):
        lt = self._route_trader(settled=None)
        r = lt._route_retired_time_barrier_exit(81, 1)
        assert r is False
        lt._rearm_time_barrier_protection.assert_not_called()
        assert lt._pending_exit_order_id == 81, (
            "fail-closed (unconfirmed settle) must RETAIN state for the "
            "next-tick settle retry"
        )

    def test_settled_zero_books_with_registered_reason(self):
        lt = self._route_trader(settled=0)
        lt._book_time_barrier_flat = MagicMock(return_value=True)
        assert lt._route_retired_time_barrier_exit(81, 1) is True
        lt._book_time_barrier_flat.assert_called_once_with(81, "TIME_BARRIER")


# ---------------------------------------------------------------------------
# Fix 2 — grace window before retiring a young exit
# ---------------------------------------------------------------------------


class TestPendingExitGraceWindow:
    def test_young_exit_is_not_cancelled(self):
        lt = _trader(submitted_age=2.0)  # 2s old, grace 30s
        r = lt._reconcile_pending_position_state()
        assert r is False
        lt.exec_client.cancel_orders_by_ids.assert_not_called()
        lt._note_time_barrier_deferral.assert_not_called()
        assert lt._pending_exit_order_id == 81

    def test_old_exit_proceeds_to_retire(self):
        lt = _trader(submitted_age=45.0)  # past grace
        lt._reconcile_pending_position_state()
        lt.exec_client.cancel_orders_by_ids.assert_called_once_with([81])

    def test_none_timestamp_proceeds_legacy(self):
        lt = _trader(submitted_age=None)
        lt._reconcile_pending_position_state()
        lt.exec_client.cancel_orders_by_ids.assert_called_once_with([81])

    def test_filled_during_grace_still_books(self):
        # settled==0 must book regardless of age — grace only guards the
        # cancel, never the fill booking.
        lt = _trader(submitted_age=2.0, settled=0)
        lt._reconcile_pending_position_state()
        lt._book_time_barrier_flat.assert_called_once_with(81, "TIME_BARRIER")

    def test_registration_stamps_submitted_at(self):
        lt = _trader()
        lt._register_pending_exit(99, reason="TIME_BARRIER")
        assert lt._pending_exit_order_id == 99
        assert lt._pending_exit_submitted_at is not None
        assert time.monotonic() - lt._pending_exit_submitted_at < 5.0


# ---------------------------------------------------------------------------
# Fix 3 — alert throttle
# ---------------------------------------------------------------------------


class TestDeferralAlertThrottle:
    def _lt(self):
        lt = object.__new__(LiveTrader)
        lt._execution_symbol = "MES"
        lt._active_trade_id = "trade_76"
        lt._time_barrier_exit_attempts = 0
        lt._telegram = MagicMock()
        lt._emit_health_event = MagicMock()
        return lt

    def test_alerts_at_max_then_every_120th_only(self):
        lt = self._lt()
        MAX = lt_mod._MAX_TIME_BARRIER_EXIT_ATTEMPTS
        EVERY = lt_mod._TIME_BARRIER_ALERT_EVERY
        total = MAX + EVERY + 5
        for _ in range(total):
            lt._note_time_barrier_deferral(81)
        # exactly 2 alerts: at MAX and at MAX+EVERY
        assert lt._telegram.send.call_count == 2, (
            f"expected alerts only at attempt {MAX} and {MAX + EVERY}, got "
            f"{lt._telegram.send.call_count} over {total} attempts (the "
            f"2026-07-28 flood was one alert per attempt)"
        )
        assert lt._emit_health_event.call_count == 2

    def test_below_max_stays_silent(self):
        lt = self._lt()
        for _ in range(lt_mod._MAX_TIME_BARRIER_EXIT_ATTEMPTS - 1):
            lt._note_time_barrier_deferral(81)
        lt._telegram.send.assert_not_called()
        lt._emit_health_event.assert_not_called()
