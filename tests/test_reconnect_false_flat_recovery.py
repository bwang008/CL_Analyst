"""reconnect-false-flat-oob_07082026_0731 — regression tests.

Incident (2026-07-08 07:27:28 PT): on a reconnect the SI child read
ib_insync's in-memory position cache as FLAT (the account-update stream had
not arrived), concluded an out-of-band close, and CANCELLED the live SL/TP of
a still-open full-size silver short — leaving it naked and unmanaged.

Root cause: ``get_position`` reads ``self.ib.positions()`` (an in-memory cache
that is empty until the async stream lands after (re)connect); the recovery /
time-barrier / startup-sweep paths trusted a single ``==0`` from it to cancel
protective orders. Fix: confirm a SETTLED snapshot (fresh reqPositions bounded
by a timeout) before cancelling / declaring OOB, and FAIL CLOSED (retain
protection) when it cannot be confirmed.

These tests pin: (1) the settled-read primitive uses the async request under a
timeout (never a blocking reqPositions, never ib.sleep); (2) recovery no longer
cancels legs / marks OOB on a false flat, still does on a CONFIRMED flat, and
retains protection on an UNCONFIRMED read; (3) the same guard on the mid-session
time-barrier and the startup orphan sweep.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import src.live_execution.live_trader as lt_module
from src.live_execution.adapters.simulated_execution import SimulatedExecution
from src.live_execution.ibkr_client import IBKRConnectionManager
from src.live_execution.interfaces.execution_interface import ExecutionClient


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def _pos(symbol, trading_class, position):
    return SimpleNamespace(
        contract=SimpleNamespace(symbol=symbol, tradingClass=trading_class),
        position=position,
    )


class _FakeIB:
    """Minimal ib_insync stand-in for the settled-read primitive.

    reqPositionsAsync() resolves after ``delay`` seconds (to force a timeout);
    ``positions()`` returns the configured snapshot. reqPositions() (the
    BLOCKING variant, which must NOT be used) records if it is ever called.
    """

    def __init__(self, positions, *, delay=0.0):
        self._positions = positions
        self._delay = delay
        self.blocking_reqPositions_called = False
        self.reqPositionsAsync_called = False
        self.slept = False

    def isConnected(self):
        return True

    def reqPositions(self):  # BLOCKING variant — forbidden by the fix
        self.blocking_reqPositions_called = True

    async def reqPositionsAsync(self):
        self.reqPositionsAsync_called = True
        if self._delay:
            await asyncio.sleep(self._delay)
        return []

    def sleep(self, _secs):  # ib.sleep — forbidden as the settle mechanism
        self.slept = True

    def positions(self):
        return list(self._positions)

    def run(self, awaitable):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(awaitable)
        finally:
            loop.close()


def _manager_with(fake_ib, *, settle_timeout=5):
    mgr = object.__new__(IBKRConnectionManager)
    mgr.ib = fake_ib
    mgr._POSITION_SETTLE_TIMEOUT = settle_timeout
    mgr.ensure_connected = lambda: None
    return mgr


# ---------------------------------------------------------------------------
# 1. The settled-read primitive
# ---------------------------------------------------------------------------

class TestSettledReadPrimitive:
    def test_interface_default_raises_not_implemented(self):
        # A bare adapter must say so loudly, never fabricate a flat.
        class _Bare(ExecutionClient):
            def connect(self): ...
            def disconnect(self): ...
            def is_connected(self): return True
            def register_order_status_callback(self, cb): ...
            def get_position(self, symbol): return 0
            def get_account_summary(self, symbol): return {}
            def place_bracket_order(self, symbol, action, quantity, **k): return []
            def place_child_orders(self, symbol, parent_order_id, action,
                                   quantity, tp_price, sl_price): return []
            def modify_order(self, order_id, event=None): ...
            def cancel_open_orders(self, symbol): return 0
            def close_position(self, symbol, exit_mode, current_price): ...
            def register_error_callback(self, cb): ...

        with pytest.raises(NotImplementedError):
            _Bare().get_position_settled("CL")

    def test_sim_returns_true_position(self):
        sim = SimulatedExecution()
        sim._position = -1
        assert sim.get_position_settled("CL") == -1
        sim._position = 3
        assert sim.get_position_settled("CL") == 3

    def test_manager_uses_async_request_and_matches_symbol(self):
        fake = _FakeIB([_pos("CL", "CL", -1), _pos("ES", "ES", 2)])
        mgr = _manager_with(fake)
        assert mgr.get_position_settled(symbol="CL") == -1
        # It settled via the ASYNC request, not the blocking one, not ib.sleep.
        assert fake.reqPositionsAsync_called is True
        assert fake.blocking_reqPositions_called is False
        assert fake.slept is False

    def test_manager_flat_when_symbol_absent(self):
        fake = _FakeIB([_pos("ES", "ES", 2)])
        assert _manager_with(fake).get_position_settled(symbol="CL") == 0

    def test_manager_raises_on_settle_timeout(self):
        # Slow gateway: reqPositionsAsync outlasts the bound → TimeoutError,
        # the caller's fail-closed trigger (NOT an infinite hang, NOT a 0).
        fake = _FakeIB([_pos("CL", "CL", -1)], delay=0.2)
        mgr = _manager_with(fake, settle_timeout=0.05)
        with pytest.raises(asyncio.TimeoutError):
            mgr.get_position_settled(symbol="CL")


# ---------------------------------------------------------------------------
# Recovery / time-barrier / startup-sweep stubs
# ---------------------------------------------------------------------------

_LEDGER = {
    "trade_id": "trade_777", "side": "SHORT", "entry_price": 68.90,
    "quantity": 1, "tp_order_id": 201, "sl_order_id": 202,
    "tp_price": 67.90, "sl_price": 69.90, "atr_at_entry": 0.50,
    "entry_bar_time": "2026-07-06T20:00:00",
    "trailing_atr_mult": None, "max_hold_bars": None,
}


def _recovery_lt(*, first_read=0, settled):
    """LiveTrader stub exercising _recover_inherited_position's flat branch.

    ``settled`` is what the settled snapshot yields: an int, or an Exception
    instance/class to raise (unconfirmed read).
    """
    lt = object.__new__(lt_module.LiveTrader)
    lt._execution_symbol = "CL"
    lt.telemetry = MagicMock()
    lt.telemetry.get_open_position.return_value = dict(_LEDGER)
    lt.exec_client = MagicMock()
    lt.exec_client.get_position.return_value = first_read
    if isinstance(settled, (Exception,)) or (
        isinstance(settled, type) and issubclass(settled, Exception)
    ):
        lt.exec_client.get_position_settled.side_effect = settled
    else:
        lt.exec_client.get_position_settled.return_value = settled
    lt._telegram = MagicMock()
    lt.rolling_df_5m = None
    lt.rolling_df_1h = None
    lt._bar_size = "5m"
    # Observe which branch fires without running the heavy collaborators.
    lt._recover_oob_close = MagicMock(return_value=("SL_HIT_OOB", 69.90))
    lt._seed_restart_cooldown = MagicMock()
    lt._reconstruct_cooldown_from_ledger = MagicMock()
    lt._verify_and_heal_protective_legs = MagicMock(return_value="verified")
    lt._emit_health_event = MagicMock()
    return lt


class TestRecoveryFalseFlatGuard:
    def test_false_flat_does_not_cancel_or_close__restores(self):
        # RED→GREEN: first read 0 (stale cache) but settled shows the short.
        lt = _recovery_lt(first_read=0, settled=-1)
        lt._recover_inherited_position()
        lt._recover_oob_close.assert_not_called()      # no OOB close
        lt._seed_restart_cooldown.assert_not_called()  # no stop cooldown seed
        lt._verify_and_heal_protective_legs.assert_called_once()  # restored
        assert lt._active_trade_id == "trade_777"

    def test_confirmed_flat_still_resolves_oob(self):
        # True out-of-band close: both reads agree flat.
        lt = _recovery_lt(first_read=0, settled=0)
        lt._recover_inherited_position()
        lt._recover_oob_close.assert_called_once()
        lt._seed_restart_cooldown.assert_called_once()
        lt._verify_and_heal_protective_legs.assert_not_called()

    def test_unconfirmed_read_fails_closed__retains_protection(self):
        # Settled snapshot times out → NEVER cancel/mark OOB; retain + LOUD.
        lt = _recovery_lt(first_read=0, settled=asyncio.TimeoutError)
        lt._recover_inherited_position()
        lt._recover_oob_close.assert_not_called()
        lt._verify_and_heal_protective_legs.assert_called_once()  # retained
        lt._emit_health_event.assert_called_once()
        assert lt._emit_health_event.call_args.args[0] == "position-flat-unconfirmed"
        lt._telegram.send.assert_called_once()


def _time_barrier_lt(*, first_read, settled, active_trade="trade_777"):
    lt = object.__new__(lt_module.LiveTrader)
    lt._execution_symbol = "CL"
    lt.exec_client = MagicMock()
    lt.exec_client.get_position.return_value = first_read
    if isinstance(settled, type) and issubclass(settled, Exception):
        lt.exec_client.get_position_settled.side_effect = settled
    else:
        lt.exec_client.get_position_settled.return_value = settled
    lt._telegram = MagicMock()
    lt._emit_health_event = MagicMock()
    lt._active_trade_id = active_trade
    lt._position_bars_held = 3
    lt._position_entry_bar_time = None
    lt.telemetry = MagicMock()
    lt._reset_position_state = MagicMock()
    # TIME BARRIER submit-and-defer state (settle-confirm-event-loop_07202026_0713):
    # the re-entrancy guard at the top of _check_time_barrier reads this. No pending
    # exit here — these guard the flat-read branch, which _check_time_barrier defers
    # to the idle-loop reconciler.
    lt._pending_exit_order_id = None
    return lt


class TestTimeBarrierFalseFlatGuard:
    def test_false_flat_does_not_book_oob_close(self):
        import pandas as pd
        lt = _time_barrier_lt(first_read=0, settled=-1)
        # In-callback: a flat cache read for a tracked trade DEFERS — no inline
        # settled confirm, no book, no cancel, no reset. (settle-confirm-event-loop
        # _07202026_0713 submit-and-defer.)
        out = lt._check_time_barrier(
            bar_time=pd.Timestamp("2026-07-08 14:00:00"),
            current_price=68.5, atr_value=0.4,
        )
        assert out is False
        lt.telemetry.close_position.assert_not_called()
        lt.exec_client.cancel_open_orders.assert_not_called()
        lt._reset_position_state.assert_not_called()

        # PROTECTION RE-VERIFIED through the idle-loop reconciler (the $296k
        # naked-short guard): the settled read (settled=-1) confirms the position is
        # STILL OPEN (false flat) — the reconciler must STILL make NO close, NO
        # cancel, NO reset. Position + protective orders retained.
        lt._reconcile_pending_position_state()
        lt.telemetry.close_position.assert_not_called()
        lt.exec_client.cancel_open_orders.assert_not_called()
        lt._reset_position_state.assert_not_called()

    def test_unconfirmed_read_fails_closed(self):
        import pandas as pd
        lt = _time_barrier_lt(first_read=0, settled=asyncio.TimeoutError)
        # In-callback: flat cache read for a tracked trade DEFERS WITHOUT touching
        # the settled read (a settled read here re-enters the running loop). No book,
        # no cancel, and — the discriminator — no in-callback health emission.
        out = lt._check_time_barrier(
            bar_time=pd.Timestamp("2026-07-08 14:00:00"),
            current_price=68.5, atr_value=0.4,
        )
        assert out is False
        lt.telemetry.close_position.assert_not_called()
        lt.exec_client.cancel_open_orders.assert_not_called()
        lt._emit_health_event.assert_not_called()  # no in-callback settled read

        # The settled confirm — and its FAIL-CLOSED behaviour — moved BYTE-FOR-BYTE
        # to the idle-loop reconciler ($296k naked-short guard): the settled snapshot
        # times out (raises), the LOUD health event fires, and NOTHING is
        # closed/cancelled/reset (position + protective orders retained).
        lt._reconcile_pending_position_state()
        lt.telemetry.close_position.assert_not_called()
        lt.exec_client.cancel_open_orders.assert_not_called()
        lt._reset_position_state.assert_not_called()
        lt._emit_health_event.assert_called_once()
        assert lt._emit_health_event.call_args.args[0] == "position-flat-unconfirmed"

    def test_confirmed_flat_books_oob_close(self):
        import pandas as pd
        lt = _time_barrier_lt(first_read=0, settled=0)
        lt._utc_iso_now = MagicMock(return_value="2026-07-08T14:00:00")
        lt._build_event_id = MagicMock(return_value="evt-1")
        lt._base_tradebook_fields = MagicMock(return_value={})
        lt.exec_client.cancel_open_orders.return_value = 0
        # In-callback: DEFER — never book/reset off an unconfirmed flat in the bar
        # callback.
        out = lt._check_time_barrier(
            bar_time=pd.Timestamp("2026-07-08 14:00:00"),
            current_price=68.5, atr_value=0.4,
        )
        assert out is False
        lt.telemetry.close_position.assert_not_called()
        lt._reset_position_state.assert_not_called()

        # The idle-loop reconciler's flat-read branch confirms settled==0 (a REAL
        # out-of-band close) and books it — BYTE-FOR-BYTE the relocated :1668 block.
        lt._reconcile_pending_position_state()
        lt.telemetry.close_position.assert_called_once()
        assert lt._reset_position_state.call_args.kwargs.get("reason") == "CLOSED_OOB"


def _sweep_lt(*, first_read, settled, orphan=True):
    lt = object.__new__(lt_module.LiveTrader)
    lt._execution_symbol = "CL"
    lt._active_trade_id = None
    lt._pending_entry_order_id = None
    lt.exec_client = MagicMock()
    lt.exec_client.get_position.return_value = first_read
    if isinstance(settled, type) and issubclass(settled, Exception):
        lt.exec_client.get_position_settled.side_effect = settled
    else:
        lt.exec_client.get_position_settled.return_value = settled
    lt.exec_client.cancel_open_orders.return_value = 1
    lt._emit_health_event = MagicMock()
    evt = SimpleNamespace(symbol="CL")
    lt._open_orders = {"CL:1": evt} if orphan else {}
    return lt


class TestStartupSweepFalseFlatGuard:
    def test_false_flat_does_not_cancel_orphans(self):
        lt = _sweep_lt(first_read=0, settled=-1)  # really still short
        lt._cancel_orphaned_orders_on_startup()
        lt.exec_client.cancel_open_orders.assert_not_called()

    def test_unconfirmed_read_does_not_cancel(self):
        lt = _sweep_lt(first_read=0, settled=asyncio.TimeoutError)
        lt._cancel_orphaned_orders_on_startup()
        lt.exec_client.cancel_open_orders.assert_not_called()

    def test_confirmed_flat_cancels_genuine_orphans(self):
        lt = _sweep_lt(first_read=0, settled=0, orphan=True)
        lt._cancel_orphaned_orders_on_startup()
        lt.exec_client.cancel_open_orders.assert_called_once()
