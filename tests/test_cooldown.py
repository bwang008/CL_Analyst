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

    # Mock the IBKR clients
    trader.exec_client = MagicMock()
    trader.exec_client.get_position = MagicMock(return_value=0)
    
    trader.data_client = MagicMock()
    trader.data_client.connect = MagicMock()
    trader.data_client.subscribe_live_bars = MagicMock()
    trader.data_client.cancel_subscription = MagicMock()

    trader._execution_symbol = "CL"
    trader._front_month_local_symbol = "CLH6"
    trader._last_rollover_check_date = None

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
    trader._telegram = MagicMock()
    trader._log_capture = MagicMock()

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
    # TIME BARRIER submit-and-defer state (settle-confirm-event-loop_07202026_0713):
    # the re-entrancy guard at the top of _check_time_barrier reads this.
    trader._pending_exit_order_id = None

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
    trader._trailing_sl_atr_offset_long = 0.25
    trader._trailing_sl_atr_offset_short = 0.25

    # TP/SL order tracking (software-side OCA)
    trader._tp_order_ids = []
    trader._sl_order_id = None
    trader._active_trade_id = None
    # Persistent duplicate-exit guard (introduced in cascade fix)
    trader._processed_exit_order_ids = set()

    # Engine-level position cap and emergency halt
    trader._max_position_size = 3
    trader._emergency_halt = False
    trader._order_timestamps = []

    # Duplicate entry fill guard
    trader._last_filled_entry_order_id = None

    # Consecutive signal threshold state
    trader._consecutive_signal_threshold = 0
    trader._consecutive_buy_count = 0
    trader._consecutive_sell_count = 0
    trader._consecutive_exit_count = 0

    trader._virtual_ledger = {"5m": 0, "1h": 0}

    return trader





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
        trader.exec_client.get_position.return_value = 1
        trader.exec_client.cancel_open_orders.return_value = 0
        trader.exec_client.close_position.return_value = MagicMock(order=MagicMock(orderId=55))
        # re-adjudicated: oca-stage4-exit-ordering_07222026_0155 (retire-then-submit)
        # Mechanical stub repair: tracked legs for the barrier to retire, the
        # Stage-4/side/budget attrs the new flow reads, and a clear open book
        # for the reconciler's retiring-leg scan.
        trader._tp_order_ids = [65]
        trader._sl_order_id = 66
        trader._position_side = 1
        trader._retiring_leg_ids = []
        trader._time_barrier_exit_attempts = 0
        trader.exec_client.get_open_trades.return_value = []
        # re-adjudicated: oca-stage4-exit-ordering_07222026_0155 (retire-then-submit)
        # settled: 1 during leg retirement (still holding -> the reconciler
        # submits the exit on tick 1), 0 on tick 2 (the exit filled).
        _settled_seq = [1, 0]
        trader.exec_client.get_position_settled.side_effect = (
            lambda *a, **k: _settled_seq.pop(0) if _settled_seq else 0
        )
        trader.exec_client.get_executions.return_value = [{"order_id": "55", "price": 72.50}]

        # re-adjudicated: oca-stage4-exit-ordering_07222026_0155 (retire-then-submit)
        # The barrier tick only RETIRES the legs; the reconciler submits the
        # exit on tick 1 — the exit_mode routing assertion now targets that
        # call, priced from the ROLLING FRAME's last close (the reconciler
        # has no bar-callback price) — and books the fill on tick 2.
        submit_result = trader._check_time_barrier(
            bar_time=pd.Timestamp("2026-03-02 18:00:00"),
            current_price=72.50,
            atr_value=0.5,
        )
        assert submit_result is False  # retire-then-submit: no in-tick exit
        assert trader._pending_exit_order_id is None  # no exit exists yet
        handoff = trader._reconcile_pending_position_state()
        assert handoff is False  # tick 1: legs retired, exit submitted
        result = trader._reconcile_pending_position_state()

        assert result is True
        # Verify it called close_position with exit_mode, NOT close_position_market
        trader.exec_client.close_position.assert_called_once_with(
            symbol=trader._execution_symbol,
            exit_mode="marketable_limit",
            current_price=float(trader.rolling_df_5m["Close"].iloc[-1]),
        )
        trader.exec_client.close_position_market.assert_not_called()

    def test_time_barrier_default_market_mode(self):
        """Default exit_mode='market' is passed to close_cl_position."""
        trader = _make_trader_stub(cooldown_bars=0)
        trader._exit_mode = "market"

        trader._max_hold_bars = 5
        trader._position_entry_bar_time = pd.Timestamp("2026-03-02 17:00:00")
        trader._position_bars_held = 6

        trader.exec_client.get_position.return_value = 1
        trader.exec_client.cancel_open_orders.return_value = 0
        trader.exec_client.close_position.return_value = MagicMock(order=MagicMock(orderId=56))
        # re-adjudicated: oca-stage4-exit-ordering_07222026_0155 (retire-then-submit)
        # Mechanical stub repair + settled timeline shift, and the submission
        # moves to the reconciler's tick-1 with the ROLLING FRAME's last
        # close as its price source — same shape as the exit_mode test above.
        trader._tp_order_ids = [65]
        trader._sl_order_id = 66
        trader._position_side = 1
        trader._retiring_leg_ids = []
        trader._time_barrier_exit_attempts = 0
        trader.exec_client.get_open_trades.return_value = []
        _settled_seq = [1, 0]
        trader.exec_client.get_position_settled.side_effect = (
            lambda *a, **k: _settled_seq.pop(0) if _settled_seq else 0
        )
        trader.exec_client.get_executions.return_value = [{"order_id": "56", "price": 72.50}]

        submit_result = trader._check_time_barrier(
            bar_time=pd.Timestamp("2026-03-02 18:00:00"),
            current_price=72.50,
            atr_value=0.5,
        )
        assert submit_result is False  # retire-then-submit: no in-tick exit
        assert trader._pending_exit_order_id is None  # no exit exists yet
        handoff = trader._reconcile_pending_position_state()
        assert handoff is False  # tick 1: legs retired, exit submitted
        trader._reconcile_pending_position_state()  # tick 2: books the fill

        trader.exec_client.close_position.assert_called_once_with(
            symbol=trader._execution_symbol,
            exit_mode="market",
            current_price=float(trader.rolling_df_5m["Close"].iloc[-1]),
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
