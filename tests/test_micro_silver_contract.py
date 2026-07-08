"""Micro-silver (SIL) IBKR contract resolution + position disambiguation.

Root cause (2026-07-08): IBKR has NO future under symbol "SIL" (that ticker is
the Global X Silver Miners ETF stock), so Future(symbol="SIL", exchange="COMEX")
raised Error 200 and the live trader could never resolve a front month. COMEX
Micro Silver (1,000 oz) lists under IB symbol "SI" + tradingClass "SIL"
(localSymbols SILU6, SILZ6, …), sharing the "SI" symbol with full silver
(×5000, tradingClass "SI").

These tests pin:
  * registry: SIL -> ib_search_symbol "SI", ib_trading_class "SIL"; SI pins
    tradingClass "SI"; single-product symbols keep ib_trading_class None.
  * contract builders emit symbol="SI" + tradingClass for silver, and stay
    tradingClass="" (byte-identical) for everything else.
  * contract_matches / position reads separate full SI from micro SIL and never
    confuse the two (the silent "reports FLAT while holding" bug).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from ib_insync import ContFuture

from src.core.instrument_master import (
    contract_matches,
    get_instrument,
    registry_symbol_for_contract,
)
from src.live_execution.ibkr_client import (
    IBKRConnectionManager,
    build_future_contract,
)


# ===========================================================================
# Registry wiring
# ===========================================================================

class TestRegistrySilverFields:
    def test_sil_resolves_to_si_symbol_and_sil_class(self):
        inst = get_instrument("SIL")
        assert inst.symbol == "SIL"          # registry key / config symbol
        assert inst.ib_search_symbol == "SI"  # what IBKR is actually asked
        assert inst.ib_trading_class == "SIL"
        assert inst.multiplier == 1000
        assert inst.exchange == "COMEX"
        assert inst.micro_of == "SI"

    def test_si_pins_full_trading_class(self):
        inst = get_instrument("SI")
        assert inst.ib_search_symbol == "SI"
        assert inst.ib_trading_class == "SI"
        assert inst.multiplier == 5000

    @pytest.mark.parametrize("symbol", ["CL", "MCL", "ES", "MES", "GC", "MGC", "NG"])
    def test_single_product_symbols_pin_no_trading_class(self, symbol):
        """Non-silver symbols must keep ib_trading_class None so every contract
        build stays tradingClass="" — byte-identical to pre-change behavior."""
        inst = get_instrument(symbol)
        assert inst.ib_search_symbol == symbol
        assert inst.ib_trading_class is None


# ===========================================================================
# build_future_contract
# ===========================================================================

class TestBuildFutureContractSilver:
    def test_sil_continuous_uses_si_symbol_and_sil_class(self):
        c = build_future_contract("SIL", continuous=True)
        assert c.secType == "CONTFUT"
        assert c.symbol == "SI"
        assert c.tradingClass == "SIL"
        assert c.exchange == "COMEX"
        assert c.currency == "USD"
        assert c.includeExpired is True

    def test_sil_month_uses_si_symbol_and_sil_class(self):
        c = build_future_contract("SIL", continuous=False, contract_month="202609")
        assert c.secType == "FUT"
        assert c.symbol == "SI"
        assert c.tradingClass == "SIL"
        assert c.lastTradeDateOrContractMonth == "202609"
        assert c.exchange == "COMEX"

    def test_si_full_pins_si_trading_class(self):
        c = build_future_contract("SI", continuous=True)
        assert c.symbol == "SI"
        assert c.tradingClass == "SI"

    @pytest.mark.parametrize("symbol", ["CL", "ES", "GC", "NG"])
    def test_non_silver_trading_class_blank(self, symbol):
        """Regression guard: single-product builds emit tradingClass="" and are
        field-for-field identical to a plain ib_insync ContFuture."""
        c = build_future_contract(symbol, continuous=True)
        assert c.tradingClass == ""
        expected = ContFuture(
            symbol=symbol, exchange=c.exchange, currency="USD",
            includeExpired=True,
        )
        assert c == expected


# ===========================================================================
# get_front_month_contract — the search contract sent to reqContractDetails
# ===========================================================================

def _make_manager() -> IBKRConnectionManager:
    mgr = object.__new__(IBKRConnectionManager)
    mgr.ib = MagicMock()
    mgr.ensure_connected = MagicMock()
    return mgr


def _detail(local_symbol, month="20260928", trading_class="SIL"):
    return SimpleNamespace(
        contract=SimpleNamespace(
            lastTradeDateOrContractMonth=month,
            localSymbol=local_symbol,
            tradingClass=trading_class,
            conId=99,
        ),
        contractMonth=month[:6],
    )


class TestFrontMonthSearchContract:
    def test_sil_search_sends_si_symbol_and_sil_class(self):
        mgr = _make_manager()
        mgr.ib.reqContractDetails.return_value = [_detail("SILU6")]

        local_sym, month = mgr.get_front_month_contract(symbol="SIL")

        assert local_sym == "SILU6"
        assert month == "202609"
        search = mgr.ib.reqContractDetails.call_args[0][0]
        assert search.symbol == "SI"
        assert search.tradingClass == "SIL"
        assert search.exchange == "COMEX"
        assert search.currency == "USD"

    def test_si_search_sends_si_class(self):
        mgr = _make_manager()
        mgr.ib.reqContractDetails.return_value = [
            _detail("SIU6", trading_class="SI")
        ]
        mgr.get_front_month_contract(symbol="SI")
        search = mgr.ib.reqContractDetails.call_args[0][0]
        assert search.symbol == "SI"
        assert search.tradingClass == "SI"

    def test_cl_search_trading_class_blank(self):
        mgr = _make_manager()
        mgr.ib.reqContractDetails.return_value = [
            _detail("CLN6", trading_class="CL")
        ]
        mgr.get_front_month_contract(symbol="CL")
        search = mgr.ib.reqContractDetails.call_args[0][0]
        assert search.symbol == "CL"
        assert search.tradingClass == ""


# ===========================================================================
# contract_matches — the disambiguation primitive
# ===========================================================================

class TestContractMatches:
    def test_micro_matches_only_sil_class(self):
        assert contract_matches("SIL", "SI", "SIL") is True
        assert contract_matches("SIL", "SI", "SI") is False   # full != micro

    def test_full_matches_only_si_class(self):
        assert contract_matches("SI", "SI", "SI") is True
        assert contract_matches("SI", "SI", "SIL") is False   # micro != full

    def test_single_product_ignores_trading_class(self):
        # CL pins no tradingClass -> matches on symbol alone (any/blank class).
        assert contract_matches("CL", "CL", None) is True
        assert contract_matches("CL", "CL", "CL") is True
        assert contract_matches("CL", "CL", "") is True
        assert contract_matches("CL", "ES", None) is False

    def test_unknown_symbol_falls_back_to_symbol_compare(self):
        # Must never raise (get_cached_position "unheld reads flat" contract).
        assert contract_matches("ZZ", "ZZ", None) is True
        assert contract_matches("ZZ", "SI", None) is False


# ===========================================================================
# Position / portfolio reads separate full SI from micro SIL
# ===========================================================================

def _pos(symbol, trading_class, position):
    return SimpleNamespace(
        contract=SimpleNamespace(symbol=symbol, tradingClass=trading_class),
        position=position,
    )


class TestPositionDisambiguation:
    def test_get_cl_position_separates_full_and_micro(self):
        mgr = _make_manager()
        mgr.ib.positions.return_value = [
            _pos("SI", "SI", 3),      # full silver
            _pos("SI", "SIL", -2),    # micro silver
        ]
        assert mgr.get_cl_position(symbol="SIL") == -2
        assert mgr.get_cl_position(symbol="SI") == 3

    def test_account_summary_reads_micro_position(self):
        mgr = _make_manager()
        mgr.ib.accountValues.return_value = []
        mgr.ib.portfolio.return_value = [
            SimpleNamespace(
                contract=SimpleNamespace(symbol="SI", tradingClass="SIL"),
                position=-1, unrealizedPNL=12.0, realizedPNL=0.0,
                marketValue=-59210.0, averageCost=59222.0, marketPrice=59.21,
            ),
            SimpleNamespace(
                contract=SimpleNamespace(symbol="SI", tradingClass="SI"),
                position=4, unrealizedPNL=999.0, realizedPNL=0.0,
                marketValue=1184200.0, averageCost=296000.0, marketPrice=59.21,
            ),
        ]
        summary = IBKRConnectionManager.get_account_summary(mgr, symbol="SIL")
        assert summary["cl_position"] == -1                 # the micro, not +4
        assert summary["cl_unrealized_pnl"] == pytest.approx(12.0)

    def test_cached_position_separates_full_and_micro(self):
        from src.live_execution.adapters.ibkr_execution import IBKRExecutionClient

        with patch(
            "src.live_execution.adapters.ibkr_execution.IBKRConnectionManager"
        ) as MockMgr:
            mock_mgr = MockMgr.return_value
            mock_mgr.ib = MagicMock()
            client = IBKRExecutionClient()

        mock_mgr.ib.portfolio.return_value = [
            _pos("SI", "SI", 3),
            _pos("SI", "SIL", -2),
        ]
        assert client.get_cached_position("SIL") == -2
        assert client.get_cached_position("SI") == 3
        assert client.get_cached_position("ZZ") == 0  # unheld reads flat


# ===========================================================================
# registry_symbol_for_contract — reverse map (IB contract -> config symbol)
# ===========================================================================

class TestRegistrySymbolForContract:
    def test_micro_silver_maps_back_to_sil(self):
        assert registry_symbol_for_contract("SI", "SIL") == "SIL"

    def test_full_silver_maps_to_si(self):
        assert registry_symbol_for_contract("SI", "SI") == "SI"

    @pytest.mark.parametrize("symbol", ["CL", "ES", "MGC", "MCL", "NG"])
    def test_single_product_maps_to_itself(self, symbol):
        assert registry_symbol_for_contract(symbol, symbol) == symbol
        assert registry_symbol_for_contract(symbol, None) == symbol

    def test_unknown_contract_passes_through(self):
        assert registry_symbol_for_contract("ZZ", None) == "ZZ"
        assert registry_symbol_for_contract(None, None) is None


# ===========================================================================
# Exit / cancel paths skip the full ×5000 and act on the ×1000 micro
# ===========================================================================

class TestExitCancelDisambiguation:
    def _mixed_positions(self):
        # position objects need a settable .exchange on their contract
        return [
            SimpleNamespace(
                contract=SimpleNamespace(
                    symbol="SI", tradingClass="SI", exchange="", conId=1
                ),
                position=4,
            ),
            SimpleNamespace(
                contract=SimpleNamespace(
                    symbol="SI", tradingClass="SIL", exchange="", conId=2
                ),
                position=-2,
            ),
        ]

    def test_close_market_targets_micro_not_full(self):
        mgr = _make_manager()
        mgr.ib.positions.return_value = self._mixed_positions()
        mgr.ib.placeOrder.return_value = SimpleNamespace()

        mgr.close_cl_position_market(symbol="SIL")

        placed_contract, order = mgr.ib.placeOrder.call_args[0]
        assert placed_contract.tradingClass == "SIL"      # the micro
        assert placed_contract.exchange == "COMEX"        # config-symbol exchange
        assert order.action == "BUY" and order.totalQuantity == 2  # cover -2

    def test_close_full_targets_full_not_micro(self):
        mgr = _make_manager()
        mgr.ib.positions.return_value = self._mixed_positions()
        mgr.ib.placeOrder.return_value = SimpleNamespace()

        mgr.close_cl_position_market(symbol="SI")

        placed_contract, order = mgr.ib.placeOrder.call_args[0]
        assert placed_contract.tradingClass == "SI"
        assert order.action == "SELL" and order.totalQuantity == 4

    def test_cancel_orders_only_touches_micro(self):
        mgr = _make_manager()
        full = SimpleNamespace(
            contract=SimpleNamespace(symbol="SI", tradingClass="SI"),
            order=SimpleNamespace(orderId=11),
        )
        micro = SimpleNamespace(
            contract=SimpleNamespace(symbol="SI", tradingClass="SIL"),
            order=SimpleNamespace(orderId=22),
        )
        mgr.ib.openTrades.return_value = [full, micro]

        n = mgr.cancel_open_cl_orders(symbol="SIL")

        assert n == 1
        cancelled = mgr.ib.cancelOrder.call_args[0][0]
        assert cancelled.orderId == 22


# ===========================================================================
# Fill / order-status events are stamped with the config symbol (SIL not SI)
# ===========================================================================

class TestEventSymbolReverseMap:
    def _client(self):
        from src.live_execution.adapters.ibkr_execution import IBKRExecutionClient

        with patch(
            "src.live_execution.adapters.ibkr_execution.IBKRConnectionManager"
        ) as MockMgr:
            MockMgr.return_value.ib = MagicMock()
            return IBKRExecutionClient()

    def test_order_status_event_stamped_sil(self):
        client = self._client()
        captured = []
        client._order_callbacks.append(captured.append)
        trade = SimpleNamespace(
            order=SimpleNamespace(orderId=7),
            contract=SimpleNamespace(symbol="SI", tradingClass="SIL"),
            orderStatus=SimpleNamespace(
                status="Filled", filled=1, remaining=0, avgFillPrice=59.21
            ),
        )
        client._on_order_status(trade)
        assert captured and captured[0].symbol == "SIL"

    def test_commission_event_stamped_sil(self):
        client = self._client()
        captured = []
        client._commission_callbacks.append(captured.append)
        trade = SimpleNamespace(
            order=SimpleNamespace(orderId=7),
            contract=SimpleNamespace(symbol="SI", tradingClass="SIL"),
        )
        fill = SimpleNamespace(execution=SimpleNamespace(execId="e1"))
        report = SimpleNamespace(commission=2.5, realizedPNL=None, currency="USD")
        client._on_commission_report(trade, fill, report)
        assert captured and captured[0].symbol == "SIL"
