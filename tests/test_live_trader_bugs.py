import logging
import pandas as pd
import pytest
from unittest.mock import MagicMock

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


def test_pnl_log_format(caplog):
    """
    Test that the LiveTrader logs the requested TP/SL format:
    TIMESTAMP [INFO] Symbol: CL | Position: X | Price: Y | TP: Z | SL: W
    """
    trader = LiveTrader.__new__(LiveTrader)
    trader.exec_client = MagicMock()
    trader.exec_client.get_account_summary.return_value = {"cl_unrealized_pnl": 0.0, "cl_avg_cost": 70000.0}
    trader.strategy = MagicMock()
    trader.strategy.evaluate.return_value = MagicMock(action="HOLD", buy_prob=0.0, sell_prob=0.0)
    trader.telemetry = MagicMock()
    
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
    
    features = pd.DataFrame([{"Open": 70.0, "High": 70.0, "Low": 70.0, "Close": 70.0, "Volume": 100}])
    
    with caplog.at_level(logging.INFO):
        trader._on_new_bar(
            bar_time=pd.Timestamp("2026-06-30 02:00:20", tz="UTC"),
            rolling_df=features,
            stream="5m"
        )
        
    log_messages = [rec.message for rec in caplog.records]
    
    found = any("Symbol: CL | Position:" in msg for msg in log_messages)
    assert found, "Did not find the requested TP/SL log format."
