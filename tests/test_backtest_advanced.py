"""Tests for CLAdvancedExecutionBacktester FSM trade management.

Uses synthetic OHLCV data to validate every FSM transition:
- TP hit, SL hit, trailing stop to breakeven, time-barrier exit
- Cooldown rejection and expiry
- Gap-aware fills and slippage
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from agent.backtest_cl_advanced import (
    BacktestResult,
    CLAdvancedExecutionBacktester,
    ExitReason,
    TradeRecord,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ohlcv(
    n: int,
    base_price: float = 65.0,
    *,
    prices: list[float] | None = None,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    opens: list[float] | None = None,
) -> pd.DataFrame:
    """Create synthetic OHLCV data for testing.

    If `prices` is provided, it overrides Open/High/Low/Close with those
    values (High = price+0.01, Low = price-0.01 by default).
    If `highs` or `lows` are provided they override the H/L.
    """
    if prices is not None:
        n = len(prices)
        close = prices
        open_ = opens if opens else prices
        high = highs if highs else [p + 0.01 for p in prices]
        low = lows if lows else [p - 0.01 for p in prices]
    else:
        close = [base_price] * n
        open_ = [base_price] * n
        high = [base_price + 0.01] * n
        low = [base_price - 0.01] * n

    idx = pd.date_range("2026-01-01", periods=n, freq="5min")
    return pd.DataFrame(
        {
            "Open": open_,
            "High": high,
            "Low": low,
            "Close": close,
            "Volume": [100.0] * n,
        },
        index=idx,
    )


def _make_signal(ohlcv: pd.DataFrame, bar_idx: int, side: int = 1) -> pd.DataFrame:
    """Create a signals DataFrame with a single signal at the given bar index."""
    dt = ohlcv.index[bar_idx]
    return pd.DataFrame({"side": [side]}, index=[dt])


def _bt(**kwargs) -> CLAdvancedExecutionBacktester:
    """Create a backtester with test-friendly defaults."""
    defaults = {
        "tp_atr_mult": 2.0,
        "sl_atr_mult": 1.0,
        "max_horizon": 288,
        "cooldown_bars": 10,
        "trailing_atr_mult": 1.0,
        "atr_period": 14,
        "commission_per_side": 0.0,  # Zero commission for cleaner P&L assertions
        "slippage_per_side": 0.0,  # Zero slippage by default
        "contract_multiplier": 1000.0,
    }
    defaults.update(kwargs)
    return CLAdvancedExecutionBacktester(**defaults)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTPHit:
    """Take-profit barrier triggers exit at correct price."""

    def test_tp_hit_exits_correctly(self) -> None:
        # ATR will stabilize by bar ~20. Signal at bar 20, entry price = 65.0.
        # ATR ≈ 0.02 (high-low = 0.02 for each bar).
        # TP = 65.0 + 2.0 * 0.02 = 65.04
        # At bar 25, push High above TP.
        n = 40
        prices = [65.0] * n
        highs = [65.01] * n
        lows = [64.99] * n

        # Bar 25: High spikes to 65.05 (above TP at 65.04)
        highs[25] = 65.05

        ohlcv = _make_ohlcv(n, prices=prices, highs=highs, lows=lows)
        signals = _make_signal(ohlcv, bar_idx=20)

        bt = _bt()
        result = bt.run(signals, ohlcv)

        assert result.trade_count == 1
        trade = result.trades[0]
        assert trade.exit_reason == ExitReason.TP
        assert trade.duration_bars == 5  # bars 21..25


class TestSLHit:
    """Stop-loss barrier triggers exit at correct price."""

    def test_sl_hit_exits_correctly(self) -> None:
        # Entry at 65.0, ATR ≈ 0.02, SL = 65.0 - 1.0 * 0.02 = 64.98
        n = 40
        prices = [65.0] * n
        highs = [65.01] * n
        lows = [64.99] * n

        # Bar 25: Low drops to 64.97 (below SL at 64.98)
        lows[25] = 64.97

        ohlcv = _make_ohlcv(n, prices=prices, highs=highs, lows=lows)
        signals = _make_signal(ohlcv, bar_idx=20)

        bt = _bt()
        result = bt.run(signals, ohlcv)

        assert result.trade_count == 1
        trade = result.trades[0]
        assert trade.exit_reason == ExitReason.SL


class TestTimeBarrier:
    """Position closes after max_horizon bars."""

    def test_time_barrier_forces_exit_at_289(self) -> None:
        # Use a short horizon for test speed
        horizon = 10
        n = 40
        prices = [65.0] * n
        highs = [65.01] * n
        lows = [64.99] * n

        ohlcv = _make_ohlcv(n, prices=prices, highs=highs, lows=lows)
        signals = _make_signal(ohlcv, bar_idx=20)

        bt = _bt(max_horizon=horizon)
        result = bt.run(signals, ohlcv)

        assert result.trade_count == 1
        trade = result.trades[0]
        assert trade.exit_reason == ExitReason.TIME_BARRIER
        assert trade.duration_bars == horizon + 1  # Exits on bar horizon+1


class TestTrailingStop:
    """Trailing stop moves SL to breakeven after +1×ATR in favor."""

    def test_trailing_stop_moves_to_breakeven(self) -> None:
        # Entry at 65.0, ATR ≈ 0.02.
        # Trailing triggers when high >= 65.0 + 1.0*0.02 = 65.02
        # Then SL moves from 64.98 to 65.0 (breakeven).
        # Finally, price drops to 65.0 → exits at breakeven.
        n = 40
        prices = [65.0] * n
        highs = [65.01] * n
        lows = [64.99] * n

        # Bar 23: High reaches 65.03 → triggers trailing stop to breakeven
        highs[23] = 65.03

        # Bar 26: Low touches 65.0 → breakeven stop fires
        lows[26] = 65.00
        prices[26] = 65.00

        ohlcv = _make_ohlcv(n, prices=prices, highs=highs, lows=lows)
        signals = _make_signal(ohlcv, bar_idx=20)

        bt = _bt()
        result = bt.run(signals, ohlcv)

        assert result.trade_count == 1
        trade = result.trades[0]
        assert trade.exit_reason == ExitReason.TRAILING_BE

    def test_trailing_stop_breakeven_exit_pnl_near_zero(self) -> None:
        """Breakeven exit should result in ~zero P&L (minus any friction)."""
        n = 40
        prices = [65.0] * n
        highs = [65.01] * n
        lows = [64.99] * n

        highs[23] = 65.03  # Trigger trailing
        lows[26] = 65.00  # Hit breakeven

        ohlcv = _make_ohlcv(n, prices=prices, highs=highs, lows=lows)
        signals = _make_signal(ohlcv, bar_idx=20)

        bt = _bt(commission_per_side=0.0, slippage_per_side=0.0)
        result = bt.run(signals, ohlcv)

        assert result.trade_count == 1
        trade = result.trades[0]
        # Entry = 65.0, exit = 65.0 (breakeven) → P&L ≈ 0
        assert abs(trade.net_pnl_dollars) < 1.0  # Allow tiny float imprecision


class TestCooldown:
    """Post-stop-out cooldown rejects signals and expires correctly."""

    def test_cooldown_rejects_signals(self) -> None:
        # Signal at bar 20 → SL hit at bar 25 → cooldown 10 bars.
        # Signal at bar 30 (during cooldown) should be rejected.
        n = 50
        prices = [65.0] * n
        highs = [65.01] * n
        lows = [64.99] * n

        # Bar 25: SL hit
        lows[25] = 64.97

        ohlcv = _make_ohlcv(n, prices=prices, highs=highs, lows=lows)

        # Two signals: bar 20 (accepted) and bar 30 (should be rejected)
        dt1 = ohlcv.index[20]
        dt2 = ohlcv.index[30]
        signals = pd.DataFrame({"side": [1, 1]}, index=[dt1, dt2])

        bt = _bt(cooldown_bars=10)
        result = bt.run(signals, ohlcv)

        # Only 1 trade — the second signal was during cooldown
        assert result.trade_count == 1

    def test_cooldown_expires_after_n_bars(self) -> None:
        # Signal at bar 20 → SL hit at bar 23 → cooldown 5 bars.
        # Cooldown covers bars 24..28 (5 bars). Bar 29 → FLAT.
        # Signal at bar 35 (after cooldown) should be accepted.
        n = 50
        prices = [65.0] * n
        highs = [65.01] * n
        lows = [64.99] * n

        # Bar 23: SL hit
        lows[23] = 64.97

        ohlcv = _make_ohlcv(n, prices=prices, highs=highs, lows=lows)

        # Two signals: bar 20 (accepted) and bar 35 (after cooldown, accepted)
        dt1 = ohlcv.index[20]
        dt2 = ohlcv.index[35]
        signals = pd.DataFrame({"side": [1, 1]}, index=[dt1, dt2])

        bt = _bt(cooldown_bars=5, max_horizon=5)
        result = bt.run(signals, ohlcv)

        # Should have 2 trades — cooldown expired before second signal
        assert result.trade_count == 2


class TestGapFill:
    """Gap past the stop should fill at the bar's Open, not ideal SL."""

    def test_gap_fills_at_open_not_stop(self) -> None:
        # Entry at 65.0, ATR ≈ 0.02, SL = 64.98.
        # Bar 25: gaps down — Open=64.90, Low=64.85 (well below SL).
        # Should fill at 64.90 (the Open), not 64.98 (the ideal SL).
        n = 40
        prices = [65.0] * n
        opens = [65.0] * n
        highs = [65.01] * n
        lows = [64.99] * n

        # Bar 25: gap down
        opens[25] = 64.90
        highs[25] = 64.91
        lows[25] = 64.85
        prices[25] = 64.86

        ohlcv = _make_ohlcv(n, prices=prices, opens=opens, highs=highs, lows=lows)
        signals = _make_signal(ohlcv, bar_idx=20)

        bt = _bt(slippage_per_side=0.0)
        result = bt.run(signals, ohlcv)

        assert result.trade_count == 1
        trade = result.trades[0]
        assert trade.exit_reason == ExitReason.SL
        # Exit price should be the Open (64.90), not the SL level (64.98)
        assert trade.exit_price == pytest.approx(64.90, abs=0.01)


class TestSlippage:
    """Slippage penalty applies to both entry and exit fills."""

    def test_slippage_applied_both_sides(self) -> None:
        n = 40
        prices = [65.0] * n
        highs = [65.01] * n
        lows = [64.99] * n

        # TP hit at bar 25
        highs[25] = 65.05

        ohlcv = _make_ohlcv(n, prices=prices, highs=highs, lows=lows)
        signals = _make_signal(ohlcv, bar_idx=20)

        slippage = 0.03
        bt = _bt(slippage_per_side=slippage)
        result = bt.run(signals, ohlcv)

        assert result.trade_count == 1
        trade = result.trades[0]

        # Entry: Buy → price + slippage
        assert trade.entry_fill == pytest.approx(65.0 + slippage, abs=0.001)
        # Exit: Sell → price - slippage
        expected_exit_price = trade.exit_price  # TP price
        assert trade.exit_fill == pytest.approx(expected_exit_price - slippage, abs=0.001)


class TestExitDistribution:
    """BacktestResult.exit_distribution returns correct counts/percentages."""

    def test_exit_distribution_counts(self) -> None:
        # Manually build a result with known exit reasons
        trades = [
            TradeRecord(
                entry_dt=pd.Timestamp("2026-01-01"),
                exit_dt=pd.Timestamp("2026-01-02"),
                entry_price=65.0,
                exit_price=65.04,
                entry_fill=65.0,
                exit_fill=65.04,
                side=1,
                atr_at_entry=0.02,
                exit_reason=ExitReason.TP,
                duration_bars=5,
                gross_pnl_dollars=40.0,
                commission_dollars=5.0,
                net_pnl_dollars=35.0,
            ),
            TradeRecord(
                entry_dt=pd.Timestamp("2026-01-03"),
                exit_dt=pd.Timestamp("2026-01-04"),
                entry_price=65.0,
                exit_price=64.98,
                entry_fill=65.0,
                exit_fill=64.98,
                side=1,
                atr_at_entry=0.02,
                exit_reason=ExitReason.SL,
                duration_bars=3,
                gross_pnl_dollars=-20.0,
                commission_dollars=5.0,
                net_pnl_dollars=-25.0,
            ),
        ]
        result = BacktestResult(trades=trades)

        dist = result.exit_distribution
        assert dist["TP"]["count"] == 1
        assert dist["TP"]["pct"] == pytest.approx(50.0)
        assert dist["SL"]["count"] == 1
        assert dist["SL"]["pct"] == pytest.approx(50.0)
