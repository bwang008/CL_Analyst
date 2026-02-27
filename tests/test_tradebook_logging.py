from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.live_execution import live_trader as lt_module
from src.live_execution.telemetry import TelemetryDB


@pytest.fixture
def trader_with_db(tmp_path):
    db = TelemetryDB(str(tmp_path / "tradebook_test.db"))
    trader = object.__new__(lt_module.LiveTrader)
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
    order = SimpleNamespace(
        orderId=101,
        permId=5001,
        parentId=0,
        account="DU123",
        action="BUY",
        orderType="MKT",
        tif="GTC",
        totalQuantity=1,
        lmtPrice=0.0,
        auxPrice=0.0,
    )
    order_status = SimpleNamespace(status="Filled", filled=1, remaining=0, avgFillPrice=70.55)
    contract = SimpleNamespace(symbol="CL", localSymbol="CLH6", lastTradeDateOrContractMonth="202603")
    trade = SimpleNamespace(order=order, orderStatus=order_status, contract=contract)
    execution = SimpleNamespace(
        execId="E-101",
        time="2026-02-23T10:05:01.123000",
        acctNumber="DU123",
        side="BOT",
        price=70.55,
        shares=1,
        realizedPNL=0.0,
    )
    fill = SimpleNamespace(execution=execution)

    trader._on_exec_details(trade, fill)
    rows = db.read_tradebook(order_id=101, limit=10)
    assert len(rows) == 1
    row = rows[0]
    assert row["event_type"] == "EXECUTION_FILL"
    assert row["signal_id"] == "sig-101"
    assert row["decision_timestamp_utc"] == "2026-02-23T10:05:00.000000"
    assert row["contract_month"] == "202603"
    assert row["local_symbol"] == "CLH6"
    assert row["last_fill_price"] == 70.55


def test_commission_logs_after_fill(trader_with_db):
    trader, db = trader_with_db
    order = SimpleNamespace(
        orderId=101,
        permId=5001,
        parentId=0,
        account="DU123",
        action="BUY",
        orderType="MKT",
        tif="GTC",
        totalQuantity=1,
        lmtPrice=0.0,
        auxPrice=0.0,
    )
    contract = SimpleNamespace(symbol="CL", localSymbol="CLH6", lastTradeDateOrContractMonth="202603")
    trade = SimpleNamespace(order=order, contract=contract)
    execution = SimpleNamespace(execId="E-101", side="BOT")
    fill = SimpleNamespace(execution=execution)
    report = SimpleNamespace(acctNumber="DU123", commission=2.11, realizedPNL=10.0)

    trader._on_commission_report(trade, fill, report)
    rows = db.read_tradebook(order_id=101, limit=10)
    assert len(rows) == 1
    assert rows[0]["event_type"] == "COMMISSION"
    assert rows[0]["broker_execution_id"] == "E-101"
    assert rows[0]["commission"] == 2.11
