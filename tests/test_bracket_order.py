"""Tests for bracket order configuration in IBKRConnectionManager.

Validates that bracket orders use:
- GTC time-in-force (not DAY)
- outsideRth=True for overnight session support
- triggerMethod=1 (double bid/ask) on stop-loss for native exchange triggers
- Entry modes: adaptive (IBALGO), marketable_limit, and market
"""

from unittest.mock import MagicMock, patch

import pytest

from src.live_execution.ibkr_client import IBKRConnectionManager


@pytest.fixture
def manager():
    """Create an IBKRConnectionManager with a mocked IB connection."""
    with patch("src.live_execution.ibkr_client.IB") as MockIB:
        mgr = IBKRConnectionManager(host="127.0.0.1", port=4002, client_id=99)
        mgr.ib = MockIB.return_value
        mgr.ib.isConnected.return_value = True

        # Create realistic mock orders for the bracket
        parent = MagicMock()
        parent.orderType = "LMT"
        parent.lmtPrice = 65.00

        tp_child = MagicMock()
        tp_child.orderType = "LMT"

        sl_child = MagicMock()
        sl_child.orderType = "STP"

        mgr.ib.bracketOrder.return_value = [parent, tp_child, sl_child]
        mgr.ib.placeOrder.side_effect = lambda c, o: MagicMock(order=o)

        yield mgr, parent, tp_child, sl_child


class TestBracketOrderConfig:
    """Verify bracket order has correct GTC/outsideRth/triggerMethod settings."""

    def test_all_orders_use_gtc(self, manager):
        mgr, parent, tp_child, sl_child = manager
        mgr.place_bracket_order(
            contract=MagicMock(),
            action="BUY",
            quantity=1,
            limit_price=65.00,
            tp_price=65.50,
            sl_price=64.50,
        )
        assert parent.tif == "GTC"
        assert tp_child.tif == "GTC"
        assert sl_child.tif == "GTC"

    def test_all_orders_outside_rth(self, manager):
        mgr, parent, tp_child, sl_child = manager
        mgr.place_bracket_order(
            contract=MagicMock(),
            action="BUY",
            quantity=1,
            limit_price=65.00,
            tp_price=65.50,
            sl_price=64.50,
        )
        assert parent.outsideRth is True
        assert tp_child.outsideRth is True
        assert sl_child.outsideRth is True

    def test_stop_loss_trigger_method_double_bid_ask(self, manager):
        """Stop-loss must use triggerMethod=1 (double bid/ask) for native exchange trigger."""
        mgr, parent, tp_child, sl_child = manager
        mgr.place_bracket_order(
            contract=MagicMock(),
            action="BUY",
            quantity=1,
            limit_price=65.00,
            tp_price=65.50,
            sl_price=64.50,
        )
        assert sl_child.triggerMethod == 1

    def test_market_order_conversion(self, manager):
        """Parent order should convert to MKT when use_market=True (backward compat)."""
        mgr, parent, tp_child, sl_child = manager
        mgr.place_bracket_order(
            contract=MagicMock(),
            action="BUY",
            quantity=1,
            limit_price=65.00,
            tp_price=65.50,
            sl_price=64.50,
            use_market=True,
        )
        assert parent.orderType == "MKT"
        assert parent.lmtPrice == 0

    def test_limit_order_preserved(self, manager):
        """Parent should remain LMT when use_market=False."""
        mgr, parent, tp_child, sl_child = manager
        parent.orderType = "LMT"
        parent.lmtPrice = 65.00
        mgr.place_bracket_order(
            contract=MagicMock(),
            action="BUY",
            quantity=1,
            limit_price=65.00,
            tp_price=65.50,
            sl_price=64.50,
            use_market=False,
            entry_mode="adaptive",
        )
        assert parent.orderType == "LMT"
        assert parent.lmtPrice == 65.00

    def test_returns_three_trades(self, manager):
        """Should return [parent, tp, sl] trades."""
        mgr, *_ = manager
        result = mgr.place_bracket_order(
            contract=MagicMock(),
            action="BUY",
            quantity=1,
            limit_price=65.00,
            tp_price=65.50,
            sl_price=64.50,
        )
        assert len(result) == 3
        assert mgr.ib.placeOrder.call_count == 3


class TestAdaptiveAlgoOrder:
    """Verify IBKR Adaptive Algo configuration on parent order."""

    def test_adaptive_algo_order(self, manager):
        """Adaptive mode sets algoStrategy + algoParams on the parent."""
        mgr, parent, _, _ = manager
        mgr.place_bracket_order(
            contract=MagicMock(),
            action="BUY",
            quantity=1,
            limit_price=65.00,
            tp_price=65.50,
            sl_price=64.50,
            entry_mode="adaptive",
        )
        assert parent.orderType == "LMT"
        assert parent.lmtPrice == 65.00
        assert parent.algoStrategy == "Adaptive"
        # Verify algoParams contain the priority TagValue
        assert parent.algoParams is not None
        assert len(parent.algoParams) == 1
        tag = parent.algoParams[0]
        assert tag.tag == "adaptivePriority"
        assert tag.value == "Normal"

    def test_adaptive_algo_urgency_param(self, manager):
        """Configurable priority passes through to algoParams."""
        mgr, parent, _, _ = manager
        mgr.place_bracket_order(
            contract=MagicMock(),
            action="BUY",
            quantity=1,
            limit_price=65.00,
            tp_price=65.50,
            sl_price=64.50,
            entry_mode="adaptive",
            adaptive_priority="Urgent",
        )
        tag = parent.algoParams[0]
        assert tag.tag == "adaptivePriority"
        assert tag.value == "Urgent"

    def test_adaptive_is_default_entry_mode(self, manager):
        """When neither use_market nor entry_mode is set, default is adaptive."""
        mgr, parent, _, _ = manager
        mgr.place_bracket_order(
            contract=MagicMock(),
            action="BUY",
            quantity=1,
            limit_price=65.00,
            tp_price=65.50,
            sl_price=64.50,
        )
        assert parent.orderType == "LMT"
        assert parent.algoStrategy == "Adaptive"


class TestMarketableLimitOrder:
    """Verify marketable limit order pricing."""

    def test_marketable_limit_buy(self, manager):
        """BUY: limit = best_ask + 2 ticks ($0.02)."""
        mgr, parent, _, _ = manager
        # Mock get_bid_ask to return a known spread
        mgr.get_bid_ask = MagicMock(return_value=(64.98, 65.00))

        mgr.place_bracket_order(
            contract=MagicMock(),
            action="BUY",
            quantity=1,
            limit_price=65.00,
            tp_price=65.50,
            sl_price=64.50,
            entry_mode="marketable_limit",
        )
        assert parent.orderType == "LMT"
        # 65.00 (best ask) + 0.02 (2 ticks) = 65.02
        assert parent.lmtPrice == 65.02

    def test_marketable_limit_sell(self, manager):
        """SELL: limit = limit_price - 2 ticks ($0.02)."""
        mgr, parent, _, _ = manager

        mgr.place_bracket_order(
            contract=MagicMock(),
            action="SELL",
            quantity=1,
            limit_price=65.00,
            tp_price=64.50,
            sl_price=65.50,
            entry_mode="marketable_limit",
        )
        assert parent.orderType == "LMT"
        # 65.00 (limit_price) - 0.02 (2 ticks) = 64.98
        assert parent.lmtPrice == 64.98

    def test_marketable_limit_uses_limit_price_buy(self, manager):
        """BUY marketable_limit uses limit_price (no live NBBO fetch)."""
        mgr, parent, _, _ = manager

        mgr.place_bracket_order(
            contract=MagicMock(),
            action="BUY",
            quantity=1,
            limit_price=64.50,
            tp_price=65.00,
            sl_price=64.00,
            entry_mode="marketable_limit",
        )
        assert parent.orderType == "LMT"
        # 64.50 (limit_price) + 0.02 (2 ticks) = 64.52
        assert parent.lmtPrice == 64.52

    def test_marketable_limit_uses_limit_price_sell(self, manager):
        """SELL marketable_limit uses limit_price (no live NBBO fetch)."""
        mgr, parent, _, _ = manager

        mgr.place_bracket_order(
            contract=MagicMock(),
            action="SELL",
            quantity=1,
            limit_price=72.00,
            tp_price=71.50,
            sl_price=72.50,
            entry_mode="marketable_limit",
        )
        assert parent.orderType == "LMT"
        # 72.00 (limit_price) - 0.02 (2 ticks) = 71.98
        assert parent.lmtPrice == 71.98


class TestEntryModeBackwardCompat:
    """Verify backward compatibility of deprecated use_market flag."""

    def test_backward_compat_use_market_true(self, manager):
        """use_market=True without explicit entry_mode → market order."""
        mgr, parent, _, _ = manager
        mgr.place_bracket_order(
            contract=MagicMock(),
            action="BUY",
            quantity=1,
            limit_price=65.00,
            tp_price=65.50,
            sl_price=64.50,
            use_market=True,
        )
        assert parent.orderType == "MKT"
        assert parent.lmtPrice == 0

    def test_entry_mode_market_explicit(self, manager):
        """Explicit entry_mode='market' produces MKT order."""
        mgr, parent, _, _ = manager
        mgr.place_bracket_order(
            contract=MagicMock(),
            action="BUY",
            quantity=1,
            limit_price=65.00,
            tp_price=65.50,
            sl_price=64.50,
            entry_mode="market",
        )
        assert parent.orderType == "MKT"
        assert parent.lmtPrice == 0

    def test_invalid_entry_mode_raises(self, manager):
        """Invalid entry_mode raises ValueError."""
        mgr, *_ = manager
        with pytest.raises(ValueError, match="Invalid entry_mode"):
            mgr.place_bracket_order(
                contract=MagicMock(),
                action="BUY",
                quantity=1,
                limit_price=65.00,
                tp_price=65.50,
                sl_price=64.50,
                entry_mode="invalid_mode",
            )


class TestClosePositionModes:
    """Verify close_cl_position() order modes."""

    @pytest.fixture
    def close_manager(self):
        """IBKRConnectionManager with a mock position to close."""
        with patch("src.live_execution.ibkr_client.IB") as MockIB:
            mgr = IBKRConnectionManager(host="127.0.0.1", port=4002, client_id=99)
            mgr.ib = MockIB.return_value
            mgr.ib.isConnected.return_value = True

            # Mock a LONG position (2 contracts)
            pos = MagicMock()
            pos.contract.symbol = "CL"
            pos.position = 2
            mgr.ib.positions.return_value = [pos]

            # Capture the order placed
            mgr.ib.placeOrder.side_effect = lambda c, o: MagicMock(order=o)

            yield mgr, pos

    def test_close_market_default(self, close_manager):
        """Default exit_mode='market' produces a MarketOrder."""
        mgr, _ = close_manager
        trade = mgr.close_cl_position()
        order = mgr.ib.placeOrder.call_args[0][1]
        assert order.orderType == "MKT"
        assert order.tif == "GTC"
        assert order.outsideRth is True

    def test_close_marketable_limit_sell(self, close_manager):
        """Closing a LONG position: SELL exit priced 2 ticks below current."""
        mgr, _ = close_manager
        trade = mgr.close_cl_position(
            exit_mode="marketable_limit", current_price=72.50,
        )
        order = mgr.ib.placeOrder.call_args[0][1]
        assert order.orderType == "LMT"
        assert order.lmtPrice == 72.48  # 72.50 - 0.02
        assert order.action == "SELL"
        assert order.tif == "GTC"
        assert order.outsideRth is True

    def test_close_marketable_limit_buy(self, close_manager):
        """Closing a SHORT position: BUY exit priced 2 ticks above current."""
        mgr, pos = close_manager
        pos.position = -2  # SHORT
        trade = mgr.close_cl_position(
            exit_mode="marketable_limit", current_price=72.50,
        )
        order = mgr.ib.placeOrder.call_args[0][1]
        assert order.orderType == "LMT"
        assert order.lmtPrice == 72.52  # 72.50 + 0.02
        assert order.action == "BUY"

    def test_close_marketable_limit_fallback_no_price(self, close_manager):
        """Marketable limit without current_price falls back to market."""
        mgr, _ = close_manager
        trade = mgr.close_cl_position(
            exit_mode="marketable_limit", current_price=None,
        )
        order = mgr.ib.placeOrder.call_args[0][1]
        assert order.orderType == "MKT"

    def test_close_adaptive(self, close_manager):
        """Adaptive exit sets algoStrategy on the order."""
        mgr, _ = close_manager
        trade = mgr.close_cl_position(
            exit_mode="adaptive", current_price=72.50,
        )
        order = mgr.ib.placeOrder.call_args[0][1]
        assert order.orderType == "LMT"
        assert order.lmtPrice == 72.50
        assert order.algoStrategy == "Adaptive"
        assert order.algoParams[0].tag == "adaptivePriority"
        assert order.algoParams[0].value == "Urgent"

    def test_close_no_position(self, close_manager):
        """No position returns None without placing any order."""
        mgr, pos = close_manager
        pos.position = 0
        result = mgr.close_cl_position(
            exit_mode="marketable_limit", current_price=72.50,
        )
        assert result is None
        mgr.ib.placeOrder.assert_not_called()

