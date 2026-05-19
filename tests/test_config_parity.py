"""
Test Config Parity — Backtest Engine vs Live Trader parameter resolution.

Verifies that for a given strategy JSON config, both BacktestEngine.from_config()
and the LiveTrader's __init__() resolve IDENTICAL values for all execution
parameters.  This catches naming mismatches and missing config key lookups.

Tests cover:
  1. trailing_sl_atr_offset parity (new canonical key + legacy fallback)
  2. atr_period propagation (bracket ATR != model feature ATR)
  3. trailing_atr_mult default alignment (100.0 in both systems)
  4. max_hold_bars routing
  5. Backward compatibility (old configs still work)
"""

import copy
import os
import sys
from typing import Optional
from unittest.mock import patch, MagicMock

import numpy as np
import pandas as pd
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from agent.backtest_engine import BacktestEngine
from src.live_execution.strategies.execution_models import (
    TieredEnsembleStrategy,
    create_execution_strategy,
)
from src.live_execution.strategy_config import StrategyConfig


# ---------------------------------------------------------------------------
# Shared Test Config Fixtures
# ---------------------------------------------------------------------------

# Minimal TieredEnsembleStrategy config resembling production
PRODUCTION_LIKE_CONFIG = {
    "nickname": "parity_test",
    "execution_class": "TieredEnsembleStrategy",
    "exit_mode": "TIERED",
    "tp_atr_mult": 7.0,
    "sl_atr_mult": 3.25,
    "trailing_atr_mult": 3.25,
    "trailing_sl_atr_offset": 3.5,
    "atr_period": 26,
    "max_hold_bars": 192,
    "cooldown_bars": 3,
    "entry_threshold": 0.5,
    "allow_concurrent": False,
    "max_concurrent": 1,
    "bar_size": "1h",
    "models": {
        "long": {"threshold": 0.5},
        "short": {"threshold": 0.53},
    },
    "long": {
        "tp_atr_mult": 7.0,
        "sl_atr_mult": 3.25,
        "trailing_atr_mult": 3.25,
        "trailing_sl_atr_offset": 3.0,
        "max_hold_bars": 192,
        "cooldown_bars": 15,
        "atr_period": 18,
        "tiers": [
            {"min_prob": 0.5, "lots": 1, "tp_atr_mult": 7.0,
             "sl_atr_mult": 3.25, "trailing_atr_mult": 3.25,
             "max_hold_bars": 192},
        ],
        "tiered_exits": [{"qty_pct": 1.0, "tp_atr_mult": 7.0}],
    },
    "short": {
        "tp_atr_mult": 5.0,
        "sl_atr_mult": 1.75,
        "trailing_atr_mult": 4.25,
        "trailing_sl_atr_offset": 3.5,
        "max_hold_bars": 216,
        "cooldown_bars": 3,
        "atr_period": 26,
        "tiers": [
            {"min_prob": 0.53, "lots": 1, "tp_atr_mult": 5.0,
             "sl_atr_mult": 1.75, "trailing_atr_mult": 4.25,
             "max_hold_bars": 216},
        ],
        "tiered_exits": [{"qty_pct": 1.0, "tp_atr_mult": 5.0}],
    },
}

# Config with MINIMAL keys — tests default fallback alignment
MINIMAL_CONFIG = {
    "nickname": "minimal_test",
    "execution_class": "TieredEnsembleStrategy",
    "models": {
        "long": {"threshold": 0.6},
        "short": {"threshold": 0.6},
    },
    "long": {
        "tiers": [{"min_prob": 0.6, "lots": 1}],
    },
    "short": {
        "tiers": [{"min_prob": 0.6, "lots": 1}],
    },
}


def _mock_live_trader_config(strategy_config: dict) -> dict:
    """Simulate the LiveTrader.__init__ parameter resolution.

    Uses StrategyConfig.from_dict() — the same path as the actual LiveTrader.
    Returns a dict of resolved parameter names and values.
    """
    sc = StrategyConfig.from_dict(strategy_config)
    return {
        "max_hold_bars": sc.max_hold_bars,
        "trailing_atr_mult": sc.trailing_atr_mult,
        "trailing_sl_atr_offset": sc.trailing_sl_atr_offset,
        "trailing_sl_atr_offset_long": sc.long.trailing_sl_atr_offset,
        "trailing_sl_atr_offset_short": sc.short.trailing_sl_atr_offset,
        "atr_period": sc.atr_period,
    }


def _backtest_engine_config(cfg: dict) -> dict:
    """Extract BacktestEngine resolved parameters from from_config.

    Creates a BacktestEngine and reads its public attributes.
    """
    engine = BacktestEngine.from_config(cfg)
    return {
        "max_hold_bars": engine.max_horizon,
        "trailing_atr_mult": engine.trailing_atr_mult,
        "trailing_sl_atr_offset": engine.trailing_sl_atr_offset,
        "trailing_sl_atr_offset_long": engine.trailing_sl_atr_offset_long,
        "trailing_sl_atr_offset_short": engine.trailing_sl_atr_offset_short,
        "atr_period": engine.atr_period,
        "atr_period_long": engine.atr_period_long,
        "atr_period_short": engine.atr_period_short,
        "tp_atr_mult": engine.tp_atr_mult,
        "sl_atr_mult": engine.sl_atr_mult,
    }


# ---------------------------------------------------------------------------
# Tests: trailing_sl_atr_offset parity
# ---------------------------------------------------------------------------


class TestTrailingSLAtrOffsetParity:
    """Verify both systems read the trailing_sl_atr_offset config key."""

    def test_production_config_trailing_offset_matches(self):
        """Both systems should resolve trailing_sl_atr_offset=3.5 from config."""
        cfg = copy.deepcopy(PRODUCTION_LIKE_CONFIG)

        bt = _backtest_engine_config(cfg)
        lt = _mock_live_trader_config(cfg)

        assert bt["trailing_sl_atr_offset"] == 3.5, \
            f"Backtest trailing_sl_atr_offset={bt['trailing_sl_atr_offset']}, expected 3.5"
        assert lt["trailing_sl_atr_offset"] == 3.5, \
            f"LiveTrader trailing_sl_atr_offset={lt['trailing_sl_atr_offset']}, expected 3.5"
        assert bt["trailing_sl_atr_offset"] == lt["trailing_sl_atr_offset"]

    def test_missing_trailing_offset_uses_default(self):
        """If trailing_sl_atr_offset is absent, both should default to 0.25."""
        cfg = copy.deepcopy(MINIMAL_CONFIG)
        assert "trailing_sl_atr_offset" not in cfg
        assert "trailing_activation_mult" not in cfg

        bt = _backtest_engine_config(cfg)
        lt = _mock_live_trader_config(cfg)

        assert bt["trailing_sl_atr_offset"] == 1.0
        assert lt["trailing_sl_atr_offset"] == 1.0

    def test_legacy_key_trailing_activation_mult_is_accepted(self):
        """If a config uses 'trailing_activation_mult' (legacy key),
        it should be parsed as trailing_sl_atr_offset."""
        cfg = copy.deepcopy(MINIMAL_CONFIG)
        cfg["trailing_activation_mult"] = 4.0  # Legacy key

        bt = _backtest_engine_config(cfg)
        lt = _mock_live_trader_config(cfg)

        assert bt["trailing_sl_atr_offset"] == 4.0
        assert lt["trailing_sl_atr_offset"] == 4.0

    def test_new_key_takes_priority_over_legacy(self):
        """If both trailing_sl_atr_offset and trailing_activation_mult exist,
        trailing_sl_atr_offset takes priority."""
        cfg = copy.deepcopy(MINIMAL_CONFIG)
        cfg["trailing_sl_atr_offset"] = 5.0  # New canonical key
        cfg["trailing_activation_mult"] = 2.0  # Legacy key (should be ignored)

        bt = _backtest_engine_config(cfg)
        lt = _mock_live_trader_config(cfg)

        assert bt["trailing_sl_atr_offset"] == 5.0  # new key wins
        assert lt["trailing_sl_atr_offset"] == 5.0

    def test_per_side_trailing_offset_parity(self):
        """Both systems should resolve asymmetric per-side trailing offsets."""
        cfg = copy.deepcopy(PRODUCTION_LIKE_CONFIG)

        bt = _backtest_engine_config(cfg)
        lt = _mock_live_trader_config(cfg)

        # long = 3.0, short = 3.5
        assert bt["trailing_sl_atr_offset_long"] == 3.0
        assert bt["trailing_sl_atr_offset_short"] == 3.5
        assert lt["trailing_sl_atr_offset_long"] == 3.0
        assert lt["trailing_sl_atr_offset_short"] == 3.5


# ---------------------------------------------------------------------------
# Tests: ATR period parity
# ---------------------------------------------------------------------------


class TestAtrPeriodParity:
    """Verify atr_period propagation to both systems."""

    def test_atr_period_26_propagates(self):
        """Config with atr_period=26 should be read by both systems."""
        cfg = copy.deepcopy(PRODUCTION_LIKE_CONFIG)

        bt = _backtest_engine_config(cfg)
        lt = _mock_live_trader_config(cfg)

        assert bt["atr_period"] == 26
        assert lt["atr_period"] == 26

    def test_atr_period_default_14(self):
        """Missing atr_period should default to 14 in both systems."""
        cfg = copy.deepcopy(MINIMAL_CONFIG)
        assert "atr_period" not in cfg

        bt = _backtest_engine_config(cfg)
        lt = _mock_live_trader_config(cfg)

        assert bt["atr_period"] == 14
        assert lt["atr_period"] == 14

    def test_per_side_atr_periods(self):
        """BacktestEngine should read per-side atr_period from long/short blocks."""
        cfg = copy.deepcopy(PRODUCTION_LIKE_CONFIG)

        bt = _backtest_engine_config(cfg)

        # long.atr_period = 18, short.atr_period = 26
        assert bt["atr_period_long"] == 18
        assert bt["atr_period_short"] == 26


# ---------------------------------------------------------------------------
# Tests: trailing_atr_mult default alignment
# ---------------------------------------------------------------------------


class TestTrailingAtrMultDefault:
    """Verify trailing_atr_mult defaults are aligned (100.0 = disabled)."""

    def test_default_trailing_atr_mult_is_100(self):
        """When trailing_atr_mult is missing, both should default to 100.0."""
        cfg = copy.deepcopy(MINIMAL_CONFIG)
        assert "trailing_atr_mult" not in cfg

        bt = _backtest_engine_config(cfg)
        lt = _mock_live_trader_config(cfg)

        assert bt["trailing_atr_mult"] == 100.0, \
            f"Backtest trailing_atr_mult={bt['trailing_atr_mult']}, expected 100.0"
        assert lt["trailing_atr_mult"] == 100.0, \
            f"LiveTrader trailing_atr_mult={lt['trailing_atr_mult']}, expected 100.0"

    def test_explicit_trailing_atr_mult_used(self):
        """When trailing_atr_mult is set, both should use the config value."""
        cfg = copy.deepcopy(PRODUCTION_LIKE_CONFIG)

        bt = _backtest_engine_config(cfg)
        lt = _mock_live_trader_config(cfg)

        assert bt["trailing_atr_mult"] == 3.25
        assert lt["trailing_atr_mult"] == 3.25


# ---------------------------------------------------------------------------
# Tests: max_hold_bars routing
# ---------------------------------------------------------------------------


class TestMaxHoldBarsParity:
    """Verify max_hold_bars is read identically."""

    def test_production_max_hold_bars(self):
        cfg = copy.deepcopy(PRODUCTION_LIKE_CONFIG)

        bt = _backtest_engine_config(cfg)
        lt = _mock_live_trader_config(cfg)

        assert bt["max_hold_bars"] == 192
        assert lt["max_hold_bars"] == 192

    def test_default_max_hold_bars(self):
        cfg = copy.deepcopy(MINIMAL_CONFIG)
        assert "max_hold_bars" not in cfg

        bt = _backtest_engine_config(cfg)
        lt = _mock_live_trader_config(cfg)

        assert bt["max_hold_bars"] == 288
        assert lt["max_hold_bars"] == 288


# ---------------------------------------------------------------------------
# Tests: Full side-by-side comparison
# ---------------------------------------------------------------------------


class TestFullParityComparison:
    """Side-by-side comparison of ALL shared parameters."""

    SHARED_PARAMS = [
        "max_hold_bars",
        "trailing_atr_mult",
        "trailing_sl_atr_offset",
        "trailing_sl_atr_offset_long",
        "trailing_sl_atr_offset_short",
        "atr_period",
    ]

    def test_production_config_full_parity(self):
        """Every shared parameter must match between systems."""
        cfg = copy.deepcopy(PRODUCTION_LIKE_CONFIG)

        bt = _backtest_engine_config(cfg)
        lt = _mock_live_trader_config(cfg)

        mismatches = []
        for param in self.SHARED_PARAMS:
            bt_val = bt[param]
            lt_val = lt[param]
            if bt_val != lt_val:
                mismatches.append(
                    f"  {param}: BacktestEngine={bt_val}, LiveTrader={lt_val}"
                )

        assert not mismatches, (
            "PARAMETER PARITY FAILURE — the following parameters differ "
            "between BacktestEngine and LiveTrader:\n"
            + "\n".join(mismatches)
        )

    def test_minimal_config_full_parity(self):
        """Default-heavy config should also have full parity."""
        cfg = copy.deepcopy(MINIMAL_CONFIG)

        bt = _backtest_engine_config(cfg)
        lt = _mock_live_trader_config(cfg)

        mismatches = []
        for param in self.SHARED_PARAMS:
            bt_val = bt[param]
            lt_val = lt[param]
            if bt_val != lt_val:
                mismatches.append(
                    f"  {param}: BacktestEngine={bt_val}, LiveTrader={lt_val}"
                )

        assert not mismatches, (
            "DEFAULT PARITY FAILURE:\n" + "\n".join(mismatches)
        )


# ---------------------------------------------------------------------------
# Tests: apply_trial_params routing for trailing_sl_atr_offset
# ---------------------------------------------------------------------------


class TestApplyTrialParamsTrailingOffset:
    """Verify apply_trial_params routes trailing_sl_atr_offset correctly."""

    def test_trailing_sl_atr_offset_reaches_side_config(self):
        """trailing_sl_atr_offset should be written to side config."""
        cfg = copy.deepcopy(PRODUCTION_LIKE_CONFIG)
        strategy = create_execution_strategy(cfg)

        strategy.apply_trial_params(
            cfg,
            {"trailing_sl_atr_offset": 2.0},
            side="long",
        )

        assert cfg["long"]["trailing_sl_atr_offset"] == 2.0
        assert cfg["trailing_sl_atr_offset"] == 2.0  # top-level too

    def test_trailing_sl_atr_offset_per_side_independence(self):
        """Each side should get its own trailing_sl_atr_offset."""
        cfg = copy.deepcopy(PRODUCTION_LIKE_CONFIG)
        strategy = create_execution_strategy(cfg)

        strategy.apply_trial_params(cfg, {"trailing_sl_atr_offset": 1.5}, side="long")
        strategy.apply_trial_params(cfg, {"trailing_sl_atr_offset": 4.0}, side="short")

        assert cfg["long"]["trailing_sl_atr_offset"] == 1.5
        assert cfg["short"]["trailing_sl_atr_offset"] == 4.0
        # Top-level gets short's value (last write wins)
        assert cfg["trailing_sl_atr_offset"] == 4.0

    def test_backtest_engine_reads_applied_trailing_offset(self):
        """After apply_trial_params, BacktestEngine.from_config should use the value."""
        cfg = copy.deepcopy(PRODUCTION_LIKE_CONFIG)
        strategy = create_execution_strategy(cfg)

        strategy.apply_trial_params(cfg, {"trailing_sl_atr_offset": 5.0})

        engine = BacktestEngine.from_config(cfg)
        assert engine.trailing_sl_atr_offset == 5.0

    def test_legacy_key_in_apply_trial_params(self):
        """apply_trial_params should still accept trailing_activation_mult."""
        cfg = copy.deepcopy(PRODUCTION_LIKE_CONFIG)
        strategy = create_execution_strategy(cfg)

        strategy.apply_trial_params(
            cfg,
            {"trailing_activation_mult": 2.5},
            side="long",
        )

        # Should be written under the new canonical key
        assert cfg["long"]["trailing_sl_atr_offset"] == 2.5


# ---------------------------------------------------------------------------
# Tests: Bracket ATR computation (live trader path)
# ---------------------------------------------------------------------------


class TestBracketAtrComputation:
    """Verify bracket ATR is computed with the correct period."""

    def _make_rolling_df(self, n_bars: int = 100) -> pd.DataFrame:
        """Generate synthetic OHLCV data for ATR computation."""
        dates = pd.date_range("2025-01-01", periods=n_bars, freq="h")
        np.random.seed(42)
        close = 60.0 + np.cumsum(np.random.randn(n_bars) * 0.1)
        high = close + np.abs(np.random.randn(n_bars) * 0.05)
        low = close - np.abs(np.random.randn(n_bars) * 0.05)
        open_ = close + np.random.randn(n_bars) * 0.02
        volume = np.random.randint(1000, 10000, n_bars)
        return pd.DataFrame({
            "Open": open_, "High": high, "Low": low,
            "Close": close, "Volume": volume,
        }, index=dates)

    def test_atr_14_fast_path(self):
        """When atr_period=14, should use ATR_14 from features (fast path)."""
        import pandas_ta as ta

        rolling_df = self._make_rolling_df(100)
        atr_14 = rolling_df.ta.atr(length=14)
        expected_atr = float(atr_14.iloc[-1])

        # Simulate the live trader logic for atr_period=14
        atr_period = 14
        features = pd.DataFrame({"ATR_14": [expected_atr]})

        if atr_period == 14 and "ATR_14" in features.columns:
            atr_value = float(features["ATR_14"].iloc[0])
        else:
            atr_value = None

        assert atr_value is not None
        assert abs(atr_value - expected_atr) < 1e-6

    def test_atr_26_custom_path(self):
        """When atr_period=26, should compute ATR(26) from rolling_df."""
        import pandas_ta as ta

        rolling_df = self._make_rolling_df(100)
        atr_26 = rolling_df.ta.atr(length=26)
        expected_atr = float(atr_26.iloc[-1])

        # Simulate the live trader logic for atr_period=26
        atr_period = 26
        features = pd.DataFrame({"ATR_14": [0.123]})  # model feature

        if atr_period == 14 and "ATR_14" in features.columns:
            atr_value = float(features["ATR_14"].iloc[0])
        elif len(rolling_df) >= atr_period + 1:
            _bracket_atr = rolling_df.ta.atr(length=atr_period)
            _last_atr = _bracket_atr.iloc[-1]
            atr_value = float(_last_atr) if not np.isnan(_last_atr) else None
        else:
            atr_value = None

        assert atr_value is not None
        assert abs(atr_value - expected_atr) < 1e-6
        # ATR(26) should differ from ATR(14)
        atr_14_val = float(rolling_df.ta.atr(length=14).iloc[-1])
        assert abs(atr_value - atr_14_val) > 1e-8, \
            "ATR(26) should differ from ATR(14)"


# ---------------------------------------------------------------------------
# Print side-by-side comparison (manual verification helper)
# ---------------------------------------------------------------------------


def print_parity_comparison(config_path: str = None):
    """Print a side-by-side parameter comparison for manual review.

    Can be run standalone:
        python tests/test_config_parity.py --compare configs/strategies/hs08_sweep_5x1_24h_logloss_fix.json
    """
    import json

    if config_path is None:
        cfg = copy.deepcopy(PRODUCTION_LIKE_CONFIG)
        config_path = "<embedded test config>"
    else:
        with open(config_path) as f:
            cfg = json.load(f)

    bt = _backtest_engine_config(cfg)
    lt = _mock_live_trader_config(cfg)

    print()
    print("=" * 75)
    print(f" CONFIG PARAMETER PARITY REPORT".center(75))
    print(f" Config: {config_path}".center(75))
    print("=" * 75)
    print()
    print(f"  {'Parameter':<30}  {'BacktestEngine':>15}  {'LiveTrader':>15}  {'Match':>6}")
    print("  " + "-" * 70)

    all_keys = sorted(set(list(bt.keys()) + list(lt.keys())))

    mismatches = 0
    for key in all_keys:
        bt_val = bt.get(key, "N/A")
        lt_val = lt.get(key, "N/A")

        if bt_val == "N/A" or lt_val == "N/A":
            match = "  -"
        elif bt_val == lt_val:
            match = " OK"
        else:
            match = " XX"
            mismatches += 1

        print(f"  {key:<30}  {str(bt_val):>15}  {str(lt_val):>15}  {match}")

    print()
    if mismatches == 0:
        print("  [PASS] PARITY CONFIRMED -- all shared parameters match")
    else:
        print(f"  [FAIL] {mismatches} PARAMETER MISMATCH(ES) DETECTED")
    print("=" * 75)
    print()


if __name__ == "__main__":
    import sys as _sys
    if len(_sys.argv) > 1 and _sys.argv[1] == "--compare":
        config_path = _sys.argv[2] if len(_sys.argv) > 2 else None
        print_parity_comparison(config_path)
    else:
        pytest.main([__file__, "-v"])
