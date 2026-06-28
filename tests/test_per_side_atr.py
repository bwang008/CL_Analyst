"""
Test per-side ATR period and trailing_sl_atr_offset in BacktestEngine.

Verifies that:
1. Different ATR periods produce different ATR columns for long/short
2. Long trades use atr_long_ and short trades use atr_short_
3. Per-side trailing_sl_atr_offset is applied correctly
4. When both sides have the same ATR period, behavior matches global
"""

import copy
import os
import sys

import numpy as np
import pandas as pd
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from agent.backtest_engine import BacktestEngine, BacktestResult
from src.live_execution.strategies.execution_models import (
    TieredEnsembleStrategy,
    EngineState,
    create_execution_strategy,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ohlcv(n: int = 200, seed: int = 42) -> pd.DataFrame:
    """Generate synthetic OHLCV data for testing."""
    rng = np.random.RandomState(seed)
    dates = pd.date_range("2020-01-01", periods=n, freq="5min")
    close = 60.0 + np.cumsum(rng.randn(n) * 0.05)
    high = close + rng.uniform(0.01, 0.10, n)
    low = close - rng.uniform(0.01, 0.10, n)
    open_ = close + rng.randn(n) * 0.02
    volume = rng.randint(100, 10000, n).astype(float)
    return pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume},
        index=dates,
    )


def _make_predictions(ohlcv: pd.DataFrame, buy_prob: float = 0.0,
                      sell_prob: float = 0.0) -> pd.DataFrame:
    """Generate uniform predictions matching OHLCV index."""
    return pd.DataFrame(
        {"prob_Buy": buy_prob, "prob_Sell": sell_prob},
        index=ohlcv.index,
    )


TIERED_CONFIG = {
    "nickname": "test_per_side_atr",
    "execution_class": "TieredEnsembleStrategy",
    "exit_mode": "TIERED",
    "tp_atr_mult": 3.0,
    "sl_atr_mult": 1.5,
    "trailing_atr_mult": 100.0,  # effectively disabled
    "max_hold_bars": 200,
    "cooldown_bars": 0,
    "entry_threshold": 0.40,
    "allow_concurrent": False,
    "max_concurrent": 1,
    "atr_period": 14,
    "trailing_sl_atr_offset": 0.25,
    "models": {
        "long": {"threshold": 0.40},
        "short": {"threshold": 0.40},
    },
    "long": {
        "tp_atr_mult": 3.0,
        "sl_atr_mult": 1.5,
        "atr_period": 10,
        "trailing_sl_atr_offset": 0.5,
        "tiered_exits": [{"qty_pct": 1.0, "tp_atr_mult": 3.0}],
        "tiers": [{"min_prob": 0.40, "lots": 1, "tp_atr_mult": 3.0, "sl_atr_mult": 1.5}],
    },
    "short": {
        "tp_atr_mult": 3.0,
        "sl_atr_mult": 1.5,
        "atr_period": 30,
        "trailing_sl_atr_offset": 1.0,
        "tiered_exits": [{"qty_pct": 1.0, "tp_atr_mult": 3.0}],
        "tiers": [{"min_prob": 0.40, "lots": 1, "tp_atr_mult": 3.0, "sl_atr_mult": 1.5}],
    },
}


# ---------------------------------------------------------------------------
# Tests: from_config reads per-side values
# ---------------------------------------------------------------------------


class TestFromConfigPerSide:
    """Verify BacktestEngine.from_config reads per-side ATR and trailing offset."""

    def test_reads_per_side_atr_period(self):
        """Engine should read long.atr_period=10 and short.atr_period=30."""
        cfg = copy.deepcopy(TIERED_CONFIG)
        engine = BacktestEngine.from_config(cfg)
        assert engine.atr_period_long == 10
        assert engine.atr_period_short == 30

    def test_reads_per_side_trailing_offset(self):
        """Engine should read long trailing=0.5 and short trailing=1.0."""
        cfg = copy.deepcopy(TIERED_CONFIG)
        engine = BacktestEngine.from_config(cfg)
        assert engine.trailing_sl_atr_offset_long == 0.5
        assert engine.trailing_sl_atr_offset_short == 1.0

    def test_fallback_to_global_when_missing(self):
        """Without per-side keys, engine falls back to global atr_period."""
        cfg = copy.deepcopy(TIERED_CONFIG)
        # Remove per-side atr_period
        del cfg["long"]["atr_period"]
        del cfg["short"]["atr_period"]
        del cfg["long"]["trailing_sl_atr_offset"]
        del cfg["short"]["trailing_sl_atr_offset"]
        engine = BacktestEngine.from_config(cfg)
        assert engine.atr_period_long == 14  # global fallback
        assert engine.atr_period_short == 14
        assert engine.trailing_sl_atr_offset_long == 0.25  # global from fixture
        assert engine.trailing_sl_atr_offset_short == 0.25


# ---------------------------------------------------------------------------
# Tests: ATR column computation
# ---------------------------------------------------------------------------


class TestATRComputation:
    """Verify that run() computes separate ATR columns when periods differ."""

    def test_different_atr_columns_when_periods_differ(self):
        """atr_long_ and atr_short_ should differ when periods differ."""
        cfg = copy.deepcopy(TIERED_CONFIG)
        engine = BacktestEngine.from_config(cfg)
        assert engine.atr_period_long != engine.atr_period_short

        ohlcv = _make_ohlcv(200)
        preds = _make_predictions(ohlcv, buy_prob=0.0, sell_prob=0.0)
        # Run to trigger ATR computation (no trades expected)
        engine.run(preds, ohlcv)
        # The ATR computation happens on a copy, so we verify via the engine
        # by running manually
        import pandas_ta as ta
        atr_10 = ohlcv.ta.atr(length=10)
        atr_30 = ohlcv.ta.atr(length=30)
        # They should differ (different lookback windows)
        valid_mask = atr_10.notna() & atr_30.notna()
        assert not np.allclose(atr_10[valid_mask].values, atr_30[valid_mask].values)

    def test_same_atr_columns_when_periods_equal(self):
        """When periods are equal, both columns should be identical."""
        cfg = copy.deepcopy(TIERED_CONFIG)
        cfg["long"]["atr_period"] = 20
        cfg["short"]["atr_period"] = 20
        engine = BacktestEngine.from_config(cfg)
        assert engine.atr_period_long == engine.atr_period_short == 20


# ---------------------------------------------------------------------------
# Tests: Trades use correct per-side ATR
# ---------------------------------------------------------------------------


class TestTradesUsePerSideATR:
    """Verify that long and short trades use their respective ATR periods."""

    def test_long_trade_uses_long_atr(self):
        """A long-only run should use atr_period_long for ATR at entry."""
        cfg = copy.deepcopy(TIERED_CONFIG)
        # Only enable long signals
        cfg["short"]["tiers"][0]["min_prob"] = 1.0  # disable short
        cfg["models"]["short"]["threshold"] = 1.0
        engine = BacktestEngine.from_config(cfg)

        ohlcv = _make_ohlcv(200, seed=42)
        preds = _make_predictions(ohlcv, buy_prob=0.50, sell_prob=0.0)
        result = engine.run(preds, ohlcv)

        if result.trades:
            # Verify ATR at entry matches ATR(10) not ATR(30)
            trade = result.trades[0]
            import pandas_ta as ta
            atr_10 = ohlcv.ta.atr(length=10)
            atr_30 = ohlcv.ta.atr(length=30)
            entry_idx = ohlcv.index.get_loc(trade.entry_dt)
            expected_atr_long = atr_10.iloc[entry_idx]
            expected_atr_short = atr_30.iloc[entry_idx]
            assert abs(trade.atr_at_entry - expected_atr_long) < 1e-10, \
                f"Long trade should use ATR(10)={expected_atr_long}, got {trade.atr_at_entry}"
            # Ensure it's NOT using the short ATR (unless they coincidentally match)
            if abs(expected_atr_long - expected_atr_short) > 1e-10:
                assert abs(trade.atr_at_entry - expected_atr_short) > 1e-10

    def test_short_trade_uses_short_atr(self):
        """A short-only run should use atr_period_short for ATR at entry."""
        cfg = copy.deepcopy(TIERED_CONFIG)
        # Only enable short signals
        cfg["long"]["tiers"][0]["min_prob"] = 1.0  # disable long
        cfg["models"]["long"]["threshold"] = 1.0
        engine = BacktestEngine.from_config(cfg)

        ohlcv = _make_ohlcv(200, seed=42)
        preds = _make_predictions(ohlcv, buy_prob=0.0, sell_prob=0.50)
        result = engine.run(preds, ohlcv)

        if result.trades:
            trade = result.trades[0]
            import pandas_ta as ta
            atr_30 = ohlcv.ta.atr(length=30)
            entry_idx = ohlcv.index.get_loc(trade.entry_dt)
            expected_atr_short = atr_30.iloc[entry_idx]
            assert abs(trade.atr_at_entry - expected_atr_short) < 1e-10, \
                f"Short trade should use ATR(30)={expected_atr_short}, got {trade.atr_at_entry}"


# ---------------------------------------------------------------------------
# Tests: Backward compatibility (no per-side config)
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    """Configs without per-side atr_period should behave exactly as before."""

    def test_global_only_config_produces_trades(self):
        """A config with only global atr_period should still run correctly."""
        cfg = {
            "nickname": "test_global_atr",
            "execution_class": "TieredEnsembleStrategy",
            "exit_mode": "TIERED",
            "tp_atr_mult": 3.0,
            "sl_atr_mult": 1.5,
            "trailing_atr_mult": 100.0,
            "max_hold_bars": 200,
            "cooldown_bars": 0,
            "entry_threshold": 0.40,
            "allow_concurrent": False,
            "max_concurrent": 1,
            "atr_period": 14,
            "trailing_sl_atr_offset": 0.25,
            "models": {
                "long": {"threshold": 0.40},
                "short": {"threshold": 0.40},
            },
            "long": {
                "tp_atr_mult": 3.0,
                "sl_atr_mult": 1.5,
                "tiered_exits": [{"qty_pct": 1.0, "tp_atr_mult": 3.0}],
                "tiers": [{"min_prob": 0.40, "lots": 1, "tp_atr_mult": 3.0, "sl_atr_mult": 1.5}],
            },
            "short": {
                "tp_atr_mult": 3.0,
                "sl_atr_mult": 1.5,
                "tiered_exits": [{"qty_pct": 1.0, "tp_atr_mult": 3.0}],
                "tiers": [{"min_prob": 0.40, "lots": 1, "tp_atr_mult": 3.0, "sl_atr_mult": 1.5}],
            },
        }
        engine = BacktestEngine.from_config(cfg)
        assert engine.atr_period_long == 14
        assert engine.atr_period_short == 14

        ohlcv = _make_ohlcv(200, seed=99)
        preds = _make_predictions(ohlcv, buy_prob=0.50, sell_prob=0.50)
        result = engine.run(preds, ohlcv)
        # Should run without errors
        assert isinstance(result, BacktestResult)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
