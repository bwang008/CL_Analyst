"""
Buy70_Sized_Manatee — Long-only strategy with tiered position sizing.

Naming convention:  {Direction}{Threshold}_{Sizing}_{Nickname}
  - Direction:  Buy   (long entries only)
  - Threshold:  70    (probability >= 0.70 to trigger)
  - Sizing:     Sized (probability-based lot tiers)
  - Nickname:   Manatee

This is the original S_Ultimate (EXP-017) decision logic, extracted
from LiveTrader._on_new_bar so it can be swapped with other strategies.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from src.LGBMLearner import LGBMLearner
from src.live_execution.strategy import Strategy, TradeSignal

log = logging.getLogger("LiveTrader")

# ---------------------------------------------------------------------------
# Constants (previously module-level in live_trader.py)
# ---------------------------------------------------------------------------

_TP_ATR_MULT = 7.0  # Optimized via backtest sweep — PF 2.99 at t=0.70
_SL_ATR_MULT = 1.0

# Probability-based position sizing tiers (highest first)
_SIZING_TIERS: list[tuple[float, int]] = [
    (0.80, 3),  # 80%+ confidence → 3 lots
    (0.70, 2),  # 70%+ confidence → 2 lots
    (0.60, 2),  # 60%+ confidence → 2 lots
    (0.50, 1),  # 50%+ confidence → 1 lot
]

# Default paths
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent

from src.data_paths import get_model_path as _dp_model_path

_DEFAULT_MODEL_PATH = str(
    _dp_model_path("registry/EXP-017_S_Ultimate/final_model.pkl")
)
_DEFAULT_CONFIG_PATH = str(
    _dp_model_path("registry/EXP-017_S_Ultimate/config.json")
)


def _sigmoid(x: float) -> float:
    """Apply sigmoid to convert logit to probability."""
    return 1.0 / (1.0 + np.exp(-x))


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------


class Buy70SizedManatee(Strategy):
    """Long-only strategy with 0.70 threshold and tiered position sizing.

    Decision logic:
        1. Run LightGBM inference → sigmoid → probability
        2. If probability >= threshold AND flat → BUY signal
        3. Bracket: TP = price + 7×ATR, SL = price - 1×ATR
        4. Lot sizing: 80%→3, 70%→2, 60%→2, 50%→1
    """

    def __init__(
        self,
        *,
        model_path: str = _DEFAULT_MODEL_PATH,
        config_path: str = _DEFAULT_CONFIG_PATH,
        base_quantity: int = 1,
    ) -> None:
        # Load model
        log.info("[%s] Loading model from %s", self.name, model_path)
        self.learner = LGBMLearner.__new__(LGBMLearner)
        self.learner.load(model_path)
        self._feature_names: list[str] = self.learner.feature_names
        log.info("[%s] Model loaded: %d features", self.name, len(self._feature_names))

        # Load config for threshold
        with open(config_path) as f:
            config = json.load(f)
        raw_threshold = config.get("optimized_probability_threshold", None)
        if raw_threshold is None or not isinstance(raw_threshold, (int, float)):
            log.warning(
                "[%s] CONFIG MISSING/INVALID: 'optimized_probability_threshold' "
                "not found or not a number in %s — using safe default 100.0 "
                "(no trades will fire)",
                self.name, config_path,
            )
            self.probability_threshold: float = 100.0
        else:
            self.probability_threshold: float = float(raw_threshold)
        log.info("[%s] Probability threshold: %.2f", self.name, self.probability_threshold)

        self.base_quantity = base_quantity

    # -- Strategy interface --------------------------------------------------

    @property
    def name(self) -> str:
        return "Buy70_Sized_Manatee"

    @property
    def direction(self) -> str:
        return "LONG"

    @property
    def feature_names(self) -> list[str]:
        return self._feature_names

    def evaluate(
        self,
        features: pd.DataFrame,
        current_price: float,
        atr_value: Optional[float],
        current_position: int,
    ) -> TradeSignal:
        """Run inference and return a TradeSignal."""
        # 1. Inference
        raw_pred = self.learner.model.predict(features)
        raw_val = float(np.asarray(raw_pred).ravel()[0])

        if raw_val < 0 or raw_val > 1:
            probability = _sigmoid(raw_val)
        else:
            probability = raw_val

        confidence_pct = probability * 100.0

        # 2. Threshold check
        if probability < self.probability_threshold:
            return TradeSignal(
                action="HOLD",
                probability=probability,
                confidence_pct=confidence_pct,
                signal_label="Hold",
                skip_reason="BELOW_THRESHOLD",
            )

        # 3. Position check — only enter if flat
        if current_position != 0:
            return TradeSignal(
                action="HOLD",
                probability=probability,
                confidence_pct=confidence_pct,
                signal_label="Buy",
                skip_reason="POSITION_OPEN",
            )

        # 4. ATR validation
        if atr_value is None or atr_value <= 0:
            return TradeSignal(
                action="HOLD",
                probability=probability,
                confidence_pct=confidence_pct,
                signal_label="Buy",
                skip_reason="ATR_INVALID",
            )

        # 5. Compute bracket levels (LONG direction)
        tp_price = round(current_price + _TP_ATR_MULT * atr_value, 2)
        sl_price = round(current_price - _SL_ATR_MULT * atr_value, 2)

        # 6. Position sizing
        lots = self._prob_to_lots(probability)

        return TradeSignal(
            action="BUY",
            probability=probability,
            confidence_pct=confidence_pct,
            tp_price=tp_price,
            sl_price=sl_price,
            lots=lots,
            signal_label="Buy",
        )

    # -- Internal helpers ----------------------------------------------------

    def _prob_to_lots(self, probability: float) -> int:
        """Map model probability to lot count using sizing tiers."""
        for min_prob, lots in _SIZING_TIERS:
            if probability >= min_prob:
                return lots
        return self.base_quantity
