"""
TDD-TESTER AUTHORIZATION
Target Implementation File: src/live_execution/live_trader.py
Target Class/Function: LiveTrader._check_trailing_stop, LiveTrader._check_entry_order_ttl
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)

Ticket: trailing-stop-log-type-error_07022026_2225

Three log.info / log.warning calls in live_trader.py use ``%d`` for IBKR
order IDs that arrive as *strings*.  This causes:

    TypeError: %d format: a real number is required, not str

These tests exercise each call site with string order IDs.
They FAIL on the buggy code (``%d``) and PASS after the fix (``%s``).
"""
import logging
import pandas as pd
import pytest
from unittest.mock import MagicMock, patch

from src.live_execution.live_trader import LiveTrader
from src.live_execution.interfaces.execution_interface import StandardExecutionEvent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_trailing_stop_trader(
    *,
    sl_order_id: str,
    position_side: int = 1,
    entry_price: float = 70.0,
    atr_at_entry: float = 0.50,
    highest_high: float = 80.0,
    lowest_low: float = 60.0,
) -> LiveTrader:
    """Build a minimal LiveTrader wired so _check_trailing_stop reaches the
    log call sites.  All external I/O is mocked."""
    trader = LiveTrader.__new__(LiveTrader)
    trader._execution_symbol = "CL"
    trader._sl_order_id = sl_order_id
    trader._position_side = position_side
    trader._entry_price = entry_price
    trader._atr_at_entry = atr_at_entry
    trader._trailing_activated = False
    # log-cosmetics-cancel-bounce_07222026_2330 (mechanical stub repair):
    # the no-op skip guard reads the tracked SL cache; None = no skip.
    trader._tracked_sl_price = None

    # Per-trade override is None → falls back to global
    trader._trade_trailing_atr_mult = None
    # Global trailing multiplier = 1.0  →  trigger when price moves ≥1×ATR
    trader._trailing_atr_mult = 1.0
    # Offset used to compute new SL
    trader._trailing_sl_atr_offset_long = 0.5
    trader._trailing_sl_atr_offset_short = 0.5
    # Mechanical stub repair (live-trailing-ladder-phase3_07232026_0035):
    # _check_trailing_stop now resolves per-side ladders; None = the legacy
    # single-rung path this suite pins.
    trader._trailing_ladder_long = None
    trader._trailing_ladder_short = None

    # Bar extremes — set so the trigger condition is satisfied immediately
    trader._highest_high = highest_high
    trader._lowest_low = lowest_low

    # rolling_df_5m — one-row DataFrame so iloc[-1] works
    trader.rolling_df_5m = pd.DataFrame(
        [{"Open": 75.0, "High": highest_high, "Low": lowest_low, "Close": 75.0}]
    )

    # Open orders dict — may or may not contain the SL order
    trader._open_orders = {}

    # Mocked collaborators
    trader.exec_client = MagicMock()
    trader.telemetry = MagicMock()
    trader._utc_iso_now = MagicMock(return_value="2026-07-02T22:00:00Z")
    trader._active_trade_id = "trade_test"
    trader._position_bars_held = 10

    return trader


# ---------------------------------------------------------------------------
# Test 1 — Site 2: TRAILING STOP modify log with string order_id
# ---------------------------------------------------------------------------

def test_trailing_stop_modify_log_accepts_string_order_id(caplog):
    """
    Site 2 (line ~1096):
        log.info("TRAILING STOP: modified SL order %d: %.2f → %.2f",
                 order_id, old_sl, new_sl)

    `order_id` comes from `evt.order_id` which is a string.
    With %d this raises TypeError; with %s it logs cleanly.
    """
    sl_id = "21"
    trader = _make_trailing_stop_trader(sl_order_id=sl_id)

    # Place a matching SL event in _open_orders so the loop finds it
    raw_order = MagicMock()
    raw_order.auxPrice = 69.00
    sl_evt = StandardExecutionEvent(
        order_id=sl_id,
        symbol="CL",
        status="PreSubmitted",
        filled_qty=0,
        remaining_qty=1,
        avg_price=0.0,
        raw_event=MagicMock(order=raw_order),
    )
    trader._open_orders[sl_id] = sl_evt

    with caplog.at_level(logging.INFO):
        trader._check_trailing_stop()          # <── should NOT raise TypeError

    # Verify the modify-log was emitted with the string order ID
    modify_msgs = [
        r.message for r in caplog.records
        if "modified SL order" in r.message
    ]
    assert len(modify_msgs) == 1, (
        f"Expected exactly 1 'modified SL order' log, got {len(modify_msgs)}"
    )
    assert sl_id in modify_msgs[0], (
        f"Log message should contain the order ID '{sl_id}': {modify_msgs[0]}"
    )


# ---------------------------------------------------------------------------
# Test 2 — Site 3: TRAILING STOP "not found" warning with string _sl_order_id
# ---------------------------------------------------------------------------

def test_trailing_stop_not_found_log_accepts_string_order_id(caplog):
    """
    Site 3 (line ~1133):
        log.warning("TRAILING STOP: triggered but SL order %d not found …",
                    self._sl_order_id)

    _sl_order_id is a string.  With %d this raises TypeError.
    Trigger: trailing conditions met but no matching order in _open_orders.
    """
    sl_id = "999"
    trader = _make_trailing_stop_trader(sl_order_id=sl_id)

    # _open_orders is empty → no match → hits the warning log
    trader._open_orders = {}

    with caplog.at_level(logging.WARNING):
        trader._check_trailing_stop()          # <── should NOT raise TypeError

    warning_msgs = [
        r.message for r in caplog.records
        if "not found" in r.message and "TRAILING STOP" in r.message
    ]
    assert len(warning_msgs) == 1, (
        f"Expected exactly 1 'not found' warning, got {len(warning_msgs)}"
    )
    assert sl_id in warning_msgs[0], (
        f"Warning should contain the order ID '{sl_id}': {warning_msgs[0]}"
    )


# ---------------------------------------------------------------------------
# Test 3 — Site 1: ENTRY TTL cancel log with string _pending_entry_order_id
# ---------------------------------------------------------------------------

def test_entry_ttl_cancel_log_accepts_string_order_id(caplog):
    """
    Site 1 (line ~949):
        log.info("ENTRY TTL: cancelling unfilled entry order %d …",
                 self._pending_entry_order_id, …)

    _pending_entry_order_id is a string like '42'.
    With %d this raises TypeError.
    """
    pending_id = "42"

    trader = LiveTrader.__new__(LiveTrader)
    trader._execution_symbol = "CL"
    trader._pending_entry_order_id = pending_id
    trader._pending_entry_bar_time = pd.Timestamp("2026-07-02 00:00:00", tz="UTC")

    # Simulate the IBKR order still being open (status=Submitted)
    entry_evt = StandardExecutionEvent(
        order_id=pending_id,
        symbol="CL",
        status="Submitted",
        filled_qty=0,
        remaining_qty=1,
        avg_price=0.0,
        raw_event=None,
    )
    trader._open_orders = {pending_id: entry_evt}

    trader.exec_client = MagicMock()
    trader.exec_client.cancel_open_orders.return_value = 1
    trader._reset_position_state = MagicMock()

    # bar_time is later than _pending_entry_bar_time → TTL expired
    bar_time = pd.Timestamp("2026-07-02 00:05:00", tz="UTC")

    with caplog.at_level(logging.INFO):
        trader._check_entry_order_ttl(bar_time)  # <── should NOT raise TypeError

    ttl_msgs = [
        r.message for r in caplog.records
        if "ENTRY TTL" in r.message and "cancelling" in r.message
    ]
    assert len(ttl_msgs) == 1, (
        f"Expected exactly 1 'ENTRY TTL: cancelling' log, got {len(ttl_msgs)}"
    )
    assert pending_id in ttl_msgs[0], (
        f"Log should contain the order ID '{pending_id}': {ttl_msgs[0]}"
    )
    # After TTL cancel, pending state should be cleared
    assert trader._pending_entry_order_id is None
