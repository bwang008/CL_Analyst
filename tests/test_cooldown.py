"""
Tests for LiveTrader cooldown logic and timezone-safe resubscription.

Validates:
- Post-exit cooldown blocks re-entry for configured number of bars
- Cooldown counts down correctly then allows new entries
- cooldown_bars=0 disables the feature (backward compat)
- Timezone-safe gap calculation in _resubscribe_and_backfill

All tests use mocks — no live IB connection or real models needed.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from src.live_execution import live_trader as lt_module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_trader_stub(
    cooldown_bars: int = 10,
    tp_cooldown_bars: int | None = None,
    sl_cooldown_bars: int | None = None,
) -> lt_module.LiveTrader:
    """Create a LiveTrader-like object with cooldown support.

    Bypasses __init__ and manually sets all attributes needed for
    cooldown tests.
    """
    trader = object.__new__(lt_module.LiveTrader)

    # Mock the IBKRConnectionManager
    trader.manager = MagicMock()
    trader.manager.ib = MagicMock()
    trader.manager.connect = MagicMock()
    trader.manager.get_cl_position = MagicMock(return_value=0)
    trader.manager.subscribe_live_bars = MagicMock()
    trader.manager.cancel_subscription = MagicMock()

    # Strategy mock
    trader.strategy = MagicMock()
    trader.strategy.name = "MockStrategy"
    trader.strategy.direction = "BOTH"
    trader.strategy.config = {"cooldown_bars": cooldown_bars}

    # State
    trader._running = True
    trader._subscriptions_lost = False
    trader._live_bars_5m = None
    trader._live_bars_1h = None
    trader._front_month_bars = None
    trader._contract = MagicMock()
    trader._front_month_contract = MagicMock()
    trader._front_month_str = "202604"
    trader._last_bar_time_5m = None
    trader._last_bar_time_1h = None
    trader._max_hold_bars = 288

    # Cooldown state
    trader._tp_cooldown_bars = tp_cooldown_bars if tp_cooldown_bars is not None else cooldown_bars
    trader._sl_cooldown_bars = sl_cooldown_bars if sl_cooldown_bars is not None else cooldown_bars
    trader._cooldown_remaining = 0

    # Position tracking
    trader._position_entry_bar_time = None
    trader._position_bars_held = 0

    # Telemetry mock
    trader.telemetry = MagicMock()

    # Feature/model mocks
    trader.feature_names = ["ATR_14", "MACD"]
    trader.rolling_df_5m = pd.DataFrame(
        {"DateTime": pd.date_range("2026-01-01", periods=200, freq="5min"),
         "Open": 70.0, "High": 71.0, "Low": 69.0, "Close": 70.5,
         "Volume": 100.0}
    )
    trader.rolling_df_5m = trader.rolling_df_5m.set_index(
        pd.DatetimeIndex(trader.rolling_df_5m["DateTime"]), drop=False
    )
    trader.rolling_df_1h = None

    # Dry-run mode (don't place real orders)
    trader.dry_run = True
    trader._bar_size = "5m"
    trader.entry_mode = "market"
    trader.exit_mode = "marketable_limit"
    trader._exit_mode = "marketable_limit"
    trader.adaptive_priority = "Normal"

    # Execution config (added in Phase 1/Phase 2 refactor)
    trader._execution_symbol = "CL"
    trader._lean_features = False

    # Execution callback state
    trader._callbacks_registered = False
    trader._last_decision_context_by_order_id = {}

    # Entry order TTL state
    trader._pending_entry_order_id = None
    trader._pending_entry_bar_time = None

    # Trailing stop state
    trader._trailing_activated = False
    trader._entry_price = None
    trader._atr_at_entry = None
    trader._position_side = 0
    trader._highest_high = 0.0
    trader._lowest_low = float("inf")
    trader._trade_trailing_atr_mult = None
    trader._trade_max_hold_bars = None
    trader._trailing_atr_mult = 100.0
    trader._trailing_sl_atr_offset = 0.25

    # TP/SL order tracking (software-side OCA)
    trader._tp_order_ids = []
    trader._sl_order_id = None
    trader._active_trade_id = None

    # Engine-level position cap
    trader._max_position_size = 3

    # Consecutive signal threshold state
    trader._consecutive_signal_threshold = 0
    trader._consecutive_buy_count = 0
    trader._consecutive_sell_count = 0

    trader._virtual_ledger = {"5m": 0, "1h": 0}

    return trader


# ---------------------------------------------------------------------------
# Tests: Cooldown in _on_new_bar
# ---------------------------------------------------------------------------


class TestCooldownEnforcement:
    """Verify cooldown blocks re-entry after exits."""

    @patch("src.live_execution.live_trader.build_live_features")
    def test_cooldown_skips_entry(self, mock_features):
        """When cooldown is active, _on_new_bar evaluates but skips execution."""
        from src.live_execution.strategy import TradeSignal
        trader = _make_trader_stub(cooldown_bars=10)
        trader._cooldown_remaining = 5

        # Features and strategy ARE called during cooldown (for INFERENCE logs)
        mock_features.return_value = pd.DataFrame(
            {"ATR_14": [0.5], "MACD": [0.1]}
        )
        trader.strategy.evaluate.return_value = TradeSignal(
            action="HOLD",
            probability=0.4,
            confidence_pct=40.0,
            signal_label="Hold",
            skip_reason="BELOW_THRESHOLD",
            buy_prob=0.4,
            sell_prob=0.1,
        )

        bar_time = pd.Timestamp("2026-03-02 18:00:00")
        trader._on_new_bar(bar_time, trader.rolling_df_5m, "5m")

        # Features and strategy SHOULD be called during cooldown
        mock_features.assert_called_once()
        trader.strategy.evaluate.assert_called_once()
        # But telemetry should log COOLDOWN action
        trader.telemetry.log_signal.assert_called_once()
        call_kwargs = trader.telemetry.log_signal.call_args[1]
        assert call_kwargs["action_taken"] == "COOLDOWN"

    @patch("src.live_execution.live_trader.build_live_features")
    def test_cooldown_counts_down(self, mock_features):
        """Cooldown decrements each bar and eventually allows entry."""
        from src.live_execution.strategy import TradeSignal
        trader = _make_trader_stub(cooldown_bars=3)
        trader._cooldown_remaining = 3

        hold_signal = TradeSignal(
            action="HOLD",
            probability=0.4,
            confidence_pct=40.0,
            signal_label="Hold",
            skip_reason="BELOW_THRESHOLD",
            buy_prob=0.4,
            sell_prob=0.1,
        )
        mock_features.return_value = pd.DataFrame(
            {"ATR_14": [0.5], "MACD": [0.1]}
        )
        trader.strategy.evaluate.return_value = hold_signal

        bar_time = pd.Timestamp("2026-03-02 18:00:00")

        # Bar 1: cooldown=3 → evaluate but skip execution
        trader._on_new_bar(bar_time, trader.rolling_df_5m, "5m")
        assert trader._cooldown_remaining == 2

        # Bar 2: cooldown=2 → evaluate but skip execution
        trader._on_new_bar(bar_time, trader.rolling_df_5m, "5m")
        assert trader._cooldown_remaining == 1

        # Bar 3: cooldown=1 → evaluate but skip execution
        trader._on_new_bar(bar_time, trader.rolling_df_5m, "5m")
        assert trader._cooldown_remaining == 0

        # Bar 4: cooldown=0 → normal evaluation
        trader._on_new_bar(bar_time, trader.rolling_df_5m, "5m")
        assert mock_features.call_count == 4
        assert trader.strategy.evaluate.call_count == 4

    @patch("src.live_execution.live_trader.build_live_features")
    def test_zero_cooldown_disabled(self, mock_features):
        """cooldown_bars=0 means no cooldown — backward compatible."""
        from src.live_execution.strategy import TradeSignal
        trader = _make_trader_stub(cooldown_bars=0)
        trader._cooldown_remaining = 0

        mock_features.return_value = pd.DataFrame(
            {"ATR_14": [0.5], "MACD": [0.1]}
        )
        trader.strategy.evaluate.return_value = TradeSignal(
            action="HOLD",
            probability=0.4,
            confidence_pct=40.0,
            signal_label="Hold",
            skip_reason="BELOW_THRESHOLD",
            buy_prob=0.4,
            sell_prob=0.1,
        )

        bar_time = pd.Timestamp("2026-03-02 18:00:00")
        trader._on_new_bar(bar_time, trader.rolling_df_5m, "5m")

        # Should call features right away
        mock_features.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: Cooldown activation on exit
# ---------------------------------------------------------------------------


class TestCooldownActivation:
    """Verify cooldown is activated when bracket child orders fill."""

    def test_sl_hit_activates_cooldown(self):
        """A stop-loss fill should activate cooldown."""
        trader = _make_trader_stub(cooldown_bars=10)

        # Simulate a STP child order fill
        mock_trade = MagicMock()
        mock_order = MagicMock()
        mock_order.orderType = "STP"
        mock_order.parentId = 42  # non-zero = child order
        mock_order.orderId = 99
        mock_order.action = "BUY"
        mock_order.totalQuantity = 2.0
        mock_order.tif = "GTC"
        mock_order.permId = 12345
        mock_order.lmtPrice = 0.0
        mock_order.auxPrice = 72.64
        mock_order.account = "DU1899929"

        mock_status = MagicMock()
        mock_status.status = "Filled"
        mock_status.avgFillPrice = 72.65
        mock_status.filled = 2.0
        mock_status.remaining = 0.0

        mock_contract = MagicMock()
        mock_contract.localSymbol = "CLJ6"
        mock_contract.symbol = "CL"
        mock_contract.lastTradeDateOrContractMonth = "20260320"

        mock_trade.order = mock_order
        mock_trade.orderStatus = mock_status
        mock_trade.contract = mock_contract

        # Set SL order ID so the fill is recognized as an SL hit
        trader._sl_order_id = 99

        assert trader._cooldown_remaining == 0
        trader._on_order_status(mock_trade)
        assert trader._cooldown_remaining == 10  # sl_cooldown_bars=10

    def test_tp_hit_activates_cooldown(self):
        """A take-profit fill should activate cooldown."""
        trader = _make_trader_stub(tp_cooldown_bars=5)

        mock_trade = MagicMock()
        mock_order = MagicMock()
        mock_order.orderType = "LMT"
        mock_order.parentId = 42
        mock_order.orderId = 100
        mock_order.action = "BUY"
        mock_order.totalQuantity = 2.0
        mock_order.tif = "GTC"
        mock_order.permId = 12346
        mock_order.lmtPrice = 71.83
        mock_order.auxPrice = 0.0
        mock_order.account = "DU1899929"

        mock_status = MagicMock()
        mock_status.status = "Filled"
        mock_status.avgFillPrice = 71.83
        mock_status.filled = 2.0
        mock_status.remaining = 0.0

        mock_contract = MagicMock()
        mock_contract.localSymbol = "CLJ6"
        mock_contract.symbol = "CL"
        mock_contract.lastTradeDateOrContractMonth = "20260320"

        mock_trade.order = mock_order
        mock_trade.orderStatus = mock_status
        mock_trade.contract = mock_contract

        # Set TP order ID so the fill is recognized as a TP hit
        trader._tp_order_ids = [100]

        assert trader._cooldown_remaining == 0
        trader._on_order_status(mock_trade)
        assert trader._cooldown_remaining == 5  # tp_cooldown_bars=5

    def test_parent_fill_does_not_activate_cooldown(self):
        """A parent entry order fill should NOT activate cooldown."""
        trader = _make_trader_stub(cooldown_bars=10)

        mock_trade = MagicMock()
        mock_order = MagicMock()
        mock_order.orderType = "LMT"
        mock_order.parentId = 0  # parentId=0 → is the parent entry order
        mock_order.orderId = 59
        mock_order.action = "SELL"
        mock_order.totalQuantity = 2.0
        mock_order.tif = "GTC"
        mock_order.permId = 12340
        mock_order.lmtPrice = 72.25
        mock_order.auxPrice = 0.0
        mock_order.account = "DU1899929"

        mock_status = MagicMock()
        mock_status.status = "Filled"
        mock_status.avgFillPrice = 72.26
        mock_status.filled = 2.0
        mock_status.remaining = 0.0

        mock_contract = MagicMock()
        mock_contract.localSymbol = "CLJ6"
        mock_contract.symbol = "CL"
        mock_contract.lastTradeDateOrContractMonth = "20260320"

        mock_trade.order = mock_order
        mock_trade.orderStatus = mock_status
        mock_trade.contract = mock_contract

        trader._on_order_status(mock_trade)
        assert trader._cooldown_remaining == 0  # NOT activated

    def test_cooldown_zero_no_activation(self):
        """With cooldown_bars=0, exit does not set cooldown."""
        trader = _make_trader_stub(cooldown_bars=0)

        mock_trade = MagicMock()
        mock_order = MagicMock()
        mock_order.orderType = "STP"
        mock_order.parentId = 42
        mock_order.orderId = 99
        mock_order.action = "BUY"
        mock_order.totalQuantity = 2.0
        mock_order.tif = "GTC"
        mock_order.permId = 12345
        mock_order.lmtPrice = 0.0
        mock_order.auxPrice = 72.64
        mock_order.account = "DU1899929"

        # Set SL order ID so the fill is recognized
        trader._sl_order_id = 99

        mock_status = MagicMock()
        mock_status.status = "Filled"
        mock_status.avgFillPrice = 72.65
        mock_status.filled = 2.0
        mock_status.remaining = 0.0

        mock_contract = MagicMock()
        mock_contract.localSymbol = "CLJ6"
        mock_contract.symbol = "CL"
        mock_contract.lastTradeDateOrContractMonth = "20260320"

        mock_trade.order = mock_order
        mock_trade.orderStatus = mock_status
        mock_trade.contract = mock_contract

        trader._on_order_status(mock_trade)
        assert trader._cooldown_remaining == 0


# ---------------------------------------------------------------------------
# Tests: Time-barrier exit mode
# ---------------------------------------------------------------------------


class TestTimeBarrierExitMode:
    """Verify _check_time_barrier uses exit_mode from config."""

    def test_time_barrier_calls_close_with_exit_mode(self):
        """Time barrier uses close_cl_position(exit_mode=...) not close_cl_position_market()."""
        trader = _make_trader_stub(cooldown_bars=0)
        trader._exit_mode = "marketable_limit"

        # Simulate a position that has exceeded max_hold_bars
        trader._max_hold_bars = 5
        trader._position_entry_bar_time = pd.Timestamp("2026-03-02 17:00:00")
        trader._position_bars_held = 6  # > max_hold_bars

        # Position is open
        trader.manager.get_cl_position.return_value = 1
        trader.manager.cancel_open_cl_orders.return_value = 0
        trader.manager.close_cl_position.return_value = MagicMock()

        result = trader._check_time_barrier(
            bar_time=pd.Timestamp("2026-03-02 18:00:00"),
            current_price=72.50,
            atr_value=0.5,
        )

        assert result is True
        # Verify it called close_cl_position with exit_mode, NOT close_cl_position_market
        trader.manager.close_cl_position.assert_called_once_with(
            symbol="CL",
            exit_mode="marketable_limit",
            current_price=72.50,
        )
        trader.manager.close_cl_position_market.assert_not_called()

    def test_time_barrier_default_market_mode(self):
        """Default exit_mode='market' is passed to close_cl_position."""
        trader = _make_trader_stub(cooldown_bars=0)
        trader._exit_mode = "market"

        trader._max_hold_bars = 5
        trader._position_entry_bar_time = pd.Timestamp("2026-03-02 17:00:00")
        trader._position_bars_held = 6

        trader.manager.get_cl_position.return_value = 1
        trader.manager.cancel_open_cl_orders.return_value = 0
        trader.manager.close_cl_position.return_value = MagicMock()

        trader._check_time_barrier(
            bar_time=pd.Timestamp("2026-03-02 18:00:00"),
            current_price=72.50,
            atr_value=0.5,
        )

        trader.manager.close_cl_position.assert_called_once_with(
            symbol="CL",
            exit_mode="market",
            current_price=72.50,
        )


# ---------------------------------------------------------------------------
# Tests: Timezone-safe gap calculation
# ---------------------------------------------------------------------------


class TestTimezoneResubscribe:
    """Verify _resubscribe_and_backfill handles tz-aware/naive datetimes."""

    def test_tz_naive_last_bar_time(self):
        """Gap calculation works when _last_bar_time is tz-naive."""
        trader = _make_trader_stub()
        # tz-naive timestamp (e.g., from warm-start cache)
        trader._last_bar_time = pd.Timestamp("2026-03-02 18:00:00")
        trader._subscriptions_lost = True

        # Should NOT raise TypeError
        trader._resubscribe_and_backfill()

    def test_tz_aware_last_bar_time(self):
        """Gap calculation works when _last_bar_time is tz-aware (UTC)."""
        trader = _make_trader_stub()
        # tz-aware timestamp (e.g., from IBKR bar callback)
        trader._last_bar_time = pd.Timestamp(
            "2026-03-02 18:00:00", tz="UTC"
        )
        trader._subscriptions_lost = True

        # Should NOT raise TypeError
        trader._resubscribe_and_backfill()

    def test_tz_aware_eastern_last_bar_time(self):
        """Gap calculation works when _last_bar_time is tz-aware (US/Eastern)."""
        trader = _make_trader_stub()
        # tz-aware in US/Eastern (IBKR sends these)
        trader._last_bar_time = pd.Timestamp(
            "2026-03-02 13:00:00", tz="US/Eastern"
        )
        trader._subscriptions_lost = True

        # Should NOT raise TypeError
        trader._resubscribe_and_backfill()
