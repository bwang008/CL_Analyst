from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.live_execution import live_trader as lt_module
from src.live_execution.telemetry import TelemetryDB
from src.live_execution.interfaces.execution_interface import StandardExecutionEvent

@pytest.fixture
def trader_with_db(tmp_path):
    db = TelemetryDB(str(tmp_path / "tradebook_test.db"))
    trader = object.__new__(lt_module.LiveTrader)
    trader._open_orders = {}
    trader._processed_exit_order_ids = set()
    trader._position_side = 1
    trader.telemetry = db
    trader._last_decision_context_by_order_id = {
        101: {
            "signal_id": "sig-101",
            "decision_id": "dec-101",
            "decision_timestamp_utc": "2026-02-23T10:05:00.000000",
            "current_price": 70.50,
        }
    }
    trader._run_id = "run-1"
    trader._session_id = "session-1"
    trader._hostname = "host-a"
    trader._process_id = 1234
    trader._environment = "paper"
    trader._front_month_str = "202603"
    yield trader, db
    db.close()


def test_exec_details_logs_fill_event(trader_with_db):
    trader, db = trader_with_db
    
    event = StandardExecutionEvent(
        order_id=101,
        symbol="CLH6",
        status="Filled",
        filled_qty=1,
        remaining_qty=0,
        avg_price=70.55
    )

    trader._on_standard_execution_event(event)
    rows = db.read_tradebook(order_id=101, limit=10)
    assert len(rows) == 1
    row = rows[0]
    assert row["event_type"] == "EXECUTION_FILL"
    assert row["signal_id"] == "sig-101"
    assert row["decision_timestamp_utc"] == "2026-02-23T10:05:00.000000"
    assert row["contract_month"] == "202603"
    assert row["local_symbol"] == "CLH6"
    assert row["last_fill_price"] == 70.55
