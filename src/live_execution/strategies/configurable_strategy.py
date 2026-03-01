"""
ConfigurableStrategy — Config-driven universal strategy for live and backtest.

Loads all decision parameters (threshold, direction, TP/SL multipliers,
sizing tiers) from a JSON configuration file and a model from the
models/registry/ folder.  This eliminates simulation-to-live divergence
by letting the backtester and live trader share the EXACT same strategy
object.

Creating a new strategy is a JSON-only operation: drop a file into
configs/strategies/ and point it at a model registry experiment.

Config schema (see configs/strategies/manatee.json for reference):
    {
        "nickname":         str   — Human-readable strategy name,
        "experiment_id":    str   — Registry folder name (e.g. "EXP-017_S_Ultimate"),
        "direction":        str   — "LONG", "SHORT", or "BOTH",
        "allow_concurrent": bool  — Allow entry while a position is open,
        "entry_threshold":  float — Min probability to trigger an entry,
        "tp_atr_mult":      float — ATR multiplier for take-profit,
        "sl_atr_mult":      float — ATR multiplier for stop-loss,
        "sizing_tiers":     dict  — {min_probability_str: lots_int, ...}
    }
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

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent


def _sigmoid(x: float) -> float:
    """Apply sigmoid to convert logit to probability."""
    return 1.0 / (1.0 + np.exp(-x))


class ConfigurableStrategy(Strategy):
    """Universal config-driven strategy.

    Decision logic mirrors Buy70SizedManatee but all parameters are
    read from a JSON config file:
        1. Run LightGBM inference → sigmoid → probability
        2. If probability >= entry_threshold AND position guard passes → signal
        3. Bracket: direction-aware TP/SL using ATR multipliers
        4. Lot sizing via config-defined tiers
    """

    def __init__(
        self,
        *,
        config_path: str,
        base_quantity: int = 1,
    ) -> None:
        # Load strategy config
        config_file = Path(config_path)
        if not config_file.exists():
            raise FileNotFoundError(
                f"Strategy config not found: {config_path}"
            )
        with open(config_file) as f:
            self.config: dict = json.load(f)

        self._nickname: str = self.config["nickname"]
        self._direction: str = self.config["direction"].upper()
        self.allow_concurrent: bool = self.config.get("allow_concurrent", False)

        # Threshold (safe default = 100.0 → no trades fire)
        raw_threshold = self.config.get("entry_threshold", None)
        if raw_threshold is None or not isinstance(raw_threshold, (int, float)):
            log.warning(
                "[%s] CONFIG MISSING/INVALID: 'entry_threshold' "
                "not found or not a number — using safe default 100.0 "
                "(no trades will fire)",
                self._nickname,
            )
            self.entry_threshold: float = 100.0
        else:
            self.entry_threshold = float(raw_threshold)

        # ATR multipliers
        self.tp_atr_mult: float = float(self.config.get("tp_atr_mult", 2.0))
        self.sl_atr_mult: float = float(self.config.get("sl_atr_mult", 1.0))

        # Sizing tiers: {"0.80": 3, "0.70": 2, ...} → [(0.80, 3), (0.70, 2), ...]
        raw_tiers = self.config.get("sizing_tiers", {})
        self.sizing_tiers: list[tuple[float, int]] = sorted(
            [(float(k), int(v)) for k, v in raw_tiers.items()],
            reverse=True,
        )

        # Load model from registry
        experiment_id = self.config["experiment_id"]
        model_dir = _PROJECT_ROOT / "models" / "registry" / experiment_id
        model_path = model_dir / "final_model.pkl"
        if not model_path.exists():
            raise FileNotFoundError(
                f"Model not found: {model_path} "
                f"(experiment_id={experiment_id})"
            )
        log.info("[%s] Loading model from %s", self._nickname, model_path)
        self.learner = LGBMLearner.__new__(LGBMLearner)
        self.learner.load(str(model_path))
        self._feature_names: list[str] = self.learner.feature_names
        log.info(
            "[%s] Model loaded: %d features", self._nickname, len(self._feature_names)
        )

        log.info(
            "[%s] Config: direction=%s  threshold=%.2f  TP=%.1fx  SL=%.1fx  "
            "concurrent=%s  tiers=%s",
            self._nickname,
            self._direction,
            self.entry_threshold,
            self.tp_atr_mult,
            self.sl_atr_mult,
            self.allow_concurrent,
            self.sizing_tiers,
        )

        self.base_quantity = base_quantity

    # -- Strategy interface --------------------------------------------------

    @property
    def name(self) -> str:
        return self._nickname

    @property
    def direction(self) -> str:
        return self._direction

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

        # Determine action label based on direction
        if self._direction == "SHORT":
            active_label = "Sell"
            active_action = "SELL"
        else:
            active_label = "Buy"
            active_action = "BUY"

        # 2. Threshold check
        if probability < self.entry_threshold:
            return TradeSignal(
                action="HOLD",
                probability=probability,
                confidence_pct=confidence_pct,
                signal_label="Hold",
                skip_reason="BELOW_THRESHOLD",
            )

        # 3. Position check
        if not self.allow_concurrent and current_position != 0:
            return TradeSignal(
                action="HOLD",
                probability=probability,
                confidence_pct=confidence_pct,
                signal_label=active_label,
                skip_reason="POSITION_OPEN",
            )

        # 4. ATR validation
        if atr_value is None or atr_value <= 0:
            return TradeSignal(
                action="HOLD",
                probability=probability,
                confidence_pct=confidence_pct,
                signal_label=active_label,
                skip_reason="ATR_INVALID",
            )

        # 5. Compute bracket levels (direction-aware)
        if self._direction == "SHORT":
            tp_price = round(current_price - self.tp_atr_mult * atr_value, 2)
            sl_price = round(current_price + self.sl_atr_mult * atr_value, 2)
        else:
            tp_price = round(current_price + self.tp_atr_mult * atr_value, 2)
            sl_price = round(current_price - self.sl_atr_mult * atr_value, 2)

        # 6. Position sizing
        lots = self._prob_to_lots(probability)

        return TradeSignal(
            action=active_action,
            probability=probability,
            confidence_pct=confidence_pct,
            tp_price=tp_price,
            sl_price=sl_price,
            lots=lots,
            signal_label=active_label,
        )

    # -- Internal helpers ----------------------------------------------------

    def _prob_to_lots(self, probability: float) -> int:
        """Map model probability to lot count using config sizing tiers."""
        for min_prob, lots in self.sizing_tiers:
            if probability >= min_prob:
                return lots
        return self.base_quantity
