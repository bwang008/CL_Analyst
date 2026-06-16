"""Integration tests for IBKR Execution Adapter.

Tests the full order flow through the adapter layer by mocking the
IBKRConnectionManager.  Exercises all paths that hit IBKR API calls
to ensure the adapter correctly routes to the right manager methods
and never makes async calls from within event loop callbacks.

Tests:
    1. Order status event translation (existing)
    2. resolve_contract — caches qualified contract at startup
    3. place_bracket_order — Phase 1 entry-only (no tp_price/sl_price)
    4. place_bracket_order — full bracket with tp_price/sl_price
    5. place_child_orders — routes to manager with cached contract
    6. cancel_open_orders — delegates to manager
    7. close_position — delegates to manager
    8. _get_contract fails fast if cache not warmed
    9. End-to-end: entry → fill callback → child orders flow
"""
import unittest
from unittest.mock import MagicMock, patch, PropertyMock
from src.live_execution.interfaces.execution_interface import StandardExecutionEvent
from src.live_execution.adapters.ibkr_execution import IBKRExecutionClient


def _make_client_and_manager():
    """Create an IBKRExecutionClient with a fully mocked manager."""
    with patch('src.live_execution.adapters.ibkr_execution.IBKRConnectionManager') as MockMgr:
        mock_mgr = MockMgr.return_value
        mock_mgr.ib = MagicMock()

        # Setup get_front_month_contract / qualify_contract for resolve_contract
        mock_mgr.get_front_month_contract.return_value = ("CLN6", "202607")
        mock_contract = MagicMock()
        mock_contract.localSymbol = "CLN6"
        mock_contract.conId = 304037461
        mock_contract.symbol = "CL"
        mock_mgr.qualify_contract.return_value = mock_contract

        client = IBKRExecutionClient()
        return client, mock_mgr, mock_contract


class TestIBKRExecutionClient(unittest.TestCase):
    @patch('src.live_execution.adapters.ibkr_execution.IBKRConnectionManager')
    def test_order_status_translation(self, MockIBKRConnectionManager):
        """Verify inbound IBKR fill events are translated to StandardExecutionEvent."""
        mock_mgr = MockIBKRConnectionManager.return_value
        mock_mgr.ib = MagicMock()

        client = IBKRExecutionClient()

        received_events = []
        def dummy_callback(event: StandardExecutionEvent):
            received_events.append(event)

        client.register_order_status_callback(dummy_callback)

        mock_trade = MagicMock()
        mock_trade.order.orderId = 12345
        mock_trade.contract.symbol = 'CL'
        mock_trade.orderStatus.status = 'Filled'
        mock_trade.orderStatus.filled = 2.0
        mock_trade.orderStatus.remaining = 0.0
        mock_trade.orderStatus.avgFillPrice = 80.50

        client._on_order_status(mock_trade)

        self.assertEqual(len(received_events), 1)
        event = received_events[0]
        self.assertEqual(event.order_id, '12345')
        self.assertEqual(event.symbol, 'CL')
        self.assertEqual(event.status, 'Filled')
        self.assertEqual(event.filled_qty, 2)
        self.assertEqual(event.avg_price, 80.50)


class TestContractCaching(unittest.TestCase):
    """Verify the contract caching mechanism that prevents event loop re-entry."""

    def test_resolve_contract_caches(self):
        """resolve_contract should call manager APIs once and cache the result."""
        client, mock_mgr, mock_contract = _make_client_and_manager()

        client.resolve_contract("CL")

        mock_mgr.get_front_month_contract.assert_called_once_with(symbol="CL")
        mock_mgr.qualify_contract.assert_called_once()
        self.assertIn("CL", client._cached_contracts)
        self.assertEqual(client._cached_contracts["CL"], mock_contract)

    def test_get_contract_fails_without_resolve(self):
        """_get_contract should raise RuntimeError if resolve_contract wasn't called."""
        client, _, _ = _make_client_and_manager()

        with self.assertRaises(RuntimeError) as ctx:
            client._get_contract("CL")
        self.assertIn("No cached contract", str(ctx.exception))
        self.assertIn("resolve_contract", str(ctx.exception))

    def test_get_contract_returns_cached(self):
        """_get_contract should return the cached contract after resolve."""
        client, _, mock_contract = _make_client_and_manager()
        client.resolve_contract("CL")

        result = client._get_contract("CL")
        self.assertEqual(result, mock_contract)

    def test_resolve_multiple_symbols(self):
        """resolve_contract should support caching multiple symbols (CL, MCL)."""
        client, mock_mgr, _ = _make_client_and_manager()

        # Setup different contracts for different symbols
        mcl_contract = MagicMock()
        mcl_contract.localSymbol = "MCLN6"
        mcl_contract.conId = 999999

        call_count = 0
        def qualify_side_effect(contract):
            nonlocal call_count
            call_count += 1
            if contract.symbol == "MCL":
                return mcl_contract
            return MagicMock(localSymbol="CLN6", conId=304037461)

        mock_mgr.qualify_contract.side_effect = qualify_side_effect
        mock_mgr.get_front_month_contract.return_value = ("CLN6", "202607")

        client.resolve_contract("CL")

        mock_mgr.get_front_month_contract.return_value = ("MCLN6", "202607")
        client.resolve_contract("MCL")

        self.assertIn("CL", client._cached_contracts)
        self.assertIn("MCL", client._cached_contracts)


class TestPlaceBracketOrder(unittest.TestCase):
    """Test the two routing paths in place_bracket_order."""

    def test_entry_only_routes_to_place_entry_order(self):
        """Phase 1 (no tp_price/sl_price) should route to manager.place_entry_order."""
        client, mock_mgr, mock_contract = _make_client_and_manager()
        client.resolve_contract("CL")

        mock_trade = MagicMock()
        mock_trade.order.orderId = 100
        mock_mgr.place_entry_order.return_value = mock_trade

        result = client.place_bracket_order(
            symbol="CL",
            action="BUY",
            quantity=1,
            limit_price=80.42,
            entry_mode="adaptive",
            adaptive_priority="Normal",
        )

        # Should call place_entry_order, NOT place_bracket_order
        mock_mgr.place_entry_order.assert_called_once_with(
            contract=mock_contract,
            action="BUY",
            quantity=1,
            limit_price=80.42,
            entry_mode="adaptive",
            adaptive_priority="Normal",
        )
        mock_mgr.place_bracket_order.assert_not_called()
        self.assertEqual(result, mock_trade)

    def test_full_bracket_routes_to_place_bracket_order(self):
        """Full bracket (with tp_price and sl_price) should route to manager.place_bracket_order."""
        client, mock_mgr, mock_contract = _make_client_and_manager()
        client.resolve_contract("CL")

        mock_trades = [MagicMock(), MagicMock(), MagicMock()]
        mock_mgr.place_bracket_order.return_value = mock_trades

        result = client.place_bracket_order(
            symbol="CL",
            action="BUY",
            quantity=1,
            limit_price=80.42,
            tp_price=82.00,
            sl_price=78.00,
            entry_mode="adaptive",
        )

        mock_mgr.place_bracket_order.assert_called_once_with(
            contract=mock_contract,
            action="BUY",
            quantity=1,
            limit_price=80.42,
            tp_price=82.00,
            sl_price=78.00,
            entry_mode="adaptive",
        )
        mock_mgr.place_entry_order.assert_not_called()
        self.assertEqual(result, mock_trades)

    def test_entry_only_without_resolve_raises(self):
        """Calling place_bracket_order without resolve_contract should fail fast."""
        client, _, _ = _make_client_and_manager()

        with self.assertRaises(RuntimeError) as ctx:
            client.place_bracket_order(
                symbol="CL",
                action="BUY",
                quantity=1,
                limit_price=80.42,
            )
        self.assertIn("No cached contract", str(ctx.exception))


class TestPlaceChildOrders(unittest.TestCase):
    """Test child order placement (Phase 2: TP/SL after fill)."""

    def test_place_child_orders_uses_cached_contract(self):
        """place_child_orders should use cached contract, not re-resolve."""
        client, mock_mgr, mock_contract = _make_client_and_manager()
        client.resolve_contract("CL")

        mock_tp_trade = MagicMock()
        mock_sl_trade = MagicMock()
        mock_mgr.place_child_orders.return_value = [mock_tp_trade, mock_sl_trade]

        result = client.place_child_orders(
            symbol="CL",
            parent_order_id=100,
            action="SELL",
            quantity=1,
            tp_price=82.00,
            sl_price=78.00,
        )

        mock_mgr.place_child_orders.assert_called_once_with(
            contract=mock_contract,
            parent_order_id=100,
            action="SELL",
            quantity=1,
            tp_price=82.00,
            sl_price=78.00,
        )
        # Verify no async calls were made
        mock_mgr.get_front_month_contract.assert_called_once()  # only from resolve_contract
        self.assertEqual(len(result), 2)

    def test_place_child_orders_without_resolve_raises(self):
        """place_child_orders should fail fast if contract not cached."""
        client, _, _ = _make_client_and_manager()

        with self.assertRaises(RuntimeError):
            client.place_child_orders(
                symbol="CL",
                parent_order_id=100,
                action="SELL",
                quantity=1,
                tp_price=82.00,
                sl_price=78.00,
            )


class TestDelegationMethods(unittest.TestCase):
    """Test pass-through methods that should delegate to manager."""

    def test_cancel_open_orders(self):
        client, mock_mgr, _ = _make_client_and_manager()
        mock_mgr.cancel_open_cl_orders.return_value = 2

        result = client.cancel_open_orders(symbol="CL")

        mock_mgr.cancel_open_cl_orders.assert_called_once_with(symbol="CL")
        self.assertEqual(result, 2)

    def test_close_position(self):
        client, mock_mgr, _ = _make_client_and_manager()
        mock_trade = MagicMock()
        mock_mgr.close_cl_position.return_value = mock_trade

        result = client.close_position(
            symbol="CL",
            exit_mode="market",
            current_price=80.50,
        )

        mock_mgr.close_cl_position.assert_called_once_with(
            symbol="CL",
            exit_mode="market",
            current_price=80.50,
        )
        self.assertEqual(result, mock_trade)

    def test_get_position(self):
        client, mock_mgr, _ = _make_client_and_manager()
        mock_mgr.get_cl_position.return_value = 2

        result = client.get_position(symbol="CL")

        mock_mgr.get_cl_position.assert_called_once_with(symbol="CL")
        self.assertEqual(result, 2)


class TestEndToEndOrderFlow(unittest.TestCase):
    """Simulate the full two-phase order flow: entry → fill → children.

    This reproduces the exact call sequence that happens when the
    live_trader gets a signal, places an entry, gets a fill callback,
    and then places TP/SL children — all using cached contracts.
    """

    def test_full_entry_fill_children_flow(self):
        """Entry-only → fill → child orders should all use cached contract."""
        client, mock_mgr, mock_contract = _make_client_and_manager()

        # ── Startup: resolve contract (outside event loop) ────────────
        client.resolve_contract("CL")
        resolve_call_count = mock_mgr.get_front_month_contract.call_count
        self.assertEqual(resolve_call_count, 1)

        # ── Phase 1: Entry order (from bar callback, inside event loop) ──
        mock_entry_trade = MagicMock()
        mock_entry_trade.order.orderId = 42
        mock_entry_trade.order.orderType = "LMT"
        mock_entry_trade.order.action = "BUY"
        mock_mgr.place_entry_order.return_value = mock_entry_trade

        entry_result = client.place_bracket_order(
            symbol="CL",
            action="BUY",
            quantity=1,
            limit_price=80.42,
            entry_mode="adaptive",
            adaptive_priority="Normal",
        )

        # Verify entry-only routing
        mock_mgr.place_entry_order.assert_called_once()
        mock_mgr.place_bracket_order.assert_not_called()
        self.assertEqual(entry_result.order.orderId, 42)

        # No new contract resolution should have happened
        self.assertEqual(
            mock_mgr.get_front_month_contract.call_count,
            resolve_call_count,
            "No new get_front_month_contract calls should happen during order placement"
        )
        self.assertEqual(
            mock_mgr.qualify_contract.call_count,
            1,  # only from resolve_contract
            "No new qualify_contract calls should happen during order placement"
        )

        # ── Phase 2: Fill callback fires → place TP/SL children ──────
        fill_price = 80.44
        tp_price = round(fill_price + 1.58, 2)  # 82.02
        sl_price = round(fill_price - 3.47, 2)  # 76.97

        mock_tp_trade = MagicMock()
        mock_tp_trade.order.orderId = 43
        mock_sl_trade = MagicMock()
        mock_sl_trade.order.orderId = 44
        mock_mgr.place_child_orders.return_value = [mock_tp_trade, mock_sl_trade]

        child_result = client.place_child_orders(
            symbol="CL",
            parent_order_id=42,
            action="SELL",
            quantity=1,
            tp_price=tp_price,
            sl_price=sl_price,
        )

        # Verify child orders used cached contract
        mock_mgr.place_child_orders.assert_called_once_with(
            contract=mock_contract,
            parent_order_id=42,
            action="SELL",
            quantity=1,
            tp_price=tp_price,
            sl_price=sl_price,
        )
        self.assertEqual(len(child_result), 2)

        # Still no new contract resolution
        self.assertEqual(
            mock_mgr.get_front_month_contract.call_count,
            resolve_call_count,
            "Child order placement should not trigger contract resolution"
        )

        # ── Simulate exit: cancel remaining orders + close position ──
        mock_mgr.cancel_open_cl_orders.return_value = 1
        cancelled = client.cancel_open_orders(symbol="CL")
        self.assertEqual(cancelled, 1)

        mock_mgr.close_cl_position.return_value = MagicMock()
        client.close_position(symbol="CL", exit_mode="market", current_price=82.00)

        # Final assertion: throughout the entire flow, contract was only
        # resolved ONCE at startup — never re-resolved during the event loop
        self.assertEqual(mock_mgr.get_front_month_contract.call_count, 1)
        self.assertEqual(mock_mgr.qualify_contract.call_count, 1)


if __name__ == '__main__':
    unittest.main()
