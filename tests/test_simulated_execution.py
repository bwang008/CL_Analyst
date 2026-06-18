"""Unit tests for SimulatedExecution matching engine.

Validates that the matching engine's fill logic, gap fills, slippage,
commission, and same-bar conflict resolution are identical to
BacktestEngine.

Run:
    python -m pytest tests/test_simulated_execution.py -v
"""

from __future__ import annotations

import pytest
import pandas as pd
from types import SimpleNamespace

from src.live_execution.adapters.simulated_execution import (
    SimulatedExecution,
    _DEFAULT_SLIPPAGE_PER_SIDE,
    _DEFAULT_COMMISSION_PER_SIDE,
    _DEFAULT_CONTRACT_MULTIPLIER,
)
from src.live_execution.interfaces.execution_interface import StandardExecutionEvent


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def engine() -> SimulatedExecution:
    """Fresh matching engine with default slippage/commission."""
    return SimulatedExecution()


@pytest.fixture
def engine_with_fills():
    """Engine + callback recorder."""
    eng = SimulatedExecution()
    fills: list[StandardExecutionEvent] = []
    eng.register_order_status_callback(lambda e: fills.append(e))
    return eng, fills


def _ts(minute: int) -> pd.Timestamp:
    """Convenience: create a timestamp at 2026-01-05 00:{minute}:00."""
    return pd.Timestamp(f"2026-01-05 00:{minute:02d}:00")


# ---------------------------------------------------------------------------
# Test: Slippage Model
# ---------------------------------------------------------------------------

class TestSlippage:
    def test_buy_slippage_adverse(self, engine):
        """Buy fill should be HIGHER than the limit price (adverse for buyer)."""
        fill = engine._apply_slippage(100.00, "BUY")
        assert fill == pytest.approx(100.01)

    def test_sell_slippage_adverse(self, engine):
        """Sell fill should be LOWER than the limit price (adverse for seller)."""
        fill = engine._apply_slippage(100.00, "SELL")
        assert fill == pytest.approx(99.99)

    def test_slippage_round_trip_cost(self, engine):
        """Round-trip slippage = 2 * 0.01 = 0.02 price points."""
        buy_fill = engine._apply_slippage(100.00, "BUY")
        sell_fill = engine._apply_slippage(100.00, "SELL")
        spread = buy_fill - sell_fill
        assert spread == pytest.approx(2 * _DEFAULT_SLIPPAGE_PER_SIDE)


# ---------------------------------------------------------------------------
# Test: Gap Fill Logic
# ---------------------------------------------------------------------------

class TestGapFill:
    def test_long_tp_no_gap(self, engine):
        """Long TP fills at target when bar doesn't gap past it."""
        fill = engine._gap_fill_price(
            bar_open=99.50, target_price=100.00, position_side=1, is_tp=True
        )
        assert fill == pytest.approx(100.00)

    def test_long_tp_gap_up(self, engine):
        """Long TP fills at OPEN when bar gaps up past target."""
        fill = engine._gap_fill_price(
            bar_open=101.00, target_price=100.00, position_side=1, is_tp=True
        )
        assert fill == pytest.approx(101.00)

    def test_long_sl_no_gap(self, engine):
        """Long SL fills at target when bar doesn't gap past it."""
        fill = engine._gap_fill_price(
            bar_open=99.50, target_price=99.00, position_side=1, is_tp=False
        )
        assert fill == pytest.approx(99.00)

    def test_long_sl_gap_down(self, engine):
        """Long SL fills at OPEN when bar gaps down past SL (worse fill)."""
        fill = engine._gap_fill_price(
            bar_open=98.50, target_price=99.00, position_side=1, is_tp=False
        )
        assert fill == pytest.approx(98.50)

    def test_short_tp_no_gap(self, engine):
        """Short TP fills at target when bar doesn't gap past it."""
        fill = engine._gap_fill_price(
            bar_open=100.50, target_price=100.00, position_side=-1, is_tp=True
        )
        assert fill == pytest.approx(100.00)

    def test_short_tp_gap_down(self, engine):
        """Short TP fills at OPEN when bar gaps down past target."""
        fill = engine._gap_fill_price(
            bar_open=99.00, target_price=100.00, position_side=-1, is_tp=True
        )
        assert fill == pytest.approx(99.00)

    def test_short_sl_no_gap(self, engine):
        """Short SL fills at target when bar doesn't gap past it."""
        fill = engine._gap_fill_price(
            bar_open=100.50, target_price=101.00, position_side=-1, is_tp=False
        )
        assert fill == pytest.approx(101.00)

    def test_short_sl_gap_up(self, engine):
        """Short SL fills at OPEN when bar gaps up past SL (worse fill)."""
        fill = engine._gap_fill_price(
            bar_open=102.00, target_price=101.00, position_side=-1, is_tp=False
        )
        assert fill == pytest.approx(102.00)


# ---------------------------------------------------------------------------
# Test: Entry and Position Tracking
# ---------------------------------------------------------------------------

class TestEntry:
    def test_buy_entry_sets_long_position(self, engine_with_fills):
        eng, fills = engine_with_fills
        # Set current bar state
        eng.on_bar_feed(_ts(0), 100.0, 100.5, 99.5, 100.0)
        trade = eng.place_bracket_order(
            symbol="CL", action="BUY", quantity=1, limit_price=100.0,
        )
        assert eng.get_position("CL") == 1
        assert trade.order.orderId >= 1000

    def test_sell_entry_sets_short_position(self, engine_with_fills):
        eng, fills = engine_with_fills
        eng.on_bar_feed(_ts(0), 100.0, 100.5, 99.5, 100.0)
        eng.place_bracket_order(
            symbol="CL", action="SELL", quantity=1, limit_price=100.0,
        )
        assert eng.get_position("CL") == -1

    def test_entry_fill_includes_slippage(self, engine_with_fills):
        eng, fills = engine_with_fills
        eng.on_bar_feed(_ts(0), 100.0, 100.5, 99.5, 100.0)
        eng.place_bracket_order(
            symbol="CL", action="BUY", quantity=1, limit_price=100.0,
        )
        # Flush deferred callback
        eng.flush_deferred_callbacks()
        assert len(fills) == 1
        assert fills[0].avg_price == pytest.approx(100.01)  # 100.0 + 0.01

    def test_deferred_callback_not_fired_immediately(self, engine_with_fills):
        eng, fills = engine_with_fills
        eng.on_bar_feed(_ts(0), 100.0, 100.5, 99.5, 100.0)
        eng.place_bracket_order(
            symbol="CL", action="BUY", quantity=1, limit_price=100.0,
        )
        # Before flush — no callback fired yet
        assert len(fills) == 0
        eng.flush_deferred_callbacks()
        assert len(fills) == 1


# ---------------------------------------------------------------------------
# Test: TP/SL Order Evaluation
# ---------------------------------------------------------------------------

class TestTPSL:
    def _enter_long(self, eng, fills, price=100.0):
        """Helper: enter a long position and place TP/SL children."""
        eng.on_bar_feed(_ts(0), price, price + 0.5, price - 0.5, price)
        trade = eng.place_bracket_order(
            symbol="CL", action="BUY", quantity=1, limit_price=price,
        )
        eng.flush_deferred_callbacks()
        fills.clear()
        # Place TP @ 102.0, SL @ 98.0
        eng.place_child_orders(
            symbol="CL",
            parent_order_id=trade.order.orderId,
            action="SELL",
            quantity=1,
            tp_price=102.0,
            sl_price=98.0,
        )
        return trade

    def _enter_short(self, eng, fills, price=100.0):
        """Helper: enter a short position and place TP/SL children."""
        eng.on_bar_feed(_ts(0), price, price + 0.5, price - 0.5, price)
        trade = eng.place_bracket_order(
            symbol="CL", action="SELL", quantity=1, limit_price=price,
        )
        eng.flush_deferred_callbacks()
        fills.clear()
        eng.place_child_orders(
            symbol="CL",
            parent_order_id=trade.order.orderId,
            action="BUY",
            quantity=1,
            tp_price=98.0,
            sl_price=102.0,
        )
        return trade

    def test_long_tp_hit(self, engine_with_fills):
        """Long TP: bar high reaches TP → fill at TP price + slippage."""
        eng, fills = engine_with_fills
        self._enter_long(eng, fills)
        # Next bar: high reaches TP
        eng.on_bar_feed(_ts(5), 100.5, 102.5, 100.0, 101.0)
        assert eng.get_position("CL") == 0
        assert len(fills) == 1
        assert fills[0].status == "Filled"
        # Gap fill: open=100.5 < TP=102.0, so fill at target 102.0
        # Then slippage (SELL): 102.0 - 0.01 = 101.99
        assert fills[0].avg_price == pytest.approx(101.99)
        # Trade recorded
        assert eng.trade_count == 1
        assert eng.completed_trades[0].exit_reason == "TP_HIT"

    def test_long_sl_hit(self, engine_with_fills):
        """Long SL: bar low reaches SL → fill at SL price + slippage."""
        eng, fills = engine_with_fills
        self._enter_long(eng, fills)
        # Next bar: low reaches SL
        eng.on_bar_feed(_ts(5), 99.5, 100.0, 97.5, 98.5)
        assert eng.get_position("CL") == 0
        assert len(fills) == 1
        # Gap fill: open=99.5 > SL=98.0, so fill at target 98.0
        # Then slippage (SELL): 98.0 - 0.01 = 97.99
        assert fills[0].avg_price == pytest.approx(97.99)
        assert eng.completed_trades[0].exit_reason == "SL_HIT"

    def test_same_bar_tp_and_sl_sl_wins(self, engine_with_fills):
        """CRITICAL: When both TP and SL trigger on same bar, SL wins."""
        eng, fills = engine_with_fills
        self._enter_long(eng, fills)
        # Wide bar that touches both TP (102.0) and SL (98.0)
        eng.on_bar_feed(_ts(5), 100.0, 103.0, 97.0, 100.5)
        assert eng.get_position("CL") == 0
        assert eng.trade_count == 1
        trade = eng.completed_trades[0]
        # SL must win (pessimistic)
        assert trade.exit_reason == "SL_HIT"
        # Gap fill: open=100.0 > SL=98.0, so fill at target 98.0
        # Then slippage (SELL): 98.0 - 0.01 = 97.99
        assert trade.exit_fill == pytest.approx(97.99)

    def test_short_tp_hit(self, engine_with_fills):
        """Short TP: bar low reaches TP → position closes."""
        eng, fills = engine_with_fills
        self._enter_short(eng, fills)
        # Next bar: low reaches short TP=98.0
        eng.on_bar_feed(_ts(5), 99.0, 99.5, 97.5, 98.5)
        assert eng.get_position("CL") == 0
        assert eng.completed_trades[0].exit_reason == "TP_HIT"

    def test_short_sl_hit(self, engine_with_fills):
        """Short SL: bar high reaches SL → position closes."""
        eng, fills = engine_with_fills
        self._enter_short(eng, fills)
        # Next bar: high reaches short SL=102.0
        eng.on_bar_feed(_ts(5), 101.0, 102.5, 100.5, 101.5)
        assert eng.get_position("CL") == 0
        assert eng.completed_trades[0].exit_reason == "SL_HIT"

    def test_no_fill_when_bar_doesnt_reach(self, engine_with_fills):
        """Bar doesn't reach TP or SL → position stays open."""
        eng, fills = engine_with_fills
        self._enter_long(eng, fills)
        eng.on_bar_feed(_ts(5), 100.0, 101.5, 99.0, 100.5)
        assert eng.get_position("CL") == 1
        assert len(fills) == 0
        assert eng.trade_count == 0


# ---------------------------------------------------------------------------
# Test: Gap Fill on TP/SL
# ---------------------------------------------------------------------------

class TestTPSLGapFill:
    def test_long_tp_gap_fill_at_open(self, engine_with_fills):
        """Long TP with gap up past target → fill at bar open."""
        eng, fills = engine_with_fills
        eng.on_bar_feed(_ts(0), 100.0, 100.5, 99.5, 100.0)
        trade = eng.place_bracket_order(
            symbol="CL", action="BUY", quantity=1, limit_price=100.0,
        )
        eng.flush_deferred_callbacks()
        fills.clear()
        eng.place_child_orders("CL", trade.order.orderId, "SELL", 1, 102.0, 98.0)
        # Bar gaps up PAST TP: open=103.0 > TP=102.0
        eng.on_bar_feed(_ts(5), 103.0, 104.0, 102.5, 103.5)
        assert eng.trade_count == 1
        # Gap fill at open (103.0), then slippage: 103.0 - 0.01 = 102.99
        assert eng.completed_trades[0].exit_fill == pytest.approx(102.99)

    def test_long_sl_gap_fill_at_open(self, engine_with_fills):
        """Long SL with gap down past target → fill at bar open."""
        eng, fills = engine_with_fills
        eng.on_bar_feed(_ts(0), 100.0, 100.5, 99.5, 100.0)
        trade = eng.place_bracket_order(
            symbol="CL", action="BUY", quantity=1, limit_price=100.0,
        )
        eng.flush_deferred_callbacks()
        fills.clear()
        eng.place_child_orders("CL", trade.order.orderId, "SELL", 1, 102.0, 98.0)
        # Bar gaps down PAST SL: open=97.0 < SL=98.0
        eng.on_bar_feed(_ts(5), 97.0, 97.5, 96.5, 97.0)
        assert eng.trade_count == 1
        # Gap fill at open (97.0), then slippage: 97.0 - 0.01 = 96.99
        assert eng.completed_trades[0].exit_fill == pytest.approx(96.99)


# ---------------------------------------------------------------------------
# Test: PnL Calculation
# ---------------------------------------------------------------------------

class TestPnL:
    def test_long_win_pnl(self, engine_with_fills):
        """Long trade wins: PnL = (exit_fill - entry_fill) * 1000 - commission."""
        eng, fills = engine_with_fills
        eng.on_bar_feed(_ts(0), 100.0, 100.5, 99.5, 100.0)
        trade = eng.place_bracket_order(
            symbol="CL", action="BUY", quantity=1, limit_price=100.0,
        )
        eng.flush_deferred_callbacks()
        fills.clear()
        eng.place_child_orders("CL", trade.order.orderId, "SELL", 1, 102.0, 98.0)
        # TP hit
        eng.on_bar_feed(_ts(5), 100.5, 102.5, 100.0, 101.0)

        t = eng.completed_trades[0]
        # Entry: 100.0 + 0.01 = 100.01 (BUY slippage)
        assert t.entry_fill == pytest.approx(100.01)
        # Exit: 102.0 - 0.01 = 101.99 (SELL slippage, target fill)
        assert t.exit_fill == pytest.approx(101.99)
        # Gross: (101.99 - 100.01) * 1000 = 1980.0
        assert t.gross_pnl_dollars == pytest.approx(1980.0)
        # Commission: 2 * 2.50 = 5.00
        assert t.commission_dollars == pytest.approx(5.00)
        # Net: 1980.0 - 5.0 = 1975.0
        assert t.net_pnl_dollars == pytest.approx(1975.0)

    def test_long_loss_pnl(self, engine_with_fills):
        """Long trade loses: PnL should be negative."""
        eng, fills = engine_with_fills
        eng.on_bar_feed(_ts(0), 100.0, 100.5, 99.5, 100.0)
        trade = eng.place_bracket_order(
            symbol="CL", action="BUY", quantity=1, limit_price=100.0,
        )
        eng.flush_deferred_callbacks()
        fills.clear()
        eng.place_child_orders("CL", trade.order.orderId, "SELL", 1, 102.0, 98.0)
        # SL hit
        eng.on_bar_feed(_ts(5), 99.5, 100.0, 97.5, 98.5)

        t = eng.completed_trades[0]
        # Entry: 100.01, Exit: 97.99
        # Gross: (97.99 - 100.01) * 1000 = -2020.0
        assert t.gross_pnl_dollars == pytest.approx(-2020.0)
        # Net: -2020 - 5 = -2025.0
        assert t.net_pnl_dollars == pytest.approx(-2025.0)


# ---------------------------------------------------------------------------
# Test: Close Position (Time Barrier)
# ---------------------------------------------------------------------------

class TestClosePosition:
    def test_close_position_resets_flat(self, engine_with_fills):
        eng, fills = engine_with_fills
        eng.on_bar_feed(_ts(0), 100.0, 100.5, 99.5, 100.0)
        eng.place_bracket_order(
            symbol="CL", action="BUY", quantity=1, limit_price=100.0,
        )
        eng.flush_deferred_callbacks()
        assert eng.get_position("CL") == 1
        eng.close_position("CL", "market", 100.5)
        assert eng.get_position("CL") == 0
        assert eng.trade_count == 1
        assert eng.completed_trades[0].exit_reason == "TIME_BARRIER"

    def test_close_position_cancels_resting_orders(self, engine_with_fills):
        eng, fills = engine_with_fills
        eng.on_bar_feed(_ts(0), 100.0, 100.5, 99.5, 100.0)
        trade = eng.place_bracket_order(
            symbol="CL", action="BUY", quantity=1, limit_price=100.0,
        )
        eng.flush_deferred_callbacks()
        eng.place_child_orders("CL", trade.order.orderId, "SELL", 1, 102.0, 98.0)
        assert len(eng._resting_orders) == 2  # TP + SL
        eng.close_position("CL", "market", 100.5)
        assert len(eng._resting_orders) == 0


# ---------------------------------------------------------------------------
# Test: Cancel Orders
# ---------------------------------------------------------------------------

class TestCancelOrders:
    def test_cancel_clears_all(self, engine_with_fills):
        eng, fills = engine_with_fills
        eng.on_bar_feed(_ts(0), 100.0, 100.5, 99.5, 100.0)
        trade = eng.place_bracket_order(
            symbol="CL", action="BUY", quantity=1, limit_price=100.0,
        )
        eng.flush_deferred_callbacks()
        eng.place_child_orders("CL", trade.order.orderId, "SELL", 1, 102.0, 98.0)
        cancelled = eng.cancel_open_orders("CL")
        assert cancelled == 2
        assert len(eng._resting_orders) == 0


# ---------------------------------------------------------------------------
# Test: Ledger Export
# ---------------------------------------------------------------------------

class TestLedgerExport:
    def test_export_columns(self, engine_with_fills):
        """Exported ledger has all required columns."""
        eng, fills = engine_with_fills
        eng.on_bar_feed(_ts(0), 100.0, 100.5, 99.5, 100.0)
        trade = eng.place_bracket_order(
            symbol="CL", action="BUY", quantity=1, limit_price=100.0,
        )
        eng.flush_deferred_callbacks()
        eng.place_child_orders("CL", trade.order.orderId, "SELL", 1, 102.0, 98.0)
        eng.on_bar_feed(_ts(5), 100.5, 102.5, 100.0, 101.0)

        df = eng.export_ledger()
        assert not df.empty
        required_cols = {
            "entry_time", "signal_side", "entry_price", "entry_fill",
            "exit_time", "exit_price", "exit_fill", "exit_reason",
            "duration_bars", "lots", "gross_pnl_dollars",
            "commission_dollars", "net_pnl_dollars",
        }
        assert required_cols.issubset(set(df.columns))

    def test_export_empty_when_no_trades(self, engine):
        df = engine.export_ledger()
        assert df.empty


# ---------------------------------------------------------------------------
# Test: Tiered TP Orders
# ---------------------------------------------------------------------------

class TestTieredTP:
    def test_tiered_tp_creates_multiple_resting_orders(self, engine_with_fills):
        """Tiered TP: list of (lots, price) creates multiple TP resting orders."""
        eng, fills = engine_with_fills
        eng.on_bar_feed(_ts(0), 100.0, 100.5, 99.5, 100.0)
        trade = eng.place_bracket_order(
            symbol="CL", action="BUY", quantity=2, limit_price=100.0,
        )
        eng.flush_deferred_callbacks()
        eng.place_child_orders(
            "CL", trade.order.orderId, "SELL", 2,
            tp_price=[(1, 101.0), (1, 102.0)],
            sl_price=98.0,
        )
        # 2 TP + 1 SL = 3 resting orders
        assert len(eng._resting_orders) == 3


# ---------------------------------------------------------------------------
# Test: Multiple Trades
# ---------------------------------------------------------------------------

class TestMultipleTrades:
    def test_sequential_trades_accumulate(self, engine_with_fills):
        """Multiple trades record independently in the ledger."""
        eng, fills = engine_with_fills

        # Trade 1: Long, TP hit
        eng.on_bar_feed(_ts(0), 100.0, 100.5, 99.5, 100.0)
        t1 = eng.place_bracket_order("CL", "BUY", 1, limit_price=100.0)
        eng.flush_deferred_callbacks()
        fills.clear()
        eng.place_child_orders("CL", t1.order.orderId, "SELL", 1, 102.0, 98.0)
        eng.on_bar_feed(_ts(5), 101.0, 102.5, 100.5, 102.0)
        assert eng.trade_count == 1

        # Trade 2: Short, SL hit
        eng.on_bar_feed(_ts(10), 101.0, 101.5, 100.5, 101.0)
        t2 = eng.place_bracket_order("CL", "SELL", 1, limit_price=101.0)
        eng.flush_deferred_callbacks()
        fills.clear()
        eng.place_child_orders("CL", t2.order.orderId, "BUY", 1, 99.0, 103.0)
        eng.on_bar_feed(_ts(15), 102.0, 103.5, 101.5, 103.0)
        assert eng.trade_count == 2

        # Verify each trade is correct
        assert eng.completed_trades[0].exit_reason == "TP_HIT"
        assert eng.completed_trades[0].signal_side == "LONG"
        assert eng.completed_trades[1].exit_reason == "SL_HIT"
        assert eng.completed_trades[1].signal_side == "SHORT"

        # Total PnL should be sum
        assert eng.total_pnl == pytest.approx(
            eng.completed_trades[0].net_pnl_dollars
            + eng.completed_trades[1].net_pnl_dollars
        )
