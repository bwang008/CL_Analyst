"""
Tests for IBKRConnectionManager port fallback logic.

These tests mock the actual IBKR connection to verify that:
1. The default port is 4002 (IB Gateway)
2. If Gateway port fails, it falls back to TWS port 7497
3. If all ports fail, it raises ConnectionError
4. If primary port succeeds, no fallback is attempted
"""

from unittest.mock import MagicMock, patch, call

import pytest

from src.live_execution.ibkr_client import (
    IBKRConnectionManager,
    _PORT_GATEWAY,
    _PORT_TWS,
)


class TestPortDefaults:
    """Verify the default port configuration."""

    def test_default_port_is_gateway(self):
        """Default port should be 4002 (IB Gateway)."""
        mgr = IBKRConnectionManager()
        assert mgr.port == _PORT_GATEWAY
        assert mgr.port == 4002

    def test_fallback_includes_tws(self):
        """Fallback ports should include TWS (7497)."""
        mgr = IBKRConnectionManager()
        assert _PORT_TWS in mgr.fallback_ports
        assert 7497 in mgr.fallback_ports


class TestPortFallback:
    """Test the connection fallback mechanism."""

    @pytest.fixture
    def mock_ib(self):
        """Patch the IB class to avoid real connections."""
        with patch("src.live_execution.ibkr_client.IB") as MockIB:
            mock_instance = MagicMock()
            mock_instance.isConnected.return_value = False
            MockIB.return_value = mock_instance
            yield mock_instance

    def test_gateway_succeeds_no_fallback(self, mock_ib):
        """When Gateway port works, no fallback is attempted."""
        mock_ib.connect.return_value = None  # success

        mgr = IBKRConnectionManager()
        mgr.connect()

        # Should have connected once on port 4002
        mock_ib.connect.assert_called_once()
        call_kwargs = mock_ib.connect.call_args
        assert call_kwargs.kwargs.get("port") == 4002
        assert mgr.port == 4002

    def test_gateway_fails_falls_back_to_tws(self, mock_ib):
        """When Gateway fails, should try TWS and succeed."""
        # First call (port 4002) fails, second call (port 7497) succeeds
        mock_ib.connect.side_effect = [
            ConnectionError("Gateway refused"),
            None,  # success
        ]

        mgr = IBKRConnectionManager()
        mgr.connect()

        assert mock_ib.connect.call_count == 2
        # Verify second call used TWS port
        second_call = mock_ib.connect.call_args_list[1]
        assert second_call.kwargs.get("port") == 7497
        # Port should be updated to the successful one
        assert mgr.port == 7497

    def test_all_ports_fail_raises_error(self, mock_ib):
        """When all ports fail, should raise ConnectionError."""
        mock_ib.connect.side_effect = ConnectionError("Refused")

        mgr = IBKRConnectionManager()

        with pytest.raises(ConnectionError, match="Could not connect"):
            mgr.connect()

        # Should have tried both ports
        assert mock_ib.connect.call_count == 2

    def test_already_connected_skips(self, mock_ib):
        """If already connected, connect() should be a no-op."""
        mock_ib.isConnected.return_value = True

        mgr = IBKRConnectionManager()
        mgr.connect()

        mock_ib.connect.assert_not_called()

    def test_custom_port_no_duplication(self, mock_ib):
        """If custom port == a fallback port, don't try it twice."""
        mock_ib.connect.side_effect = ConnectionError("Refused")

        mgr = IBKRConnectionManager(port=7497)
        with pytest.raises(ConnectionError):
            mgr.connect()

        # Port 7497 should only be tried once (not duplicated)
        assert mock_ib.connect.call_count == 1

    def test_custom_port_with_gateway_fallback(self, mock_ib):
        """Custom port tries custom first, then defaults."""
        # Custom port 9999 fails, then fallback 7497 also fails
        mock_ib.connect.side_effect = ConnectionError("Refused")

        mgr = IBKRConnectionManager(port=9999)
        with pytest.raises(ConnectionError):
            mgr.connect()

        # Should try 9999, then 7497
        assert mock_ib.connect.call_count == 2
        first_port = mock_ib.connect.call_args_list[0].kwargs.get("port")
        second_port = mock_ib.connect.call_args_list[1].kwargs.get("port")
        assert first_port == 9999
        assert second_port == 7497
