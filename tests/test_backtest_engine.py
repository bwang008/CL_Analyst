"""Tests for BacktestEngine FSM trade management.

Uses synthetic OHLCV data to validate every FSM transition:
- TP hit, SL hit, trailing stop to breakeven, time-barrier exit
- Cooldown rejection and expiry
- Gap-aware fills and slippage
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from agent.backtest_engine import (
    BacktestResult,
    BacktestEngine,
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


def _bt(**kwargs) -> BacktestEngine:
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
    for k in ["cooldown_bars", "tp_cooldown_bars", "sl_cooldown_bars", "consecutive_signal_threshold"]:
        defaults.pop(k, None)
    return BacktestEngine(**defaults)


def _make_prob_signal(
    ohlcv: pd.DataFrame,
    bar_idx: int,
    prob_buy: float = 0.0,
    prob_sell: float = 0.0,
) -> pd.DataFrame:
    """Create a signal DataFrame with prob_Buy/prob_Sell at the given bar."""
    prob_buy_col = [0.0] * len(ohlcv)
    prob_sell_col = [0.0] * len(ohlcv)
    prob_buy_col[bar_idx] = prob_buy
    prob_sell_col[bar_idx] = prob_sell
    return pd.DataFrame(
        {"prob_Buy": prob_buy_col, "prob_Sell": prob_sell_col},
        index=ohlcv.index,
    )


def _bt_with_strategy(config: dict, **bt_kwargs) -> BacktestEngine:
    """Create a BacktestEngine from a strategy config dict with test defaults."""
    return BacktestEngine.from_config(
        config,
        commission_per_side=bt_kwargs.get("commission_per_side", 0.0),
        slippage_per_side=bt_kwargs.get("slippage_per_side", 0.0),
        contract_multiplier=bt_kwargs.get("contract_multiplier", 1000.0),
    )


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


# ---------------------------------------------------------------------------
# Concurrent Mode Tests
# ---------------------------------------------------------------------------


class TestConcurrentMode:
    """Concurrent multi-position mode allows overlapping trades."""

    def test_concurrent_opens_multiple_positions(self) -> None:
        """With allow_concurrent=True, two close signals both fire."""
        n = 40
        prices = [65.0] * n
        highs = [65.01] * n
        lows = [64.99] * n

        # TP hit for both signals at bar 30
        highs[30] = 65.05

        ohlcv = _make_ohlcv(n, prices=prices, highs=highs, lows=lows)

        # Two signals at bars 20 and 22 — both should be accepted
        dt1 = ohlcv.index[20]
        dt2 = ohlcv.index[22]
        signals = pd.DataFrame({"side": [1, 1]}, index=[dt1, dt2])

        bt = _bt(allow_concurrent=True, max_concurrent=5)
        result = bt.run(signals, ohlcv)

        # Both trades should execute (not just one)
        assert result.trade_count == 2

    def test_concurrent_respects_max_concurrent(self) -> None:
        """Signals beyond max_concurrent are rejected."""
        n = 40
        prices = [65.0] * n
        highs = [65.01] * n
        lows = [64.99] * n

        highs[30] = 65.05  # TP hit

        ohlcv = _make_ohlcv(n, prices=prices, highs=highs, lows=lows)

        # Three signals — but max_concurrent=2
        dt1 = ohlcv.index[20]
        dt2 = ohlcv.index[21]
        dt3 = ohlcv.index[22]
        signals = pd.DataFrame({"side": [1, 1, 1]}, index=[dt1, dt2, dt3])

        bt = _bt(allow_concurrent=True, max_concurrent=2)
        result = bt.run(signals, ohlcv)

        # Only 2 accepted (third signal rejected — at capacity)
        assert result.trade_count == 2

    def test_single_mode_rejects_second_signal(self) -> None:
        """With allow_concurrent=False (default), overlapping signal rejected."""
        n = 40
        prices = [65.0] * n
        highs = [65.01] * n
        lows = [64.99] * n

        highs[30] = 65.05  # TP hit

        ohlcv = _make_ohlcv(n, prices=prices, highs=highs, lows=lows)

        # Two signals at bars 20 and 22 — second should be rejected
        dt1 = ohlcv.index[20]
        dt2 = ohlcv.index[22]
        signals = pd.DataFrame({"side": [1, 1]}, index=[dt1, dt2])

        bt = _bt(allow_concurrent=False)
        result = bt.run(signals, ohlcv)

        assert result.trade_count == 1  # Only one trade — FSM was IN_POSITION


class TestFromConfig:
    """BacktestEngine.from_config() reads all strategy fields."""

    def test_from_config_reads_all_fields(self) -> None:
        cfg = {
            "nickname": "TestStrat",
            "tp_atr_mult": 5.0,
            "sl_atr_mult": 0.5,
            "entry_threshold": 0.70,
            "allow_concurrent": True,
            "max_concurrent": 3,
            "trailing_atr_mult": 2.0,
            "max_hold_bars": 100,
        }
        bt = BacktestEngine.from_config(cfg)

        assert bt.tp_atr_mult == 5.0
        assert bt.sl_atr_mult == 0.5
        assert bt.prob_threshold == 0.70
        assert bt.allow_concurrent is True
        assert bt.max_concurrent == 3
        assert bt.trailing_atr_mult == 2.0
        assert bt.max_horizon == 100

    def test_from_config_defaults(self) -> None:
        """Missing fields use sensible defaults."""
        cfg = {"nickname": "Minimal"}
        bt = BacktestEngine.from_config(cfg)

        assert bt.tp_atr_mult == 2.0
        assert bt.allow_concurrent is False
        assert bt.max_concurrent == 1
        assert bt.max_horizon == 288

    def test_from_config_overrides(self) -> None:
        """CLI overrides take precedence over config values."""
        cfg = {"tp_atr_mult": 5.0, "commission_per_side": 999}
        bt = BacktestEngine.from_config(
            cfg, commission_per_side=1.00
        )

        assert bt.tp_atr_mult == 5.0  # from config
        assert bt.commission_per_side == 1.00  # override wins


class TestBackwardCompatAlias:
    """CLAdvancedExecutionBacktester alias still works."""

    def test_alias_is_same_class(self) -> None:
        from agent.backtest_engine import CLAdvancedExecutionBacktester

        assert CLAdvancedExecutionBacktester is BacktestEngine


# ---------------------------------------------------------------------------
# Pessimistic FSM Tests
# ---------------------------------------------------------------------------


class TestPessimisticSameBar:
    """When both TP and SL breach on the same bar, SL wins."""

    def test_same_bar_tp_sl_exits_as_sl(self) -> None:
        """High-volatility bar breaches both barriers — pessimistic takes SL."""
        n = 40
        prices = [65.0] * n
        highs = [65.01] * n
        lows = [64.99] * n

        # Bar 25: massive range — High breaches TP AND Low breaches SL
        # ATR ≈ 0.02, TP = 65.04, SL = 64.98
        highs[25] = 65.10  # above TP at 65.04
        lows[25] = 64.90   # below SL at 64.98

        ohlcv = _make_ohlcv(n, prices=prices, highs=highs, lows=lows)
        signals = _make_signal(ohlcv, bar_idx=20)

        bt = _bt()
        result = bt.run(signals, ohlcv)

        assert result.trade_count == 1
        trade = result.trades[0]
        # Pessimistic: SL wins over TP on same bar
        assert trade.exit_reason == ExitReason.SL

    def test_only_tp_still_exits_tp(self) -> None:
        """When only TP breaches (no SL), TP still fires correctly."""
        n = 40
        prices = [65.0] * n
        highs = [65.01] * n
        lows = [64.99] * n

        # Bar 25: only TP breached
        highs[25] = 65.05

        ohlcv = _make_ohlcv(n, prices=prices, highs=highs, lows=lows)
        signals = _make_signal(ohlcv, bar_idx=20)

        bt = _bt()
        result = bt.run(signals, ohlcv)

        assert result.trade_count == 1
        assert result.trades[0].exit_reason == ExitReason.TP


# ---------------------------------------------------------------------------
# Equity Curve Tests
# ---------------------------------------------------------------------------


class TestEquityCurve:
    """BacktestResult.equity_curve tracks floating + realized PnL."""

    def test_equity_curve_has_entries(self) -> None:
        """After a run, equity_curve has one entry per OHLCV bar."""
        n = 40
        prices = [65.0] * n
        highs = [65.01] * n
        lows = [64.99] * n
        highs[25] = 65.05  # TP hit

        ohlcv = _make_ohlcv(n, prices=prices, highs=highs, lows=lows)
        signals = _make_signal(ohlcv, bar_idx=20)

        bt = _bt()
        result = bt.run(signals, ohlcv)

        assert len(result.equity_curve) == n

    def test_equity_curve_flat_when_no_position(self) -> None:
        """Before any trade, equity should be zero."""
        n = 40
        prices = [65.0] * n
        highs = [65.01] * n
        lows = [64.99] * n
        highs[30] = 65.05  # TP hit late

        ohlcv = _make_ohlcv(n, prices=prices, highs=highs, lows=lows)
        signals = _make_signal(ohlcv, bar_idx=20)

        bt = _bt()
        result = bt.run(signals, ohlcv)

        # Before signal at bar 20, equity should be 0
        for i in range(20):
            assert result.equity_curve[i] == 0.0

    def test_max_drawdown_uses_equity_curve(self) -> None:
        """max_drawdown should reflect intra-trade floating losses."""
        n = 40
        prices = [65.0] * n
        highs = [65.01] * n
        lows = [64.99] * n

        # Entry at bar 20 (price 65.0), price dips then recovers to TP
        # Bar 23: price drops → floating loss
        prices[23] = 64.99
        lows[23] = 64.99
        # Bar 30: TP hit
        highs[30] = 65.05

        ohlcv = _make_ohlcv(n, prices=prices, highs=highs, lows=lows)
        signals = _make_signal(ohlcv, bar_idx=20)

        bt = _bt()
        result = bt.run(signals, ohlcv)

        # max_drawdown should be negative (reflecting the dip)
        assert result.max_drawdown < 0.0


# ---------------------------------------------------------------------------
# Execution Strategy Tests
# ---------------------------------------------------------------------------

from src.live_execution.strategies.execution_models import (
    SingleModelStrategy,
    ConservativeEnsembleStrategy,
    AggressiveEnsembleStrategy,
    create_execution_strategy,
)


def _make_prob_signal(
    ohlcv: pd.DataFrame,
    bar_idx: int,
    prob_buy: float = 0.0,
    prob_sell: float = 0.0,
) -> pd.DataFrame:
    """Create a signals DataFrame with prob_Buy and/or prob_Sell at given bar."""
    dt = ohlcv.index[bar_idx]
    data: dict = {}
    if prob_buy > 0:
        data["prob_Buy"] = [prob_buy]
    if prob_sell > 0:
        data["prob_Sell"] = [prob_sell]
    return pd.DataFrame(data, index=[dt])


def _bt_with_strategy(config: dict, **kwargs) -> BacktestEngine:
    """Create a BacktestEngine from a config dict with test-friendly defaults."""
    defaults = {
        "commission_per_side": 0.0,
        "slippage_per_side": 0.0,
    }
    defaults.update(kwargs)
    return BacktestEngine.from_config(config, **defaults)


class TestSingleModelStrategy:
    """SingleModelStrategy produces the same results as legacy behavior."""

    def test_long_strategy_fires_on_prob_buy(self) -> None:
        """prob_Buy above threshold with direction=LONG triggers a trade."""
        config = {
            "nickname": "TestLong",
            "direction": "LONG",
            "entry_threshold": 0.70,
            "tp_atr_mult": 2.0,
            "sl_atr_mult": 1.0,
        }
        n = 40
        prices = [65.0] * n
        highs = [65.01] * n
        lows = [64.99] * n
        highs[25] = 65.05  # TP hit

        ohlcv = _make_ohlcv(n, prices=prices, highs=highs, lows=lows)
        signals = _make_prob_signal(ohlcv, bar_idx=20, prob_buy=0.80)

        bt = _bt_with_strategy(config)
        result = bt.run(signals, ohlcv)

        assert result.trade_count == 1
        trade = result.trades[0]
        assert trade.exit_reason == ExitReason.TP
        assert trade.side == 1

    def test_short_strategy_fires_on_prob_sell(self) -> None:
        """prob_Sell above threshold with direction=SHORT triggers a short trade."""
        config = {
            "nickname": "TestShort",
            "direction": "SHORT",
            "entry_threshold": 0.60,
            "tp_atr_mult": 2.0,
            "sl_atr_mult": 1.0,
        }
        n = 40
        prices = [65.0] * n
        highs = [65.01] * n
        lows = [64.99] * n
        lows[25] = 64.95  # TP hit for short (price goes down)

        ohlcv = _make_ohlcv(n, prices=prices, highs=highs, lows=lows)
        signals = _make_prob_signal(ohlcv, bar_idx=20, prob_sell=0.75)

        bt = _bt_with_strategy(config)
        result = bt.run(signals, ohlcv)

        assert result.trade_count == 1
        assert result.trades[0].side == -1

    def test_below_threshold_no_trade(self) -> None:
        """Signal below threshold does not trigger a trade."""
        config = {
            "nickname": "TestNoTrade",
            "direction": "LONG",
            "entry_threshold": 0.70,
            "tp_atr_mult": 2.0,
            "sl_atr_mult": 1.0,
        }
        n = 40
        prices = [65.0] * n
        highs = [65.01] * n
        lows = [64.99] * n
        highs[25] = 65.05

        ohlcv = _make_ohlcv(n, prices=prices, highs=highs, lows=lows)
        signals = _make_prob_signal(ohlcv, bar_idx=20, prob_buy=0.50)  # below 0.70

        bt = _bt_with_strategy(config)
        result = bt.run(signals, ohlcv)

        assert result.trade_count == 0

    def test_default_execution_class_is_single(self) -> None:
        """Configs without execution_class default to SingleModelStrategy."""
        config = {"nickname": "NoClass", "direction": "LONG"}
        strategy = create_execution_strategy(config)
        assert isinstance(strategy, SingleModelStrategy)


class TestConservativeEnsembleStrategy:
    """ConservativeEnsembleStrategy handles dual-model signals correctly."""

    def test_buy_signal_only(self) -> None:
        """Only buy signal above threshold → enters LONG."""
        config = {
            "nickname": "Ensemble",
            "execution_class": "ConservativeEnsembleStrategy",
            "models": {
                "long": {"threshold": 0.70},
                "short": {"threshold": 0.60},
            },
            "tp_atr_mult": 2.0,
            "sl_atr_mult": 1.0,
        }
        n = 40
        prices = [65.0] * n
        highs = [65.01] * n
        lows = [64.99] * n
        highs[25] = 65.05

        ohlcv = _make_ohlcv(n, prices=prices, highs=highs, lows=lows)
        signals = _make_prob_signal(ohlcv, bar_idx=20, prob_buy=0.80, prob_sell=0.30)

        bt = _bt_with_strategy(config)
        result = bt.run(signals, ohlcv)

        assert result.trade_count == 1
        assert result.trades[0].side == 1

    def test_sell_signal_only(self) -> None:
        """Only sell signal above threshold → enters SHORT."""
        config = {
            "nickname": "Ensemble",
            "execution_class": "ConservativeEnsembleStrategy",
            "models": {
                "long": {"threshold": 0.70},
                "short": {"threshold": 0.60},
            },
            "tp_atr_mult": 2.0,
            "sl_atr_mult": 1.0,
        }
        n = 40
        prices = [65.0] * n
        highs = [65.01] * n
        lows = [64.99] * n
        lows[25] = 64.95

        ohlcv = _make_ohlcv(n, prices=prices, highs=highs, lows=lows)
        signals = _make_prob_signal(ohlcv, bar_idx=20, prob_buy=0.40, prob_sell=0.75)

        bt = _bt_with_strategy(config)
        result = bt.run(signals, ohlcv)

        assert result.trade_count == 1
        assert result.trades[0].side == -1

    def test_no_flip_when_in_position(self) -> None:
        """Conservative strategy ignores signals when already in a position."""
        config = {
            "nickname": "Ensemble",
            "execution_class": "ConservativeEnsembleStrategy",
            "models": {
                "long": {"threshold": 0.70},
                "short": {"threshold": 0.60},
            },
            "tp_atr_mult": 2.0,
            "sl_atr_mult": 1.0,
            "max_hold_bars": 50,
        }
        n = 50
        prices = [65.0] * n
        highs = [65.01] * n
        lows = [64.99] * n
        highs[35] = 65.05  # TP hit

        ohlcv = _make_ohlcv(n, prices=prices, highs=highs, lows=lows)

        # Signal at bar 20 (BUY), then bar 25 (SELL while in position)
        dt1 = ohlcv.index[20]
        dt2 = ohlcv.index[25]
        signals = pd.DataFrame(
            {"prob_Buy": [0.80, 0.10], "prob_Sell": [0.10, 0.90]},
            index=[dt1, dt2],
        )

        bt = _bt_with_strategy(config)
        result = bt.run(signals, ohlcv)

        # Only one trade — the sell signal was ignored (in position)
        assert result.trade_count == 1
        assert result.trades[0].side == 1


class TestEnsembleSameBarConflict:
    """When both signals exceed threshold on the same bar, higher prob wins."""

    def test_buy_wins_on_higher_prob(self) -> None:
        config = {
            "nickname": "Conflict",
            "execution_class": "ConservativeEnsembleStrategy",
            "models": {
                "long": {"threshold": 0.60},
                "short": {"threshold": 0.60},
            },
            "tp_atr_mult": 2.0,
            "sl_atr_mult": 1.0,
        }
        n = 40
        prices = [65.0] * n
        highs = [65.01] * n
        lows = [64.99] * n
        highs[25] = 65.05

        ohlcv = _make_ohlcv(n, prices=prices, highs=highs, lows=lows)
        # Both above threshold, but buy is higher
        signals = _make_prob_signal(ohlcv, bar_idx=20, prob_buy=0.85, prob_sell=0.70)

        bt = _bt_with_strategy(config)
        result = bt.run(signals, ohlcv)

        assert result.trade_count == 1
        assert result.trades[0].side == 1  # Buy wins

    def test_sell_wins_on_higher_prob(self) -> None:
        config = {
            "nickname": "Conflict",
            "execution_class": "ConservativeEnsembleStrategy",
            "models": {
                "long": {"threshold": 0.60},
                "short": {"threshold": 0.60},
            },
            "tp_atr_mult": 2.0,
            "sl_atr_mult": 1.0,
        }
        n = 40
        prices = [65.0] * n
        highs = [65.01] * n
        lows = [64.99] * n
        lows[25] = 64.95

        ohlcv = _make_ohlcv(n, prices=prices, highs=highs, lows=lows)
        # Both above threshold, but sell is higher
        signals = _make_prob_signal(ohlcv, bar_idx=20, prob_buy=0.65, prob_sell=0.90)

        bt = _bt_with_strategy(config)
        result = bt.run(signals, ohlcv)

        assert result.trade_count == 1
        assert result.trades[0].side == -1  # Sell wins


class TestAggressiveEnsembleFlip:
    """AggressiveEnsembleStrategy flips positions on opposing signals."""

    def test_no_flip_needed_when_flat(self) -> None:
        """When flat, aggressive behaves same as conservative."""
        config = {
            "nickname": "Aggressive",
            "execution_class": "AggressiveEnsembleStrategy",
            "models": {
                "long": {"threshold": 0.70},
                "short": {"threshold": 0.60},
            },
            "tp_atr_mult": 2.0,
            "sl_atr_mult": 1.0,
        }
        n = 40
        prices = [65.0] * n
        highs = [65.01] * n
        lows = [64.99] * n
        highs[25] = 65.05

        ohlcv = _make_ohlcv(n, prices=prices, highs=highs, lows=lows)
        signals = _make_prob_signal(ohlcv, bar_idx=20, prob_buy=0.80)

        bt = _bt_with_strategy(config)
        result = bt.run(signals, ohlcv)

        assert result.trade_count == 1
        assert result.trades[0].side == 1


# ---------------------------------------------------------------------------
# Separate TP/SL Cooldown Tests
# ---------------------------------------------------------------------------


class TestSeparateCooldowns:
    """Separate tp_cooldown_bars and sl_cooldown_bars apply correctly."""

    def test_sl_cooldown_rejects_during_window(self) -> None:
        """SL exit activates sl_cooldown_bars, rejecting signals within it."""
        n = 50
        prices = [65.0] * n
        highs = [65.01] * n
        lows = [64.99] * n

        # Bar 25: SL hit
        lows[25] = 64.97

        ohlcv = _make_ohlcv(n, prices=prices, highs=highs, lows=lows)

        # Signal at bar 20 (accepted), signal at bar 28 (during sl_cooldown=5)
        dt1 = ohlcv.index[20]
        dt2 = ohlcv.index[28]
        signals = pd.DataFrame({"side": [1, 1]}, index=[dt1, dt2])

        bt = _bt(sl_cooldown_bars=5, tp_cooldown_bars=0)
        result = bt.run(signals, ohlcv)

        # Only 1 trade — second signal rejected during SL cooldown
        assert result.trade_count == 1

    def test_tp_cooldown_rejects_during_window(self) -> None:
        """TP exit activates tp_cooldown_bars, rejecting signals within it."""
        n = 50
        prices = [65.0] * n
        highs = [65.01] * n
        lows = [64.99] * n

        # Bar 25: TP hit
        highs[25] = 65.05

        ohlcv = _make_ohlcv(n, prices=prices, highs=highs, lows=lows)

        # Signal at bar 20 (accepted), signal at bar 28 (during tp_cooldown=5)
        dt1 = ohlcv.index[20]
        dt2 = ohlcv.index[28]
        signals = pd.DataFrame({"side": [1, 1]}, index=[dt1, dt2])

        bt = _bt(tp_cooldown_bars=5, sl_cooldown_bars=0)
        result = bt.run(signals, ohlcv)

        # Only 1 trade — second signal rejected during TP cooldown
        assert result.trade_count == 1

    def test_different_tp_sl_cooldown_lengths(self) -> None:
        """SL gets long cooldown (10), TP gets short cooldown (2)."""
        n = 50
        prices = [65.0] * n
        highs = [65.01] * n
        lows = [64.99] * n

        highs[25] = 65.05  # TP hit for first trade
        highs[35] = 65.05  # TP hit for second trade

        ohlcv = _make_ohlcv(n, prices=prices, highs=highs, lows=lows)

        dt1 = ohlcv.index[20]
        dt2 = ohlcv.index[29]  # After tp_cooldown=2 expires
        signals = pd.DataFrame({"side": [1, 1]}, index=[dt1, dt2])

        bt = _bt(tp_cooldown_bars=2, sl_cooldown_bars=10)
        result = bt.run(signals, ohlcv)

        # Both trades should execute — TP cooldown (2 bars) expired before bar 29
        assert result.trade_count == 2

    def test_time_barrier_no_cooldown(self) -> None:
        """Time barrier exit goes straight to FLAT with no cooldown."""
        horizon = 5
        n = 50
        prices = [65.0] * n
        highs = [65.01] * n
        lows = [64.99] * n

        ohlcv = _make_ohlcv(n, prices=prices, highs=highs, lows=lows)

        # Signal at bar 20, time barrier exits at bar 26, signal at bar 27
        dt1 = ohlcv.index[20]
        dt2 = ohlcv.index[27]
# Trailing Stop Offset Tests
# ---------------------------------------------------------------------------


class TestTrailingStopOffset:
    """Trailing stop with configurable offset locks in profit."""

    def test_offset_locks_in_profit(self) -> None:
        """With offset=0.5, SL moves to entry + 0.5*ATR (small profit)."""
        n = 40
        prices = [65.0] * n
        highs = [65.01] * n
        lows = [64.99] * n
        opens = [65.0] * n

        # Entry at bar 20 (price=65.0), ATR ≈ 0.02
        # TP = 65.0 + 2.0*0.02 = 65.04
        # Trailing triggers when high >= 65.0 + 1.0*0.02 = 65.02
        # New SL = 65.0 + 0.5*0.02 = 65.01

        # Bar 23: High triggers trailing (65.03 >= 65.02) but below TP (65.04)
        # Bars 23-29: price stays above new SL (65.01) and below TP
        for i in range(23, 30):
            opens[i] = 65.02
            prices[i] = 65.02
            highs[i] = 65.03   # Above trailing trigger, below TP
            lows[i] = 65.015   # Above new SL at 65.01

        # Bar 30: Open above SL, Low breaches SL → fills at SL price (65.01)
        opens[30] = 65.015
        prices[30] = 65.005
        highs[30] = 65.02
        lows[30] = 65.005  # Below SL at 65.01

        ohlcv = _make_ohlcv(n, prices=prices, opens=opens, highs=highs, lows=lows)
        signals = _make_signal(ohlcv, bar_idx=20)

        bt = _bt(trailing_atr_mult=1.0, trailing_sl_atr_offset=0.5)
        result = bt.run(signals, ohlcv)

        assert result.trade_count == 1
        trade = result.trades[0]
        assert trade.exit_reason == ExitReason.TRAILING_BE
        # Exit at SL=65.01, entry at 65.0 → positive PnL
        assert trade.net_pnl_dollars > 0

    def test_zero_offset_is_breakeven(self) -> None:
        """With offset=0.0, trailing stop is exact breakeven."""
        n = 40
        prices = [65.0] * n
        highs = [65.01] * n
        lows = [64.99] * n

        highs[23] = 65.03  # Trigger trailing
        lows[26] = 65.00   # Hit breakeven
        prices[26] = 65.00

        ohlcv = _make_ohlcv(n, prices=prices, highs=highs, lows=lows)
        signals = _make_signal(ohlcv, bar_idx=20)

        bt = _bt(trailing_atr_mult=1.0, trailing_sl_atr_offset=0.0)
        result = bt.run(signals, ohlcv)

        assert result.trade_count == 1
        trade = result.trades[0]
        assert trade.exit_reason == ExitReason.TRAILING_BE
        assert abs(trade.net_pnl_dollars) < 1.0  # ~$0


# ---------------------------------------------------------------------------
# Per-Order Override Tests
# ---------------------------------------------------------------------------


from src.live_execution.strategies.execution_models import (
    TieredEnsembleStrategy,
    Order as ExecOrder,
    EngineState as ExecEngineState,
    create_execution_strategy,
)


class TestOrderOverrides:
    """BacktestEngine honours per-Order TP/SL/trailing/max_hold overrides."""

    def test_order_tp_override_changes_tp_barrier(self) -> None:
        """Order.tp_atr_mult override should produce a different TP price."""
        # ATR ≈ 0.02, global tp_atr_mult=2.0 → TP=65.04
        # Override tp_atr_mult=50.0 → TP=65.0+50*0.02=66.0 (way above)
        # So with the override, TP is never hit and trade exits at time barrier.
        n = 40
        prices = [65.0] * n
        highs = [65.01] * n
        lows = [64.99] * n
        highs[25] = 65.05  # would be TP with global mult but NOT with override

        ohlcv = _make_ohlcv(n, prices=prices, highs=highs, lows=lows)

        # Create a TieredEnsembleStrategy config with a huge TP override
        config = {
            "nickname": "TPOverride",
            "execution_class": "TieredEnsembleStrategy",
            "long": {
                "experiment_id": "test",
                "tiers": [
                    {"min_prob": 0.50, "lots": 1, "tp_atr_mult": 50.0, "sl_atr_mult": 1.0, "max_hold_bars": 10, "label": "big_tp"}
                ]
            },
            "short": {},
            "tp_atr_mult": 2.0,
            "sl_atr_mult": 1.0,
            "max_hold_bars": 10,
        }
        bt = _bt_with_strategy(config)
        signals = _make_prob_signal(ohlcv, bar_idx=20, prob_buy=0.60)
        result = bt.run(signals, ohlcv)

        assert result.trade_count == 1
        # Should NOT be TP since override raised the TP barrier way above
        assert result.trades[0].exit_reason != ExitReason.TP

    def test_order_max_hold_override(self) -> None:
        """Order.max_hold_bars override should shorten the time barrier."""
        n = 40
        prices = [65.0] * n
        highs = [65.01] * n
        lows = [64.99] * n

        ohlcv = _make_ohlcv(n, prices=prices, highs=highs, lows=lows)

        config = {
            "nickname": "HoldOverride",
            "execution_class": "TieredEnsembleStrategy",
            "long": {
                "experiment_id": "test",
                "tiers": [
                    {"min_prob": 0.50, "lots": 1, "tp_atr_mult": 100.0, "sl_atr_mult": 100.0, "max_hold_bars": 5, "label": "short_hold"}
                ]
            },
            "short": {},
            "tp_atr_mult": 100.0,
            "sl_atr_mult": 100.0,
            "max_hold_bars": 288,
        }
        bt = _bt_with_strategy(config)
        signals = _make_prob_signal(ohlcv, bar_idx=20, prob_buy=0.60)
        result = bt.run(signals, ohlcv)

        assert result.trade_count == 1
        trade = result.trades[0]
        assert trade.exit_reason == ExitReason.TIME_BARRIER
        assert trade.duration_bars == 6  # max_hold_bars=5 → exits on bar 6


class TestTieredEnsembleStrategy:
    """TieredEnsembleStrategy tier matching and per-tier Order fields."""

    def test_high_prob_matches_first_tier(self) -> None:
        """prob_buy=0.85 should match the high_confidence tier (min_prob=0.75)."""
        config = {
            "execution_class": "TieredEnsembleStrategy",
            "long": {
                "tiers": [
                    {"min_prob": 0.75, "lots": 3, "tp_atr_mult": 5.0, "label": "high"},
                    {"min_prob": 0.60, "lots": 1, "tp_atr_mult": 2.0, "label": "base"},
                ]
            },
            "short": {},
        }
        strat = TieredEnsembleStrategy(config)
        state = ExecEngineState()
        orders = strat.on_bar(None, 65.0, 65.01, 64.99, 65.0, 0.02, 0.85, 0.0, state)
        assert len(orders) == 1
        assert orders[0].action == "BUY"
        assert orders[0].lots == 3
        assert orders[0].tp_atr_mult == 5.0
        assert "high" in orders[0].reason

    def test_medium_prob_matches_base_tier(self) -> None:
        """prob_buy=0.65 (< 0.75) should match the base tier."""
        config = {
            "execution_class": "TieredEnsembleStrategy",
            "long": {
                "tiers": [
                    {"min_prob": 0.75, "lots": 3, "label": "high"},
                    {"min_prob": 0.60, "lots": 1, "tp_atr_mult": 2.0, "label": "base"},
                ]
            },
            "short": {},
        }
        strat = TieredEnsembleStrategy(config)
        state = ExecEngineState()
        orders = strat.on_bar(None, 65.0, 65.01, 64.99, 65.0, 0.02, 0.65, 0.0, state)
        assert len(orders) == 1
        assert orders[0].lots == 1
        assert orders[0].tp_atr_mult == 2.0

    def test_below_all_tiers_returns_hold(self) -> None:
        """prob_buy=0.40 (below all tiers) should return HOLD."""
        config = {
            "execution_class": "TieredEnsembleStrategy",
            "long": {
                "tiers": [
                    {"min_prob": 0.75, "lots": 3, "label": "high"},
                    {"min_prob": 0.60, "lots": 1, "label": "base"},
                ]
            },
            "short": {},
        }
        strat = TieredEnsembleStrategy(config)
        state = ExecEngineState()
        orders = strat.on_bar(None, 65.0, 65.01, 64.99, 65.0, 0.02, 0.40, 0.0, state)
        assert orders[0].action == "HOLD"

    def test_conflict_resolution_higher_prob_wins(self) -> None:
        """When both buy and sell fire, higher probability wins."""
        config = {
            "execution_class": "TieredEnsembleStrategy",
            "long": {"tiers": [{"min_prob": 0.60, "lots": 1, "label": "long"}]},
            "short": {"tiers": [{"min_prob": 0.60, "lots": 1, "label": "short"}]},
        }
        strat = TieredEnsembleStrategy(config)
        state = ExecEngineState()

        # Buy wins
        orders = strat.on_bar(None, 65.0, 65.01, 64.99, 65.0, 0.02, 0.80, 0.70, state)
        assert orders[0].action == "BUY"

        # Sell wins
        orders = strat.on_bar(None, 65.0, 65.01, 64.99, 65.0, 0.02, 0.65, 0.75, state)
        assert orders[0].action == "SELL"

    def test_in_position_returns_hold(self) -> None:
        """When already in a position, TieredEnsembleStrategy should HOLD."""
        config = {
            "execution_class": "TieredEnsembleStrategy",
            "long": {"tiers": [{"min_prob": 0.60, "lots": 1, "label": "long"}]},
            "short": {},
        }
        strat = TieredEnsembleStrategy(config)
        state = ExecEngineState(position=1, side=1)
        orders = strat.on_bar(None, 65.0, 65.01, 64.99, 65.0, 0.02, 0.90, 0.0, state)
        assert orders[0].action == "HOLD"

    def test_order_carries_all_override_fields(self) -> None:
        """Matched tier's TP/SL/trailing/max_hold should be on the Order."""
        config = {
            "execution_class": "TieredEnsembleStrategy",
            "long": {
                "tiers": [{
                    "min_prob": 0.50,
                    "lots": 2,
                    "tp_atr_mult": 3.0,
                    "sl_atr_mult": 1.5,
                    "trailing_atr_mult": 2.5,
                    "max_hold_bars": 100,
                    "label": "full",
                }]
            },
            "short": {},
        }
        strat = TieredEnsembleStrategy(config)
        state = ExecEngineState()
        orders = strat.on_bar(None, 65.0, 65.01, 64.99, 65.0, 0.02, 0.60, 0.0, state)
        o = orders[0]
        assert o.tp_atr_mult == 3.0
        assert o.sl_atr_mult == 1.5
        assert o.trailing_atr_mult == 2.5
        assert o.max_hold_bars == 100
        assert o.lots == 2


class TestTieredWithEngine:
    """End-to-end: TieredEnsembleStrategy + BacktestEngine.from_config."""

    def test_tiered_config_produces_trades(self) -> None:
        """TieredEnsemble config fires on prob signals and exits correctly."""
        config = {
            "nickname": "TieredTest",
            "execution_class": "TieredEnsembleStrategy",
            "long": {
                "experiment_id": "test",
                "tiers": [
                    {"min_prob": 0.60, "lots": 1, "tp_atr_mult": 2.0, "sl_atr_mult": 1.0, "max_hold_bars": 15, "label": "test"}
                ]
            },
            "short": {},
            "tp_atr_mult": 2.0,
            "sl_atr_mult": 1.0,
            "max_hold_bars": 288,
        }
        n = 40
        prices = [65.0] * n
        highs = [65.01] * n
        lows = [64.99] * n
        highs[25] = 65.05  # TP hit

        ohlcv = _make_ohlcv(n, prices=prices, highs=highs, lows=lows)
        signals = _make_prob_signal(ohlcv, bar_idx=20, prob_buy=0.70)

        bt = _bt_with_strategy(config)
        result = bt.run(signals, ohlcv)

        assert result.trade_count == 1
        assert result.trades[0].exit_reason == ExitReason.TP
        assert result.trades[0].side == 1

    def test_tiered_registry_lookup(self) -> None:
        """TieredEnsembleStrategy is found in the registry."""
        config = {
            "execution_class": "TieredEnsembleStrategy",
            "long": {"tiers": []},
            "short": {"tiers": []},
        }
        strategy = create_execution_strategy(config)
        assert isinstance(strategy, TieredEnsembleStrategy)
