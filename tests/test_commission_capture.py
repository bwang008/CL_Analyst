"""
TDD-TESTER AUTHORIZATION
Target Implementation File: src/live_execution/interfaces/execution_interface.py
                            (R1: StandardCommissionEvent dataclass;
                             ExecutionClient.register_commission_callback
                             base no-op),
                            src/live_execution/adapters/ibkr_execution.py
                            (R1: commissionReportEvent bridge —
                             _on_commission_report translates + dispatches;
                             callback exceptions contained),
                            src/live_execution/adapters/simulated_execution.py
                            (R1: inherits/accepts register_commission_callback;
                             R3: get_account_summary includes cl_market_price),
                            src/live_execution/ibkr_client.py
                            (R3: get_account_summary includes cl_market_price),
                            src/live_execution/live_trader.py
                            (R2: _on_commission_event -> COMMISSION tradebook
                             row with deterministic event_id; entry fill calls
                             telemetry.update_fill; EXECUTION_FILL row carries
                             avg_fill_price; R3: _price_decimals helper)
Status: FINALIZED
Strict-Lock: TRUE (Implementation agents may NOT modify this file)

Ticket: telemetry-fill-commission_07062026_0640
Phase: RED — verified against fleet_telemetry.db 2026-07-06: every
trade_ledger.fill_price NULL, zero COMMISSION events, tradebook fills carry
only last_fill_price. GC trade_5 gross -$183.00 vs IBKR realized -$184.94
(invisible $1.94 commission).

Conventions: object.__new__ stubs with explicit seams; SimpleNamespace for
ib_insync raw objects; pandas 1.5.3 compatible.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.live_execution import ibkr_client as ibkr_module
from src.live_execution import live_trader as lt_module
from src.live_execution.adapters import ibkr_execution as ibx
from src.live_execution.adapters import simulated_execution as simx
from src.live_execution.interfaces import execution_interface as ei

_IBKR_UNSET_DOUBLE = 1.7976931348623157e+308  # IBKR sentinel for "no value"


# ===========================================================================
# R1 — StandardCommissionEvent + adapter bridge
# ===========================================================================

class TestCommissionBridge:

    def _make_adapter(self, callbacks):
        adapter = object.__new__(ibx.IBKRExecutionClient)
        adapter._commission_callbacks = []
        for cb in callbacks:
            ibx.IBKRExecutionClient.register_commission_callback(adapter, cb)
        return adapter

    @staticmethod
    def _raw_event(commission=0.97, realized=-184.94):
        trade = SimpleNamespace(
            order=SimpleNamespace(orderId=8),
            contract=SimpleNamespace(symbol="MGC", localSymbol="MGCQ6"),
        )
        fill = SimpleNamespace(
            execution=SimpleNamespace(execId="0000e1a7.686e1", orderId=8),
        )
        report = SimpleNamespace(
            commission=commission, realizedPNL=realized, currency="USD",
        )
        return trade, fill, report

    def test_standard_commission_event_fields(self):
        evt = ei.StandardCommissionEvent(
            order_id="8", exec_id="0000e1a7.686e1", symbol="MGC",
            commission=0.97, realized_pnl=-184.94, currency="USD",
        )
        assert evt.order_id == "8"
        assert evt.exec_id == "0000e1a7.686e1"
        assert evt.symbol == "MGC"
        assert evt.commission == pytest.approx(0.97)
        assert evt.realized_pnl == pytest.approx(-184.94)
        assert evt.currency == "USD"

    def test_adapter_translates_commission_report(self):
        received = []
        adapter = self._make_adapter([received.append])
        trade, fill, report = self._raw_event()
        ibx.IBKRExecutionClient._on_commission_report(
            adapter, trade, fill, report)
        assert len(received) == 1
        evt = received[0]
        assert evt.order_id == "8"
        assert evt.exec_id == "0000e1a7.686e1"
        assert evt.symbol == "MGC"
        assert evt.commission == pytest.approx(0.97)
        assert evt.realized_pnl == pytest.approx(-184.94)

    def test_adapter_maps_ibkr_unset_realized_to_none(self):
        """Entry fills carry IBKR's DBL_MAX sentinel — must become None,
        not a 1.8e308 stored in the DB."""
        received = []
        adapter = self._make_adapter([received.append])
        trade, fill, report = self._raw_event(realized=_IBKR_UNSET_DOUBLE)
        ibx.IBKRExecutionClient._on_commission_report(
            adapter, trade, fill, report)
        assert received[0].realized_pnl is None

    def test_adapter_contains_callback_exception(self):
        """A broken consumer must never propagate into ib_insync's loop."""
        def boom(evt):
            raise RuntimeError("consumer broke")
        received = []
        adapter = self._make_adapter([boom, received.append])
        trade, fill, report = self._raw_event()
        ibx.IBKRExecutionClient._on_commission_report(
            adapter, trade, fill, report)  # must not raise
        assert len(received) == 1  # later callbacks still dispatched

    def test_simulated_adapter_accepts_registration(self):
        sim = simx.SimulatedExecution()
        sim.register_commission_callback(lambda evt: None)  # must not raise


# ===========================================================================
# R2 — LiveTrader wiring
# ===========================================================================

class TestLiveTraderCommissionWiring:

    def test_on_commission_event_writes_tradebook_row(self):
        lt = object.__new__(lt_module.LiveTrader)
        lt.telemetry = MagicMock()
        lt._utc_iso_now = lambda: "2026-07-06T13:00:07+00:00"
        evt = ei.StandardCommissionEvent(
            order_id="8", exec_id="0000e1a7.686e1", symbol="MGC",
            commission=0.97, realized_pnl=-184.94, currency="USD",
        )
        lt_module.LiveTrader._on_commission_event(lt, evt)
        kw = lt.telemetry.log_tradebook_event.call_args.kwargs
        assert kw["event_type"] == "COMMISSION"
        assert kw["event_id"] == "COMMISSION_0000e1a7.686e1"
        assert kw["order_id"] == "8"
        assert kw["broker_execution_id"] == "0000e1a7.686e1"
        assert kw["symbol"] == "MGC"
        assert kw["commission"] == pytest.approx(0.97)
        assert kw["realized_pnl"] == pytest.approx(-184.94)

    def test_on_commission_event_never_raises(self):
        lt = object.__new__(lt_module.LiveTrader)
        lt.telemetry = MagicMock()
        lt.telemetry.log_tradebook_event.side_effect = RuntimeError("db locked")
        lt._utc_iso_now = lambda: "2026-07-06T13:00:07+00:00"
        evt = ei.StandardCommissionEvent(
            order_id="8", exec_id="x", symbol="MGC",
            commission=0.97, realized_pnl=None, currency="USD",
        )
        lt_module.LiveTrader._on_commission_event(lt, evt)  # must not raise

    def _make_fill_trader(self):
        lt = object.__new__(lt_module.LiveTrader)
        lt._open_orders = {}
        lt.telemetry = MagicMock()
        lt._utc_iso_now = lambda: "2026-07-06T13:00:07+00:00"
        lt._build_event_id = lambda **kw: "eid-1"
        lt._last_decision_context_by_order_id = {}
        lt._front_month_str = "202608"
        lt._processed_exit_order_ids = set()
        lt._processed_entry_order_ids = set()
        lt._tp_order_ids = []
        lt._sl_order_id = None
        lt._entry_order_ids = {"8"}
        lt._position_side = 1
        lt._atr_at_entry = 16.95
        lt._position_entry_bar_time = None
        lt._trade_trailing_atr_mult = None
        lt._trade_max_hold_bars = None
        lt._telegram = MagicMock()
        lt._place_bracket_children_on_fill = MagicMock()
        return lt

    @staticmethod
    def _entry_fill_event():
        raw_trade = SimpleNamespace(
            order=SimpleNamespace(action="BUY", permId=123, parentId=0,
                                  account="DU111"),
            contract=SimpleNamespace(symbol="MGC"),
        )
        return ei.StandardExecutionEvent(
            order_id="8", symbol="MGC", status="Filled",
            filled_qty=1, remaining_qty=0, avg_price=4150.1,
            raw_event=raw_trade,
        )

    def test_entry_fill_stamps_ledger_fill_price(self):
        lt = self._make_fill_trader()
        lt._on_standard_execution_event(self._entry_fill_event())
        lt.telemetry.update_fill.assert_called_once_with(8, 4150.1)

    def test_execution_fill_row_carries_avg_fill_price(self):
        lt = self._make_fill_trader()
        lt._on_standard_execution_event(self._entry_fill_event())
        kw = lt.telemetry.log_tradebook_event.call_args.kwargs
        assert kw["event_type"] == "EXECUTION_FILL"
        assert kw["avg_fill_price"] == pytest.approx(4150.1)
        assert kw["last_fill_price"] == pytest.approx(4150.1)


# ===========================================================================
# R3 — single-source [PNL] inputs
# ===========================================================================

class TestPnlDisplayInputs:

    @pytest.mark.parametrize("tick,expected", [
        (0.001, 3),   # NG
        (0.01, 2),    # CL
        (0.1, 1),     # MGC
        (0.25, 2),    # ES/MES
        (1, 0),
        (1.0, 0),
    ])
    def test_price_decimals(self, tick, expected):
        assert lt_module._price_decimals(tick) == expected

    def test_ibkr_account_summary_includes_market_price(self):
        mgr = object.__new__(ibkr_module.IBKRConnectionManager)
        mgr.ensure_connected = MagicMock()
        mgr.ib = MagicMock()
        mgr.ib.accountValues.return_value = []
        item = SimpleNamespace(
            contract=SimpleNamespace(symbol="NG"),
            position=-1,
            unrealizedPNL=81.59,
            realizedPNL=0.0,
            marketValue=-31866.0,
            averageCost=31947.53,
            marketPrice=3.1866,
        )
        mgr.ib.portfolio.return_value = [item]
        summary = ibkr_module.IBKRConnectionManager.get_account_summary(
            mgr, symbol="NG")
        assert summary["cl_market_price"] == pytest.approx(3.1866)
        # pre-existing keys unchanged
        assert summary["cl_unrealized_pnl"] == pytest.approx(81.59)
        assert summary["cl_avg_cost"] == pytest.approx(31947.53)

    def test_ibkr_account_summary_market_price_default_zero(self):
        mgr = object.__new__(ibkr_module.IBKRConnectionManager)
        mgr.ensure_connected = MagicMock()
        mgr.ib = MagicMock()
        mgr.ib.accountValues.return_value = []
        mgr.ib.portfolio.return_value = []
        summary = ibkr_module.IBKRConnectionManager.get_account_summary(
            mgr, symbol="NG")
        assert summary["cl_market_price"] == 0.0

    def test_simulated_account_summary_includes_market_price(self):
        sim = simx.SimulatedExecution()
        summary = sim.get_account_summary(symbol="CL")
        assert "cl_market_price" in summary
