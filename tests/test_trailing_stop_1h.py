"""
TDD-TESTER AUTHORIZATION
Target Implementation File: src/live_execution/live_trader.py
Target Class/Function: LiveTrader._on_bar_update_1h
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)
Ticket ID: livetest-trailing-stop_07042026_0205
"""
import threading
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from src.live_execution.live_trader import LiveTrader


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_1h_trader(bar_size: str = "1h"):
    """Build a minimal LiveTrader wired for exercising _on_bar_update_1h.

    All I/O boundaries are mocked to isolate the trailing stop check.
    The bar_size parameter controls which dispatch branch fires.
    """
    trader = LiveTrader.__new__(LiveTrader)
    trader._execution_symbol = "CL"
    trader._bar_size = bar_size
    trader._last_bar_time_1h = None
    trader._ledger_lock = threading.Lock()
    trader._last_1h_bar_log = ""

    # Rolling 1h DataFrame — seed with one historical bar
    seed = pd.DataFrame(
        [{
            "DateTime": pd.Timestamp("2026-06-30 01:00:00"),
            "Open": 70.0, "High": 70.5, "Low": 69.5,
            "Close": 70.2, "Volume": 100.0,
        }]
    )
    seed = seed.set_index(
        pd.DatetimeIndex(seed["DateTime"]), drop=False
    )
    seed.index.name = "DateTime"
    trader.rolling_df_1h = seed

    # Mocked collaborators
    trader.data_manager_1h = MagicMock()
    trader._check_trailing_stop = MagicMock()
    trader._on_new_bar = MagicMock()

    return trader


def _make_1h_bars_stub(bar_time_str: str = "2026-06-30 02:00:00"):
    """Return a two-element list that mimics the IBKR barUpdateEvent bars.

    bars[-2] is the completed bar; bars[-1] is the in-progress one.
    """
    completed = MagicMock()
    completed.date = pd.Timestamp(bar_time_str)
    completed.open = 70.10
    completed.high = 70.50
    completed.low = 69.90
    completed.close = 70.30
    completed.volume = 150

    in_progress = MagicMock()
    in_progress.date = pd.Timestamp("2026-06-30 03:00:00")
    in_progress.open = 70.30
    in_progress.high = 70.30
    in_progress.low = 70.30
    in_progress.close = 70.30
    in_progress.volume = 0

    return [completed, in_progress]


# ---------------------------------------------------------------------------
# Test 1 — trailing stop fires on every 1h bar (bar_size == "1h")
# ---------------------------------------------------------------------------

def test_trailing_stop_called_on_1h_bar_update():
    """When _bar_size == '1h', _on_bar_update_1h must call
    _check_trailing_stop() on every bar.

    RED BEFORE FIX: The current _on_bar_update_1h() does NOT call
    _check_trailing_stop() at all — only _on_bar_update_5m() does.
    """
    trader = _make_1h_trader(bar_size="1h")
    bars = _make_1h_bars_stub()

    trader._on_bar_update_1h(bars, has_new_bar=True)

    trader._check_trailing_stop.assert_called_once()


# ---------------------------------------------------------------------------
# Test 2 — trailing stop fires for 2h strategy on 1h bar
# ---------------------------------------------------------------------------

def test_trailing_stop_called_for_2h_strategy():
    """When _bar_size == '2h', _on_bar_update_1h must STILL call
    _check_trailing_stop() on every 1h bar — bar-size agnostic.

    RED BEFORE FIX: No trailing stop call in _on_bar_update_1h().
    """
    trader = _make_1h_trader(bar_size="2h")
    bars = _make_1h_bars_stub()

    trader._on_bar_update_1h(bars, has_new_bar=True)

    trader._check_trailing_stop.assert_called_once()


# ---------------------------------------------------------------------------
# Test 3 — trailing stop fires for 4h strategy on 1h bar
# ---------------------------------------------------------------------------

def test_trailing_stop_called_for_4h_strategy():
    """When _bar_size == '4h', _on_bar_update_1h must STILL call
    _check_trailing_stop() on every 1h bar — bar-size agnostic.

    RED BEFORE FIX: No trailing stop call in _on_bar_update_1h().
    """
    trader = _make_1h_trader(bar_size="4h")
    bars = _make_1h_bars_stub()

    trader._on_bar_update_1h(bars, has_new_bar=True)

    trader._check_trailing_stop.assert_called_once()


# ---------------------------------------------------------------------------
# Test 4 — trailing stop runs under _ledger_lock in 1h path
# ---------------------------------------------------------------------------

def test_trailing_stop_called_under_ledger_lock_1h():
    """_check_trailing_stop must execute inside `with self._ledger_lock:`
    when called from _on_bar_update_1h().

    Mirrors the same pattern tested for the 5m path in
    test_trailing_stop_5m_scheduling.py::test_trailing_stop_called_under_ledger_lock.

    RED BEFORE FIX: _on_bar_update_1h() never calls _check_trailing_stop(),
    so the lock ordering is untested for the 1h path.
    """
    trader = _make_1h_trader(bar_size="1h")
    bars = _make_1h_bars_stub()

    # Replace the real lock with a tracking wrapper
    real_lock = threading.Lock()
    lock_held = False

    class TrackingLock:
        """Context-manager wrapper that records lock held state."""
        def __enter__(self):
            nonlocal lock_held
            real_lock.acquire()
            lock_held = True
            return self

        def __exit__(self, *args):
            nonlocal lock_held
            lock_held = False
            real_lock.release()

    trader._ledger_lock = TrackingLock()

    # Spy on _check_trailing_stop to verify lock is held at call time
    was_locked_during_call = []

    def _trailing_spy():
        was_locked_during_call.append(lock_held)

    trader._check_trailing_stop = _trailing_spy

    trader._on_bar_update_1h(bars, has_new_bar=True)

    assert len(was_locked_during_call) == 1, (
        "_check_trailing_stop was not called from _on_bar_update_1h"
    )
    assert was_locked_during_call[0] is True, (
        "_check_trailing_stop was called but the _ledger_lock was NOT held"
    )


# ---------------------------------------------------------------------------
# Test 5 — trailing stop fires BEFORE _on_new_bar dispatch
# ---------------------------------------------------------------------------

def test_trailing_stop_fires_before_on_new_bar():
    """_check_trailing_stop() must be called BEFORE _on_new_bar() in the
    1h callback, mirroring the 5m path where trailing stop fires before
    the inference cycle.

    This ensures the trailing stop evaluation uses bar N's extremes
    before bar N+1's signal evaluation begins.

    RED BEFORE FIX: _on_bar_update_1h() never calls _check_trailing_stop().
    """
    trader = _make_1h_trader(bar_size="1h")
    bars = _make_1h_bars_stub()

    call_order = []

    original_trailing = trader._check_trailing_stop
    original_new_bar = trader._on_new_bar

    def mock_trailing():
        call_order.append("trailing_stop")

    def mock_new_bar(*args, **kwargs):
        call_order.append("on_new_bar")

    trader._check_trailing_stop = mock_trailing
    trader._on_new_bar = mock_new_bar

    trader._on_bar_update_1h(bars, has_new_bar=True)

    assert "trailing_stop" in call_order, (
        "_check_trailing_stop was never called from _on_bar_update_1h"
    )
    assert "on_new_bar" in call_order, (
        "_on_new_bar was never called from _on_bar_update_1h"
    )
    assert call_order.index("trailing_stop") < call_order.index("on_new_bar"), (
        f"_check_trailing_stop must fire BEFORE _on_new_bar, "
        f"but call order was: {call_order}"
    )
