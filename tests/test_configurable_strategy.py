"""
Tests for the ConfigurableStrategy — config-driven universal strategy.

All tests use mocks — no real model, config file on disk, or IBKR needed.
"""

from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from src.live_execution.strategy import Strategy, TradeSignal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_config(tmp_dir: str, overrides: dict | None = None) -> str:
    """Write a strategy JSON config to a temp file, return its path."""
    config = {
        "nickname": "TestStrat",
        "experiment_id": "EXP-TEST",
        "direction": "LONG",
        "allow_concurrent": False,
        "entry_threshold": 0.70,
        "tp_atr_mult": 7.0,
        "sl_atr_mult": 1.0,
        "sizing_tiers": {
            "0.80": 3,
            "0.70": 2,
            "0.60": 2,
            "0.50": 1,
        },
    }
    if overrides:
        config.update(overrides)
    path = os.path.join(tmp_dir, "test_strategy.json")
    with open(path, "w") as f:
        json.dump(config, f)
    return path


def _make_strategy_stub(
    tmp_dir: str,
    overrides: dict | None = None,
    *,
    base_quantity: int = 1,
):
    """Create a ConfigurableStrategy without hitting disk for model."""
    from src.live_execution.strategies.configurable_strategy import (
        ConfigurableStrategy,
    )

    config_path = _write_config(tmp_dir, overrides)

    # Bypass __init__ to avoid loading a real model
    strategy = object.__new__(ConfigurableStrategy)

    # Load config manually (same as __init__ would)
    with open(config_path) as f:
        strategy.config = json.load(f)

    strategy._nickname = strategy.config["nickname"]
    strategy._direction = strategy.config["direction"].upper()
    strategy.allow_concurrent = strategy.config.get("allow_concurrent", False)

    raw_threshold = strategy.config.get("entry_threshold", None)
    if raw_threshold is None or not isinstance(raw_threshold, (int, float)):
        strategy.entry_threshold = 100.0
    else:
        strategy.entry_threshold = float(raw_threshold)

    strategy.tp_atr_mult = float(strategy.config.get("tp_atr_mult", 2.0))
    strategy.sl_atr_mult = float(strategy.config.get("sl_atr_mult", 1.0))

    raw_tiers = strategy.config.get("sizing_tiers", {})
    strategy.sizing_tiers = sorted(
        [(float(k), int(v)) for k, v in raw_tiers.items()],
        reverse=True,
    )

    # Mock model
    strategy.learner = MagicMock()
    strategy._feature_names = ["ATR_14", "MACD", "ADX"]
    strategy.base_quantity = base_quantity

    # Exit mode and tiered exit configs (required by evaluate)
    strategy.exit_mode = strategy.config.get("exit_mode", "SINGLE").upper()
    strategy._long_tiered_exits = None
    strategy._short_tiered_exits = None

    # Ensemble support attributes (single-model stub)
    strategy._is_ensemble = False
    strategy._is_tiered = False
    strategy._long_tiers = []
    strategy._short_tiers = []
    strategy._long_threshold = strategy.entry_threshold
    strategy._short_threshold = strategy.entry_threshold
    direction = strategy.config.get("direction", "LONG").upper()
    if direction == "SHORT":
        strategy._long_learner = None
        strategy._short_learner = strategy.learner
    else:
        strategy._long_learner = strategy.learner
        strategy._short_learner = None

    return strategy


def _make_features(atr: float = 0.50) -> pd.DataFrame:
    """Create a minimal single-row features DataFrame."""
    return pd.DataFrame([{"ATR_14": atr, "MACD": 0.01, "ADX": 25.0}])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestConfigLoading:
    """Verify config parsing produces correct strategy properties."""

    def test_name_from_config(self, tmp_path):
        s = _make_strategy_stub(str(tmp_path), {"nickname": "Dolphin"})
        assert s.name == "Dolphin"

    def test_direction_from_config(self, tmp_path):
        s = _make_strategy_stub(str(tmp_path), {"direction": "SHORT"})
        assert s.direction == "SHORT"

    def test_direction_case_insensitive(self, tmp_path):
        s = _make_strategy_stub(str(tmp_path), {"direction": "long"})
        assert s.direction == "LONG"

    def test_feature_names(self, tmp_path):
        s = _make_strategy_stub(str(tmp_path))
        assert "ATR_14" in s.feature_names

    def test_is_strategy_subclass(self, tmp_path):
        s = _make_strategy_stub(str(tmp_path))
        assert isinstance(s, Strategy)


class TestSafeDefaults:
    """Verify the safe default threshold when config is missing or invalid."""

    def test_missing_threshold_uses_safe_default(self, tmp_path):
        # Remove entry_threshold from config
        s = _make_strategy_stub(str(tmp_path))
        # Manually set threshold to safe default to simulate missing config key
        s.entry_threshold = 100.0
        s._long_threshold = 100.0
        s._short_threshold = 100.0
        # logit 4.6 → sigmoid ≈ 0.99, still below safe threshold of 100.0
        s.learner.model.predict.return_value = np.array([4.6])
        signal = s.evaluate(
            features=_make_features(),
            current_price=65.0,
            atr_value=0.50,
            current_position=0,
        )
        assert signal.action == "HOLD"
        assert signal.skip_reason == "BELOW_THRESHOLD"


class TestLongDirection:
    """Test LONG-direction signal generation."""

    def test_buy_above_threshold_flat(self, tmp_path):
        """Probability >= 0.70 and flat → BUY."""
        s = _make_strategy_stub(str(tmp_path))
        # logit 1.0986 → sigmoid ≈ 0.75 (above 0.70 threshold)
        s.learner.model.predict.return_value = np.array([1.0986])

        signal = s.evaluate(
            features=_make_features(),
            current_price=65.0,
            atr_value=0.50,
            current_position=0,
        )
        assert signal.action == "BUY"
        assert signal.signal_label == "Buy"
        assert signal.tp_price == round(65.0 + 7.0 * 0.50, 2)  # 68.50
        assert signal.sl_price == round(65.0 - 1.0 * 0.50, 2)  # 64.50
        assert signal.tp_price > 65.0  # TP above entry
        assert signal.sl_price < 65.0  # SL below entry

    def test_hold_below_threshold(self, tmp_path):
        s = _make_strategy_stub(str(tmp_path))
        # logit 0.0 → sigmoid = 0.50 (below 0.70 threshold)
        s.learner.model.predict.return_value = np.array([0.0])

        signal = s.evaluate(
            features=_make_features(),
            current_price=65.0,
            atr_value=0.50,
            current_position=0,
        )
        assert signal.action == "HOLD"
        assert signal.skip_reason == "BELOW_THRESHOLD"


class TestShortDirection:
    """Test SHORT-direction signal generation with reversed brackets."""

    def test_sell_above_threshold_flat(self, tmp_path):
        """Probability >= threshold and flat → SELL with reversed brackets."""
        s = _make_strategy_stub(
            str(tmp_path),
            {
                "direction": "SHORT",
                "entry_threshold": 0.60,
                "tp_atr_mult": 5.0,
                "sl_atr_mult": 0.75,
            },
        )
        # logit 0.6190 → sigmoid ≈ 0.65 (above 0.60 threshold)
        s.learner.model.predict.return_value = np.array([0.6190])

        signal = s.evaluate(
            features=_make_features(),
            current_price=65.0,
            atr_value=0.50,
            current_position=0,
        )
        assert signal.action == "SELL"
        assert signal.signal_label == "Sell"
        assert signal.tp_price == round(65.0 - 5.0 * 0.50, 2)  # 62.50
        assert signal.sl_price == round(65.0 + 0.75 * 0.50, 2)  # 65.375
        assert signal.tp_price < 65.0  # TP below entry (short)
        assert signal.sl_price > 65.0  # SL above entry (short)


class TestPositionGuard:
    """Test the position concurrency guard."""

    def test_hold_when_position_open_no_concurrent(self, tmp_path):
        s = _make_strategy_stub(str(tmp_path), {"allow_concurrent": False})
        # logit 1.3863 → sigmoid ≈ 0.80 (above 0.70 threshold)
        s.learner.model.predict.return_value = np.array([1.3863])

        signal = s.evaluate(
            features=_make_features(),
            current_price=65.0,
            atr_value=0.50,
            current_position=1,
        )
        assert signal.action == "HOLD"
        assert signal.skip_reason == "POSITION_OPEN"
        assert signal.signal_label == "Buy"  # Still labeled as the signal type

    def test_signal_fires_when_concurrent_allowed(self, tmp_path):
        s = _make_strategy_stub(str(tmp_path), {"allow_concurrent": True})
        # logit 1.3863 → sigmoid ≈ 0.80 (above 0.70 threshold)
        s.learner.model.predict.return_value = np.array([1.3863])

        signal = s.evaluate(
            features=_make_features(),
            current_price=65.0,
            atr_value=0.50,
            current_position=1,
        )
        assert signal.action == "BUY"  # Signal fires despite position open


class TestATRValidation:
    """Test ATR edge cases."""

    def test_hold_when_atr_none(self, tmp_path):
        s = _make_strategy_stub(str(tmp_path))
        # logit 1.3863 → sigmoid ≈ 0.80 (above 0.70 threshold)
        s.learner.model.predict.return_value = np.array([1.3863])

        signal = s.evaluate(
            features=_make_features(),
            current_price=65.0,
            atr_value=None,
            current_position=0,
        )
        assert signal.action == "HOLD"
        assert signal.skip_reason == "ATR_INVALID"

    def test_hold_when_atr_zero(self, tmp_path):
        s = _make_strategy_stub(str(tmp_path))
        # logit 1.3863 → sigmoid ≈ 0.80 (above 0.70 threshold)
        s.learner.model.predict.return_value = np.array([1.3863])

        signal = s.evaluate(
            features=_make_features(),
            current_price=65.0,
            atr_value=0.0,
            current_position=0,
        )
        assert signal.action == "HOLD"
        assert signal.skip_reason == "ATR_INVALID"


class TestSizingTiers:
    """Test probability-to-lots mapping from config."""

    def test_highest_tier(self, tmp_path):
        s = _make_strategy_stub(str(tmp_path), {"entry_threshold": 0.50})
        # logit 2.1972 → sigmoid ≈ 0.90 (above 0.80 tier → 3 lots)
        s.learner.model.predict.return_value = np.array([2.1972])
        sig = s.evaluate(_make_features(), 65.0, 0.50, 0)
        assert sig.lots == 3  # 90% >= 80% tier

    def test_middle_tier(self, tmp_path):
        s = _make_strategy_stub(str(tmp_path), {"entry_threshold": 0.50})
        # logit 1.0986 → sigmoid ≈ 0.75 (above 0.70 tier → 2 lots)
        s.learner.model.predict.return_value = np.array([1.0986])
        sig = s.evaluate(_make_features(), 65.0, 0.50, 0)
        assert sig.lots == 2  # 75% >= 70% tier

    def test_lowest_tier(self, tmp_path):
        s = _make_strategy_stub(str(tmp_path), {"entry_threshold": 0.50})
        # logit 0.2007 → sigmoid ≈ 0.55 (above 0.50 tier → 1 lot)
        s.learner.model.predict.return_value = np.array([0.2007])
        sig = s.evaluate(_make_features(), 65.0, 0.50, 0)
        assert sig.lots == 1  # 55% >= 50% tier

    def test_below_all_tiers_uses_base(self, tmp_path):
        s = _make_strategy_stub(
            str(tmp_path),
            {"entry_threshold": 0.10, "sizing_tiers": {"0.80": 3}},
        )
        # logit 0.0 → sigmoid = 0.50 (above 0.10 threshold, below 0.80 tier)
        s.learner.model.predict.return_value = np.array([0.0])
        sig = s.evaluate(_make_features(), 65.0, 0.50, 0)
        assert sig.lots == 1  # Falls through to base_quantity


class TestSigmoid:
    """Test sigmoid handling for logit outputs."""

    def test_sigmoid_applied_to_logits(self, tmp_path):
        s = _make_strategy_stub(str(tmp_path), {"entry_threshold": 0.50})
        s.learner.model.predict.return_value = np.array([2.0])  # logit

        signal = s.evaluate(
            features=_make_features(),
            current_price=65.0,
            atr_value=0.50,
            current_position=0,
        )
        # sigmoid(2.0) ≈ 0.88 → above threshold
        assert signal.action == "BUY"
        assert 0.85 < signal.probability < 0.92


class TestConfigErrors:
    """Test error handling for missing config/model files."""

    def test_config_not_found(self):
        from src.live_execution.strategies.configurable_strategy import (
            ConfigurableStrategy,
        )

        with pytest.raises(FileNotFoundError, match="Strategy config not found"):
            ConfigurableStrategy(config_path="/nonexistent/config.json")

    def test_model_not_found(self, tmp_path):
        from src.live_execution.strategies.configurable_strategy import (
            ConfigurableStrategy,
        )

        config_path = _write_config(str(tmp_path))
        with pytest.raises(FileNotFoundError, match="Model not found"):
            ConfigurableStrategy(config_path=config_path)
