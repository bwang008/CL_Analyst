"""Tests for bracket order configuration in IBKRConnectionManager.

Validates that bracket orders use:
- GTC time-in-force (not DAY)
- outsideRth=True for overnight session support
- triggerMethod=1 (double bid/ask) on stop-loss for native exchange triggers
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
        """Parent order should convert to MKT when use_market=True."""
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
