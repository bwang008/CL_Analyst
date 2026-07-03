"""
TDD-TESTER AUTHORIZATION
Target Implementation File: src/live_execution/live_trader.py
Target Class/Function: LiveTrader._on_bar_update_5m, LiveTrader._on_new_bar
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)
"""
import threading
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, call

import pandas as pd
import pytest

from src.live_execution.live_trader import LiveTrader


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_5m_trader():
    """Build a minimal LiveTrader wired for exercising _on_bar_update_5m.

    The strategy resolution is "1h" so the _on_new_bar branch does NOT fire.
    All I/O boundaries are mocked to isolate the scheduling logic.
    """
    trader = LiveTrader.__new__(LiveTrader)
    trader._execution_symbol = "CL"
    trader._bar_size = "1h"  # Strategy uses hourly bars — NOT 5m
    trader._last_bar_time_5m = None
    trader._ledger_lock = threading.Lock()
    trader._last_5m_bar_log = ""

    # Rolling 5m DataFrame — seed with one historical bar so iloc[-1] works
    seed = pd.DataFrame(
        [{
            "DateTime": pd.Timestamp("2026-06-30 01:50:00"),
            "Open": 70.0, "High": 70.5, "Low": 69.5,
            "Close": 70.2, "Volume": 100.0,
        }]
    )
    seed = seed.set_index(
        pd.DatetimeIndex(seed["DateTime"]), drop=False
    )
    seed.index.name = "DateTime"
    trader.rolling_df_5m = seed

    # Mocked collaborators
    trader.data_manager_5m = MagicMock()
    trader.telemetry = MagicMock()
    trader._check_trailing_stop = MagicMock()

    return trader


def _make_bars_stub(bar_time_str: str = "2026-06-30 02:00:00"):
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
    in_progress.date = pd.Timestamp("2026-06-30 02:05:00")
    in_progress.open = 70.30
    in_progress.high = 70.30
    in_progress.low = 70.30
    in_progress.close = 70.30
    in_progress.volume = 0

    return [completed, in_progress]


# ---------------------------------------------------------------------------
# Test 1 — trailing stop fires on every 5m bar (even when resolution is 1h)
# ---------------------------------------------------------------------------

def test_trailing_stop_called_on_every_5m_bar():
    """When _bar_size == '1h', _on_bar_update_5m must still call
    _check_trailing_stop().  This proves the trailing stop is decoupled
    from the inference cycle.

    RED BEFORE FIX: The current code only calls _check_trailing_stop()
    inside _on_new_bar(), which is gated behind `if self._bar_size == "5m"`.
    With _bar_size == "1h", neither _on_new_bar() nor _check_trailing_stop()
    is reached from _on_bar_update_5m().
    """
    trader = _make_5m_trader()
    bars = _make_bars_stub()

    trader._on_bar_update_5m(bars, has_new_bar=True)

    trader._check_trailing_stop.assert_called_once()


# ---------------------------------------------------------------------------
# Test 2 — trailing stop is NOT called inside _on_new_bar
# ---------------------------------------------------------------------------

@patch("src.live_execution.live_trader.build_live_features")
def test_trailing_stop_not_called_in_on_new_bar(mock_build_live_features):
    """After the fix, _on_new_bar must NOT invoke _check_trailing_stop().

    RED BEFORE FIX: Line 2952 explicitly calls self._check_trailing_stop()
    inside the in-position block of _on_new_bar().
    """
    trader = LiveTrader.__new__(LiveTrader)
    trader._execution_symbol = "CL"
    trader._bar_size = "5m"
    trader._virtual_ledger = {"5m": 1, "1h": 0}
    trader._last_virtual_ledger_log = ""
    trader._data_mute = False
    trader._max_position_size = 1
    trader._position_bars_held = 5
    trader._position_side = 1
    trader._front_month_last_close = 70.0
    trader._atr_period_long = 14
    trader._atr_period_short = 14
    trader._atr_period = 14
    trader._rollover_in_progress = False
    trader._emergency_halt = False
    trader._pending_entry_order_id = None
    trader._needs_macro = False
    trader._lean_features = False
    trader._front_month_str = "202607"
    trader.feature_names = ["Open", "High", "Low", "Close", "Volume"]

    trader.exec_client = MagicMock()
    trader.exec_client.get_position.return_value = 1
    trader.exec_client.get_account_summary.return_value = {
        "cl_unrealized_pnl": 50.0,
        "cl_avg_cost": 70.0,
    }
    trader.telemetry = MagicMock()
    trader.data_manager_1h = MagicMock()
    trader.strategy = MagicMock()
    trader.strategy.evaluate.return_value = MagicMock(
        action="HOLD", buy_prob=0.0, sell_prob=0.0,
    )

    trader._open_orders = {}
    trader._tp_order_ids = [999]
    trader._sl_order_id = 888

    raw_tp = MagicMock()
    raw_tp.lmtPrice = 71.00
    tp_evt = MagicMock(order_id="999", symbol="CL", status="PreSubmitted")
    tp_evt.raw_event.order = raw_tp

    raw_sl = MagicMock()
    raw_sl.auxPrice = 69.50
    sl_evt = MagicMock(order_id="888", symbol="CL", status="PreSubmitted")
    sl_evt.raw_event.order = raw_sl

    trader._open_orders["999"] = tp_evt
    trader._open_orders["888"] = sl_evt

    trader._check_trailing_stop = MagicMock()
    trader._check_time_barrier = MagicMock(return_value=False)

    features = pd.DataFrame(
        [{"Open": 70.0, "High": 70.0, "Low": 70.0, "Close": 70.0, "Volume": 100}] * 15
    )
    mock_build_live_features.return_value = features

    trader._on_new_bar(
        bar_time=pd.Timestamp("2026-06-30 02:00:00", tz="UTC"),
        rolling_df=features,
        stream="5m",
    )

    # After the fix, _check_trailing_stop must NOT be called from _on_new_bar
    trader._check_trailing_stop.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3 — trailing stop runs under _ledger_lock
# ---------------------------------------------------------------------------

def test_trailing_stop_called_under_ledger_lock():
    """_check_trailing_stop must execute inside `with self._ledger_lock:`.

    This test replaces _ledger_lock with a mock that records __enter__/__exit__
    calls, and _check_trailing_stop with a spy that checks the lock is held
    at the moment of invocation.

    RED BEFORE FIX: The current code never calls _check_trailing_stop()
    from _on_bar_update_5m() at all, so the lock ordering is untested.
    """
    trader = _make_5m_trader()
    bars = _make_bars_stub()

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

    trader._on_bar_update_5m(bars, has_new_bar=True)

    assert len(was_locked_during_call) == 1, (
        "_check_trailing_stop was not called from _on_bar_update_5m"
    )
    assert was_locked_during_call[0] is True, (
        "_check_trailing_stop was called but the _ledger_lock was NOT held"
    )
