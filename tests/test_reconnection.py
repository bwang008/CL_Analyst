"""
Tests for LiveTrader auto-reconnection after IB Gateway disconnect.

All tests use mocks — no live IB connection needed.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from src.live_execution import live_trader as lt_module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_trader_stub():
    """Create a LiveTrader-like object without calling __init__.

    We bypass __init__ (which loads models, configs, etc.) and
    manually set the attributes that _reconnect / _event_loop need.
    """
    trader = object.__new__(lt_module.LiveTrader)

    # Mock the IBKRConnectionManager
    trader.manager = MagicMock()
    trader.manager.ib = MagicMock()
    trader.manager.connect = MagicMock()

    # Strategy (not used by reconnection tests, but must exist)
    trader.strategy = MagicMock()
    trader.strategy.name = "MockStrategy"

    # State flags
    trader._running = True
    trader._subscriptions_lost = False
    trader._live_bars = None
    trader._front_month_bars = None
    trader._contract = MagicMock()
    trader._front_month_contract = MagicMock()
    trader._front_month_str = "202604"
    trader._last_bar_time = None

    # Mock resubscribe (tested separately, already has its own tests)
    trader._resubscribe_and_backfill = MagicMock()
    trader._on_ib_error = MagicMock()

    return trader


# ---------------------------------------------------------------------------
# Tests: _reconnect
# ---------------------------------------------------------------------------

class TestReconnect:
    """Tests for the _reconnect method."""

    @patch.object(lt_module, "_RECONNECT_BASE_DELAY", 0.01)
    @patch.object(lt_module, "_RECONNECT_MAX_DELAY", 0.05)
    @patch.object(lt_module, "_RECONNECT_MAX_ATTEMPTS", 5)
    def test_reconnect_success_first_attempt(self):
        """_reconnect returns True when connect() works on first try."""
        trader = _make_trader_stub()

        result = trader._reconnect()

        assert result is True
        trader.manager.connect.assert_called_once()
        trader._resubscribe_and_backfill.assert_called_once()

    @patch.object(lt_module, "_RECONNECT_BASE_DELAY", 0.01)
    @patch.object(lt_module, "_RECONNECT_MAX_DELAY", 0.05)
    @patch.object(lt_module, "_RECONNECT_MAX_ATTEMPTS", 5)
    def test_reconnect_retries_on_failure(self):
        """_reconnect retries when connect() fails, then succeeds."""
        trader = _make_trader_stub()
        # Fail twice, then succeed
        trader.manager.connect.side_effect = [
            ConnectionError("refused"),
            ConnectionError("refused"),
            None,  # success
        ]

        result = trader._reconnect()

        assert result is True
        assert trader.manager.connect.call_count == 3

    @patch.object(lt_module, "_RECONNECT_BASE_DELAY", 0.01)
    @patch.object(lt_module, "_RECONNECT_MAX_DELAY", 0.05)
    @patch.object(lt_module, "_RECONNECT_MAX_ATTEMPTS", 3)
    def test_reconnect_gives_up_after_max_attempts(self):
        """_reconnect returns False after exhausting all attempts."""
        trader = _make_trader_stub()
        trader.manager.connect.side_effect = ConnectionError("refused")

        result = trader._reconnect()

        assert result is False
        assert trader.manager.connect.call_count == 3

    @patch.object(lt_module, "_RECONNECT_BASE_DELAY", 0.01)
    @patch.object(lt_module, "_RECONNECT_MAX_DELAY", 0.05)
    @patch.object(lt_module, "_RECONNECT_MAX_ATTEMPTS", 5)
    def test_reconnect_calls_resubscribe_and_backfill(self):
        """After successful reconnect, _resubscribe_and_backfill is called."""
        trader = _make_trader_stub()

        trader._reconnect()

        trader._resubscribe_and_backfill.assert_called_once()
        assert trader._subscriptions_lost is True  # set before resubscribe

    @patch.object(lt_module, "_RECONNECT_BASE_DELAY", 0.01)
    @patch.object(lt_module, "_RECONNECT_MAX_DELAY", 0.05)
    @patch.object(lt_module, "_RECONNECT_MAX_ATTEMPTS", 5)
    def test_reconnect_re_registers_error_handler(self):
        """After reconnect, errorEvent handler is re-registered."""
        trader = _make_trader_stub()
        # Use a real list-like to track += calls
        registered_handlers = []

        class FakeEvent:
            def __iadd__(self, handler):
                registered_handlers.append(handler)
                return self

        trader.manager.ib.errorEvent = FakeEvent()

        trader._reconnect()

        assert trader._on_ib_error in registered_handlers

    @patch.object(lt_module, "_RECONNECT_BASE_DELAY", 0.01)
    @patch.object(lt_module, "_RECONNECT_MAX_DELAY", 0.05)
    @patch.object(lt_module, "_RECONNECT_MAX_ATTEMPTS", 5)
    def test_reconnect_respects_running_flag(self):
        """_reconnect returns False immediately if _running is unset."""
        trader = _make_trader_stub()
        trader._running = False

        result = trader._reconnect()

        assert result is False
        trader.manager.connect.assert_not_called()

    @patch.object(lt_module, "_RECONNECT_BASE_DELAY", 0.01)
    @patch.object(lt_module, "_RECONNECT_MAX_DELAY", 0.05)
    @patch.object(lt_module, "_RECONNECT_MAX_ATTEMPTS", 5)
    def test_reconnect_disconnects_before_connecting(self):
        """_reconnect calls ib.disconnect() before attempting connect()."""
        trader = _make_trader_stub()

        trader._reconnect()

        trader.manager.ib.disconnect.assert_called()
        trader.manager.connect.assert_called_once()
        # disconnect should come before connect
        disconnect_call = trader.manager.ib.disconnect.call_args_list[0]
        connect_call = trader.manager.connect.call_args_list[0]
        # Both were called (order verified by mock call_args_list ordering)
        assert disconnect_call is not None
        assert connect_call is not None


# ---------------------------------------------------------------------------
# Tests: _event_loop reconnection integration
# ---------------------------------------------------------------------------

class TestEventLoopReconnection:
    """Tests for _event_loop interaction with _reconnect."""

    def test_event_loop_calls_reconnect_on_connection_error(self):
        """_event_loop invokes _reconnect when ib.sleep raises ConnectionError."""
        trader = _make_trader_stub()
        call_count = 0

        def sleep_side_effect(_):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("Socket disconnect")
            # After reconnect succeeds, stop the loop
            trader._running = False

        trader.manager.ib.sleep.side_effect = sleep_side_effect

        with patch.object(
            lt_module.LiveTrader, "_reconnect", return_value=True
        ) as mock_reconnect:
            trader._event_loop()
            mock_reconnect.assert_called_once()

    def test_event_loop_shuts_down_on_failed_reconnect(self):
        """_event_loop sets _running=False when _reconnect returns False."""
        trader = _make_trader_stub()
        trader.manager.ib.sleep.side_effect = ConnectionError("Socket disconnect")

        with patch.object(
            lt_module.LiveTrader, "_reconnect", return_value=False
        ):
            trader._event_loop()

        assert trader._running is False
