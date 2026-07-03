"""
TDD-TESTER AUTHORIZATION
Target Implementation File: src/live_execution/live_trader.py
Target Class/Function: LiveTrader._on_new_bar
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)
"""
import logging
import pandas as pd
import pytest
from unittest.mock import MagicMock, patch

from src.live_execution.live_trader import LiveTrader
from src.live_execution.interfaces.execution_interface import StandardExecutionEvent

def test_check_entry_order_ttl_type_mismatch():
    """
    Test that _check_entry_order_ttl correctly matches string order IDs from IBKR
    against integer pending entry order IDs.
    """
    # Create a mock LiveTrader
    trader = LiveTrader.__new__(LiveTrader)
    trader._open_orders = {}
    
    # Simulate an integer pending entry order ID
    pending_id_int = 12345
    trader._pending_entry_order_id = pending_id_int
    
    # Set the pending bar time to the past
    trader._pending_entry_bar_time = pd.Timestamp("2026-06-30 00:00:00", tz="UTC")
    trader._execution_symbol = "CL"
    current_time = pd.Timestamp("2026-06-30 00:05:00", tz="UTC")
    
    # Simulate IBKR sending a string order_id in open_orders
    evt = StandardExecutionEvent(
        order_id=str(pending_id_int),
        symbol="CL",
        status="Submitted",
        filled_qty=0,
        remaining_qty=1,
        avg_price=0.0,
        raw_event=None,
    )
    trader._open_orders[str(pending_id_int)] = evt
    
    trader.exec_client = MagicMock()
    
    trader._check_entry_order_ttl(current_time)
    
    trader.exec_client.cancel_open_orders.assert_called_once_with(symbol="CL")
    assert trader._pending_entry_order_id is None


@patch('src.live_execution.live_trader.build_live_features')
def test_pnl_log_format(mock_build_live_features, caplog):
    """
    Test that the LiveTrader logs the requested TP/SL format:
    TIMESTAMP [INFO] Symbol: CL | Position: X | Price: Y | TP: Z | SL: W
    """
    trader = LiveTrader.__new__(LiveTrader)
    trader.exec_client = MagicMock()
    trader.exec_client.get_account_summary.return_value = {"cl_unrealized_pnl": 0.0, "cl_avg_cost": 70000.0}
    trader.exec_client.get_position.return_value = 1
    trader.strategy = MagicMock()
    trader.strategy.evaluate.return_value = MagicMock(action="HOLD", buy_prob=0.0, sell_prob=0.0)
    trader.telemetry = MagicMock()
    trader.data_manager_1h = MagicMock()  # Mock this to prevent AttributeError
    
    trader._open_orders = {}
    trader._tp_order_ids = [999]
    trader._sl_order_id = 888
    
    raw_tp = MagicMock()
    raw_tp.lmtPrice = 71.00
    tp_evt = StandardExecutionEvent(order_id="999", symbol="CL", status="PreSubmitted", filled_qty=0, remaining_qty=1, avg_price=0.0, raw_event=MagicMock(order=raw_tp))

    raw_sl = MagicMock()
    raw_sl.auxPrice = 69.50
    sl_evt = StandardExecutionEvent(order_id="888", symbol="CL", status="PreSubmitted", filled_qty=0, remaining_qty=1, avg_price=0.0, raw_event=MagicMock(order=raw_sl))
    
    trader._open_orders["999"] = tp_evt
    trader._open_orders["888"] = sl_evt
    
    trader._max_position_size = 1
    trader._position_bars_held = 10
    trader._data_mute = False
    trader._virtual_ledger = {"5m": 1, "1h": 0}
    trader._last_virtual_ledger_log = ""
    trader._position_side = 1
    trader._execution_symbol = "CL"
    trader._emergency_halt = False
    trader._check_trailing_stop = MagicMock()
    trader._front_month_last_close = 70.0
    trader._atr_period_long = 14
    trader._atr_period_short = 14
    trader._atr_period = 14
    trader._rollover_in_progress = False
    trader._check_time_barrier = MagicMock(return_value=False)
    trader._pending_entry_order_id = None
    trader._needs_macro = False
    trader.feature_names = ["Open", "High", "Low", "Close", "Volume"]
    trader._lean_features = False
    trader._front_month_str = "202607"
    
    features = pd.DataFrame([{"Open": 70.0, "High": 70.0, "Low": 70.0, "Close": 70.0, "Volume": 100}])
    mock_build_live_features.return_value = features
    
    with caplog.at_level(logging.INFO):
        trader._on_new_bar(
            bar_time=pd.Timestamp("2026-06-30 02:00:20", tz="UTC"),
            rolling_df=features,
            stream="5m"
        )
        
    log_messages = [rec.message for rec in caplog.records]
    
    found = any("Symbol: CL | Position:" in msg for msg in log_messages)
    assert found, "Did not find the requested TP/SL log format."


def test_out_of_band_exit_routing(caplog):
    """
    Test that an automated time-barrier exit is correctly tracked as a close order
    and does not trigger the 'ENTRY FILLED' logic or the %d format crash.
    """
    trader = LiveTrader.__new__(LiveTrader)
    trader._execution_symbol = "CL"
    trader._position_entry_bar_time = pd.Timestamp("2026-06-30 00:00:00", tz="UTC")
    trader._position_bars_held = 25
    trader._max_hold_bars = 25
    trader._trade_max_hold_bars = None
    trader._exit_mode = "MARKETABLE_LIMIT"
    trader._active_trade_id = "trade_123"
    trader._pending_close_order_ids = set()
    trader._open_orders = {}
    trader._last_decision_context_by_order_id = {}
    trader._processed_exit_order_ids = set()
    trader._pending_entry_order_id = None
    trader._front_month_str = "202607"
    
    # Track the system generated bracket IDs
    trader.telemetry = MagicMock()
    trader.telemetry.close_position = MagicMock()
    
    trader.exec_client = MagicMock()
    trader.exec_client.cancel_open_orders.return_value = 2
    
    mock_trade = MagicMock()
    mock_trade.order.orderId = 10
    trader.exec_client.close_position.return_value = mock_trade
    
    # 1. Trigger the time barrier
    result = trader._check_time_barrier(
        bar_time=pd.Timestamp("2026-06-30 02:00:00", tz="UTC"),
        current_price=70.50,
        atr_value=0.5
    )
    assert result is True  # _check_time_barrier returns True if it exited
    assert "10" in trader._processed_exit_order_ids
    
    # 2. Trigger the _on_standard_execution_event callback for the fill of order 10
    evt = StandardExecutionEvent(
        order_id="10",
        symbol="CL",
        status="Filled",
        filled_qty=1,
        remaining_qty=0,
        avg_price=70.50,
        raw_event=MagicMock()
    )
    evt.raw_event.order.action = "SELL"
    
    trader._utc_iso_now = MagicMock(return_value="2026-06-30T02:00:10Z")
    trader._build_event_id = MagicMock(return_value="evt_123")
    trader._reset_position_state = MagicMock()
    trader._place_bracket_children_on_fill = MagicMock()
    
    with caplog.at_level(logging.INFO):
        trader._on_standard_execution_event(evt)
        
    log_messages = [rec.message for rec in caplog.records]
    
    # Check that it did NOT log "ENTRY FILLED"
    entry_fill_logs = [msg for msg in log_messages if "ENTRY FILLED" in msg]
    assert len(entry_fill_logs) == 0, "It mistakenly logged an ENTRY FILLED for a close order!"
    
    
    # Check that it did NOT attempt to place bracket children
    trader._place_bracket_children_on_fill.assert_not_called()


@patch('src.live_execution.live_trader.build_live_features')
def test_unbound_local_error_on_zero_position(mock_build_live_features, caplog):
    """
    Test to reproduce the UnboundLocalError when _on_new_bar is called with current_position=0.
    """
    trader = LiveTrader.__new__(LiveTrader)
    trader._execution_symbol = "CL"
    trader._virtual_ledger = {"5m": 0, "1h": 0}
    trader._last_virtual_ledger_log = ""
    
    trader.strategy = MagicMock()
    # Ensure it returns a HOLD signal so it doesn't try to place orders
    trader.strategy.evaluate.return_value = MagicMock(
        action="HOLD", buy_prob=0.0, sell_prob=0.0, probability=0.0, confidence_pct=0.0, signal_label="Hold", skip_reason="None"
    )
    
    trader.telemetry = MagicMock()
    trader.exec_client = MagicMock()
    trader.exec_client.get_position.return_value = 0
    trader.exec_client.get_account_summary.return_value = {"cl_unrealized_pnl": 0.0, "cl_avg_cost": 0.0}
    
    trader._open_orders = {}
    trader._tp_order_ids = []
    trader._sl_order_id = None
    trader._tracked_tp_price = None
    trader._tracked_sl_price = None
    
    trader._data_mute = False
    trader._max_position_size = 1
    trader._position_bars_held = 0
    trader._front_month_last_close = 70.0
    trader._atr_period_long = 14
    trader._atr_period_short = 14
    trader._atr_period = 14
    trader._rollover_in_progress = False
    trader._check_time_barrier = MagicMock(return_value=False)
    trader._check_trailing_stop = MagicMock()
    trader._data_mute_since = 0
    trader._emergency_halt = False
    trader._rollover_in_progress = False
    trader._pending_entry_order_id = None
    trader._needs_macro = False
    trader.feature_names = []
    trader.data_manager_1h = MagicMock()
    trader._needs_macro = False
    trader.feature_names = ["Open", "High", "Low", "Close", "Volume"]
    trader._lean_features = False
    trader._front_month_str = "202607"
    
    # Create mock dataframe for rolling_df with enough rows for ATR calculation (length=14)
    features = pd.DataFrame([{"Open": 70.0, "High": 70.0, "Low": 70.0, "Close": 70.0, "Volume": 100}] * 15)
    mock_build_live_features.return_value = features

    
    # Execute the method which should raise UnboundLocalError
    with caplog.at_level(logging.INFO):
        trader._on_new_bar(
            bar_time=pd.Timestamp("2026-06-30 02:00:20", tz="UTC"),
            rolling_df=features,
            stream="5m"
        )


def test_place_bracket_children_on_fill():
    """
    Test that _place_bracket_children_on_fill correctly calculates child prices
    and places child orders without raising NameError for 'ctx'.
    """
    trader = LiveTrader.__new__(LiveTrader)
    trader._execution_symbol = "CL"
    trader.exec_client = MagicMock()
    
    # Mock child trades returned by place_child_orders
    tp_trade = MagicMock()
    tp_trade.order.orderId = 124
    sl_trade = MagicMock()
    sl_trade.order.orderId = 125
    trader.exec_client.place_child_orders.return_value = [tp_trade, sl_trade]
    
    order_id = 123
    trader._last_decision_context_by_order_id = {
        order_id: {
            "tp_offset": 0.5,
            "sl_offset": 0.5,
            "entry_action": "BUY",
            "lots": 1,
            "tiered_tp_offsets": []
        }
    }
    
    # This should not raise an error, and should place orders at fill_price + 0.5 and fill_price - 0.5
    trader._place_bracket_children_on_fill(
        order_id=order_id,
        fill_price=70.0,
        action_str="BUY",
        qty=1.0,
        contract=MagicMock()
    )
    
    trader.exec_client.place_child_orders.assert_called_once_with(
        symbol="CL",
        parent_order_id=order_id,
        action="SELL",
        quantity=1,
        tp_price=70.5,
        sl_price=69.5
    )
    assert trader._tp_order_ids == [124]
    assert trader._sl_order_id == 125


def _make_oca_trader():
    """
    Build an isolated LiveTrader instance wired for exercising the OCA
    (One-Cancels-All) block inside _on_standard_execution_event.

    All external I/O boundaries (execution client, telemetry, telegram,
    event-id/time helpers, position-state reset, bracket placement) are
    mocked so only the OCA cancel-contract logic is under test.
    """
    trader = LiveTrader.__new__(LiveTrader)
    trader._execution_symbol = "CL"
    trader._front_month_str = "202607"
    trader._active_trade_id = "trade_abc"
    trader._position_bars_held = 5
    trader._position_side = 1
    trader._open_orders = {}
    trader._processed_exit_order_ids = set()
    trader._processed_entry_order_ids = set()
    trader._last_decision_context_by_order_id = {}

    # Bracket protective legs: ints (matches production contract)
    trader._tp_order_ids = [999]
    trader._sl_order_id = 888

    # Execution client — MagicMock auto-creates BOTH cancel_open_orders and
    # cancel_order attributes, so the buggy code (cancel_order) raises no error.
    # The RED signal is therefore that cancel_open_orders is NOT called.
    trader.exec_client = MagicMock()
    trader.exec_client.cancel_open_orders.return_value = 1

    # Mock every other side-effecting collaborator to isolate OCA logic.
    trader.telemetry = MagicMock()
    trader._telegram = MagicMock()
    trader._utc_iso_now = MagicMock(return_value="2026-06-30T02:00:10Z")
    trader._build_event_id = MagicMock(return_value="evt_oca")
    trader._reset_position_state = MagicMock()
    trader._place_bracket_children_on_fill = MagicMock()
    return trader


def _make_fill_event(order_id_str):
    """A Filled StandardExecutionEvent for the given (string) order id."""
    raw_order = MagicMock()
    raw_order.action = "SELL"
    raw_order.permId = None
    raw_order.parentId = None
    raw_order.account = None
    return StandardExecutionEvent(
        order_id=order_id_str,
        symbol="CL",
        status="Filled",
        filled_qty=1,
        remaining_qty=0,
        avg_price=70.50,
        raw_event=MagicMock(order=raw_order),
    )


def test_oca_tp_fill_uses_bulk_symbol_scoped_cancel(caplog):
    """
    RED-before-fix: When a TAKE-PROFIT leg fills, the OCA block must cancel the
    resting sibling protective order via the ONLY existing cancel contract:
    exec_client.cancel_open_orders(symbol=...) -> int.

    The current buggy implementation calls the nonexistent
    exec_client.cancel_order(str(self._sl_order_id)) instead, so
    cancel_open_orders is never invoked. This assertion is therefore RED
    against current code and GREEN after the Coder collapses the two
    per-order branches into one bulk symbol-scoped cancel.
    """
    trader = _make_oca_trader()

    # order_id "999" is in _tp_order_ids -> TP fill
    evt = _make_fill_event("999")

    with caplog.at_level(logging.INFO):
        trader._on_standard_execution_event(evt)

    # The required contract: single bulk, symbol-scoped cancel.
    trader.exec_client.cancel_open_orders.assert_called_once_with(symbol="CL")

    # Must NOT use the nonexistent per-order cancel API.
    assert not trader.exec_client.cancel_order.called, (
        "OCA must not call the nonexistent cancel_order(order_id) API; "
        "it must use cancel_open_orders(symbol=...)."
    )

    # Sanity: the OCA exit path still ran to completion.
    trader._reset_position_state.assert_called_once_with(reason="TP_HIT")
    assert "999" in trader._processed_exit_order_ids


def test_oca_sl_fill_uses_bulk_symbol_scoped_cancel(caplog):
    """
    RED-before-fix: When a STOP-LOSS leg fills, the OCA block must cancel the
    resting sibling TP order(s) via exec_client.cancel_open_orders(symbol=...)
    and must NOT call the nonexistent cancel_order(order_id) API.
    """
    trader = _make_oca_trader()

    # order_id "888" matches _sl_order_id -> SL fill
    evt = _make_fill_event("888")

    with caplog.at_level(logging.INFO):
        trader._on_standard_execution_event(evt)

    trader.exec_client.cancel_open_orders.assert_called_once_with(symbol="CL")

    assert not trader.exec_client.cancel_order.called, (
        "OCA must not call the nonexistent cancel_order(order_id) API; "
        "it must use cancel_open_orders(symbol=...)."
    )

    trader._reset_position_state.assert_called_once_with(reason="SL_HIT")
    assert "888" in trader._processed_exit_order_ids
