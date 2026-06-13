import unittest
from unittest.mock import MagicMock, patch
from src.live_execution.interfaces.execution_interface import StandardExecutionEvent
from src.live_execution.adapters.ibkr_execution import IBKRExecutionClient

class TestIBKRExecutionClient(unittest.TestCase):
    @patch('src.live_execution.adapters.ibkr_execution.IBKRConnectionManager')
    def test_order_status_translation(self, MockIBKRConnectionManager):
        # Mock IBKRConnectionManager to avoid real network
        mock_mgr = MockIBKRConnectionManager.return_value
        mock_mgr.ib = MagicMock()
        
        client = IBKRExecutionClient()
        
        # Setup dummy callback
        received_events = []
        def dummy_callback(event: StandardExecutionEvent):
            received_events.append(event)
        
        client.register_order_status_callback(dummy_callback)
        
        # Simulate inbound fill from ib_insync
        mock_trade = MagicMock()
        mock_trade.order.orderId = 12345
        mock_trade.contract.symbol = 'CL'
        mock_trade.orderStatus.status = 'Filled'
        mock_trade.orderStatus.filled = 2.0
        mock_trade.orderStatus.remaining = 0.0
        mock_trade.orderStatus.avgFillPrice = 80.50
        
        # Fire event manually
        client._on_order_status(mock_trade)
        
        # Assertions
        self.assertEqual(len(received_events), 1)
        event = received_events[0]
        self.assertEqual(event.order_id, '12345')
        self.assertEqual(event.symbol, 'CL')
        self.assertEqual(event.status, 'Filled')
        self.assertEqual(event.filled_qty, 2)
        self.assertEqual(event.avg_price, 80.50)

if __name__ == '__main__':
    unittest.main()
