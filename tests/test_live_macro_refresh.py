"""
Tests for LiveTrader automatic macro feature refresh and loud failures.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from src.features.macro_features import MacroFeatureEngine
from src.live_execution.live_trader import LiveTrader


class DummyStrategy:
    def __init__(self, feature_names: list[str]):
        self.feature_names = feature_names
        self.name = "DummyStrategy"
        self.direction = "LONG"
        self.config = {}


class TestLiveMacroRefresh:
    """Test macro refresh detection, startup checks, and heartbeat hooks."""

    def test_needs_macro_detection(self):
        """Verify _needs_macro is True only if strategy uses macro features."""
        # Case 1: Strategy has macro features
        strategy_with_macro = DummyStrategy(feature_names=["MACD", "MACRO_VIX"])
        trader_1 = LiveTrader(
            strategy=strategy_with_macro,
            dry_run=True,
        )
        assert trader_1._needs_macro is True

        # Case 2: Strategy does not have macro features
        strategy_without_macro = DummyStrategy(feature_names=["MACD", "ATR_14"])
        trader_2 = LiveTrader(
            strategy=strategy_without_macro,
            dry_run=True,
        )
        assert trader_2._needs_macro is False

    @patch.dict(os.environ, {}, clear=True)
    def test_stale_refresh_raises_on_missing_api_key(self):
        """ValueError must be raised loudly if FRED_API_KEY is missing when refreshing stale data."""
        engine = MacroFeatureEngine()
        engine.fred_path = MagicMock()
        # Mock fred_path to exist and be extremely stale (10 days old)
        engine.fred_path.exists.return_value = True
        engine.fred_path.stat.return_value.st_mtime = 0.0

        with pytest.raises(ValueError) as excinfo:
            engine.refresh_if_stale()
        
        assert "FRED_API_KEY not set" in str(excinfo.value)

    @patch("src.features.macro_features.MacroFeatureEngine.refresh_if_stale")
    def test_heartbeat_triggers_refresh(self, mock_refresh):
        """Heartbeat must invoke refresh_if_stale when needs_macro is True but rate-limit to once per hour."""
        strategy = DummyStrategy(feature_names=["MACRO_VIX"])
        trader = LiveTrader(
            strategy=strategy,
            dry_run=True,
        )
        assert trader._needs_macro is True
        assert trader._last_macro_check_time == 0.0

        # Trigger heartbeat log (mock dependencies so it doesn't try to touch live portfolios)
        trader.manager = MagicMock()
        trader._get_market_status = MagicMock(return_value="OPEN")
        trader._last_bar_time_5m = None
        trader._subscriptions_lost = False

        # First call: triggers refresh (since elapsed >= 3600)
        trader._log_heartbeat()
        assert mock_refresh.call_count == 1
        assert trader._last_macro_check_time > 0.0

        # Second call immediately: does NOT trigger refresh (rate-limited)
        trader._log_heartbeat()
        assert mock_refresh.call_count == 1
