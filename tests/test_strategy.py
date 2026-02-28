"""
Tests for the Strategy abstraction and Buy70_Sized_Manatee strategy.

All tests use mocks — no real model or IBKR connection needed.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch, PropertyMock

import numpy as np
import pandas as pd
import pytest

from src.live_execution.strategy import Strategy, TradeSignal


# ---------------------------------------------------------------------------
# Helpers — build a Buy70SizedManatee without loading a real model
# ---------------------------------------------------------------------------


def _make_manatee_stub(*, threshold: float = 0.70, base_quantity: int = 1):
    """Create a Buy70SizedManatee without hitting disk for model/config."""
    from src.live_execution.strategies.buy70_sized_manatee import Buy70SizedManatee

    strategy = object.__new__(Buy70SizedManatee)
    strategy.learner = MagicMock()
    strategy._feature_names = ["ATR_14", "MACD", "ADX"]
    strategy.probability_threshold = threshold
    strategy.base_quantity = base_quantity
    return strategy


def _make_features(atr: float = 0.50) -> pd.DataFrame:
    """Create a minimal single-row features DataFrame."""
    return pd.DataFrame([{"ATR_14": atr, "MACD": 0.01, "ADX": 25.0}])


# ---------------------------------------------------------------------------
# TradeSignal tests
# ---------------------------------------------------------------------------


class TestTradeSignal:
    """Verify TradeSignal dataclass defaults and construction."""

    def test_default_action_is_hold(self):
        s = TradeSignal(action="HOLD")
        assert s.action == "HOLD"
        assert s.signal_label == "Hold"
        assert s.skip_reason is None

    def test_buy_signal_fields(self):
        s = TradeSignal(
            action="BUY",
            probability=0.85,
            confidence_pct=85.0,
            tp_price=70.0,
            sl_price=64.0,
            lots=3,
            signal_label="Buy",
        )
        assert s.action == "BUY"
        assert s.lots == 3
        assert s.tp_price == 70.0

    def test_sell_signal_fields(self):
        s = TradeSignal(
            action="SELL",
            probability=0.75,
            confidence_pct=75.0,
            tp_price=60.0,
            sl_price=70.0,
            lots=2,
            signal_label="Sell",
        )
        assert s.action == "SELL"
        assert s.tp_price < s.sl_price  # reversed for short


# ---------------------------------------------------------------------------
# Buy70SizedManatee tests
# ---------------------------------------------------------------------------


class TestBuy70SizedManatee:
    """Test the extracted buy strategy logic."""

    def test_name_and_direction(self):
        s = _make_manatee_stub()
        assert s.name == "Buy70_Sized_Manatee"
        assert s.direction == "LONG"

    def test_feature_names(self):
        s = _make_manatee_stub()
        assert "ATR_14" in s.feature_names

    def test_hold_below_threshold(self):
        """Probability below 0.70 → HOLD."""
        s = _make_manatee_stub(threshold=0.70)
        s.learner.model.predict.return_value = np.array([0.50])

        signal = s.evaluate(
            features=_make_features(),
            current_price=65.0,
            atr_value=0.50,
            current_position=0,
        )
        assert signal.action == "HOLD"
        assert signal.skip_reason == "BELOW_THRESHOLD"

    def test_buy_above_threshold_flat(self):
        """Probability >= 0.70 and flat → BUY."""
        s = _make_manatee_stub(threshold=0.70)
        s.learner.model.predict.return_value = np.array([0.75])

        signal = s.evaluate(
            features=_make_features(),
            current_price=65.0,
            atr_value=0.50,
            current_position=0,
        )
        assert signal.action == "BUY"
        assert signal.tp_price == round(65.0 + 7.0 * 0.50, 2)  # 68.50
        assert signal.sl_price == round(65.0 - 1.0 * 0.50, 2)  # 64.50
        assert signal.lots == 2  # 70%+ → 2 lots

    def test_hold_when_position_open(self):
        """Signal but already holding → HOLD with POSITION_OPEN."""
        s = _make_manatee_stub(threshold=0.70)
        s.learner.model.predict.return_value = np.array([0.80])

        signal = s.evaluate(
            features=_make_features(),
            current_price=65.0,
            atr_value=0.50,
            current_position=1,  # already long
        )
        assert signal.action == "HOLD"
        assert signal.skip_reason == "POSITION_OPEN"
        # signal_label should still say Buy (it's a buy signal, just skipped)
        assert signal.signal_label == "Buy"

    def test_hold_when_atr_invalid(self):
        """Valid signal but ATR is None → HOLD."""
        s = _make_manatee_stub(threshold=0.70)
        s.learner.model.predict.return_value = np.array([0.80])

        signal = s.evaluate(
            features=_make_features(),
            current_price=65.0,
            atr_value=None,
            current_position=0,
        )
        assert signal.action == "HOLD"
        assert signal.skip_reason == "ATR_INVALID"

    def test_hold_when_atr_zero(self):
        """Valid signal but ATR is 0 → HOLD."""
        s = _make_manatee_stub(threshold=0.70)
        s.learner.model.predict.return_value = np.array([0.80])

        signal = s.evaluate(
            features=_make_features(),
            current_price=65.0,
            atr_value=0.0,
            current_position=0,
        )
        assert signal.action == "HOLD"
        assert signal.skip_reason == "ATR_INVALID"

    def test_sigmoid_applied_to_logits(self):
        """When model outputs a logit (outside 0-1), sigmoid is applied."""
        s = _make_manatee_stub(threshold=0.50)
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

    def test_sizing_tiers(self):
        """Verify lot sizing at different probability levels."""
        s = _make_manatee_stub(threshold=0.50)

        # 90% → 3 lots
        s.learner.model.predict.return_value = np.array([0.90])
        sig = s.evaluate(_make_features(), 65.0, 0.50, 0)
        assert sig.lots == 3

        # 75% → 2 lots
        s.learner.model.predict.return_value = np.array([0.75])
        sig = s.evaluate(_make_features(), 65.0, 0.50, 0)
        assert sig.lots == 2

        # 55% → 1 lot
        s.learner.model.predict.return_value = np.array([0.55])
        sig = s.evaluate(_make_features(), 65.0, 0.50, 0)
        assert sig.lots == 1

    def test_safe_default_threshold(self):
        """When threshold is 100.0, no trades should ever fire."""
        s = _make_manatee_stub(threshold=100.0)
        s.learner.model.predict.return_value = np.array([0.99])

        signal = s.evaluate(
            features=_make_features(),
            current_price=65.0,
            atr_value=0.50,
            current_position=0,
        )
        assert signal.action == "HOLD"
        assert signal.skip_reason == "BELOW_THRESHOLD"

    def test_bracket_direction_is_long(self):
        """TP should be above entry, SL below (LONG direction)."""
        s = _make_manatee_stub(threshold=0.50)
        s.learner.model.predict.return_value = np.array([0.80])

        signal = s.evaluate(
            features=_make_features(atr=1.0),
            current_price=65.0,
            atr_value=1.0,
            current_position=0,
        )
        assert signal.tp_price > 65.0  # TP above entry
        assert signal.sl_price < 65.0  # SL below entry
