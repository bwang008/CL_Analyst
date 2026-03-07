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

from src.data_paths import get_model_path as _dp_model_path


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
        self._direction: str = self.config.get("direction", "BOTH").upper()
        self.allow_concurrent: bool = self.config.get("allow_concurrent", False)

        # Detect ensemble vs single-model config
        self._is_ensemble: bool = "models" in self.config

        # Threshold
        if self._is_ensemble:
            # Ensemble: per-model thresholds; entry_threshold is a fallback
            models_cfg = self.config["models"]
            self._long_threshold: float = float(
                models_cfg.get("long", {}).get("threshold", 0.50)
            )
            self._short_threshold: float = float(
                models_cfg.get("short", {}).get("threshold", 0.50)
            )
            self.entry_threshold: float = min(
                self._long_threshold, self._short_threshold
            )
        else:
            raw_threshold = self.config.get("entry_threshold", None)
            if raw_threshold is None or not isinstance(raw_threshold, (int, float)):
                log.warning(
                    "[%s] CONFIG MISSING/INVALID: 'entry_threshold' "
                    "not found or not a number -- using safe default 100.0 "
                    "(no trades will fire)",
                    self._nickname,
                )
                self.entry_threshold = 100.0
            else:
                self.entry_threshold = float(raw_threshold)
            self._long_threshold = self.entry_threshold
            self._short_threshold = self.entry_threshold

        # ATR multipliers
        self.tp_atr_mult: float = float(self.config.get("tp_atr_mult", 2.0))
        self.sl_atr_mult: float = float(self.config.get("sl_atr_mult", 1.0))

        # Sizing tiers: {"0.80": 3, "0.70": 2, ...} → [(0.80, 3), (0.70, 2), ...]
        raw_tiers = self.config.get("sizing_tiers", {})
        self.sizing_tiers: list[tuple[float, int]] = sorted(
            [(float(k), int(v)) for k, v in raw_tiers.items()],
            reverse=True,
        )

        # Load model(s) from registry
        live_cfg = self.config.get("live_config", {})

        if self._is_ensemble:
            models_cfg = self.config["models"]
            # Load LONG model
            long_exp_id = (
                models_cfg.get("long", {}).get("experiment_id")
                or live_cfg.get("experiment_id")
            )
            if long_exp_id:
                self._long_learner = self._load_model(long_exp_id, "LONG")
            else:
                self._long_learner = None
                log.warning("[%s] No LONG model experiment_id — long signals disabled", self._nickname)

            # Load SHORT model
            short_exp_id = models_cfg.get("short", {}).get("experiment_id")
            if short_exp_id:
                self._short_learner = self._load_model(short_exp_id, "SHORT")
            else:
                self._short_learner = None
                log.warning("[%s] No SHORT model experiment_id — short signals disabled", self._nickname)

            # Use the long model's features as primary (both should share features)
            primary = self._long_learner or self._short_learner
            if primary is None:
                raise ValueError(f"[{self._nickname}] Ensemble config has no valid models")
            self.learner = primary
            self._feature_names: list[str] = primary.feature_names
        else:
            # Single-model: original behavior
            experiment_id = live_cfg.get(
                "experiment_id", self.config.get("experiment_id")
            )
            if experiment_id is None:
                raise ValueError(
                    f"[{self._nickname}] Config missing 'experiment_id' in both "
                    f"'live_config' and top-level."
                )
            self.learner = self._load_model(experiment_id, self._direction)
            self._feature_names = self.learner.feature_names
            self._long_learner = self.learner if self._direction != "SHORT" else None
            self._short_learner = self.learner if self._direction != "LONG" else None

        log.info(
            "[%s] Config: direction=%s  long_thresh=%.2f  short_thresh=%.2f  "
            "TP=%.1fx  SL=%.1fx  concurrent=%s  tiers=%s  ensemble=%s",
            self._nickname,
            self._direction,
            self._long_threshold,
            self._short_threshold,
            self.tp_atr_mult,
            self.sl_atr_mult,
            self.allow_concurrent,
            self.sizing_tiers,
            self._is_ensemble,
        )

        self.base_quantity = base_quantity

    def _load_model(self, experiment_id: str, label: str) -> LGBMLearner:
        """Load a LGBMLearner from the model registry."""
        model_dir = _dp_model_path(f"registry/{experiment_id}")
        model_path = model_dir / "final_model.pkl"
        if not model_path.exists():
            raise FileNotFoundError(
                f"Model not found: {model_path} "
                f"(experiment_id={experiment_id})"
            )
        log.info("[%s] Loading %s model from %s", self._nickname, label, model_path)
        learner = LGBMLearner.__new__(LGBMLearner)
        learner.load(str(model_path))
        log.info(
            "[%s] %s model loaded: %d features",
            self._nickname, label, len(learner.feature_names),
        )
        return learner

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

    def _run_inference(self, learner: LGBMLearner, features: pd.DataFrame) -> float:
        """Run model inference and return a probability."""
        raw_pred = learner.model.predict(features)
        raw_val = float(np.asarray(raw_pred).ravel()[0])
        if raw_val < 0 or raw_val > 1:
            return _sigmoid(raw_val)
        return raw_val

    def evaluate(
        self,
        features: pd.DataFrame,
        current_price: float,
        atr_value: Optional[float],
        current_position: int,
    ) -> TradeSignal:
        """Run inference and return a TradeSignal."""
        # 1. Run inference on available models
        buy_prob = 0.0
        sell_prob = 0.0
        if self._long_learner is not None:
            buy_prob = self._run_inference(self._long_learner, features)
        if self._short_learner is not None:
            sell_prob = self._run_inference(self._short_learner, features)

        # 2. Determine which signals pass their thresholds
        buy_ok = buy_prob >= self._long_threshold and self._long_learner is not None
        sell_ok = sell_prob >= self._short_threshold and self._short_learner is not None

        # Pick the winning signal
        if buy_ok and sell_ok:
            # Conflict: higher probability wins (conservative)
            if buy_prob >= sell_prob:
                probability = buy_prob
                active_label = "Buy"
                active_action = "BUY"
            else:
                probability = sell_prob
                active_label = "Sell"
                active_action = "SELL"
        elif buy_ok:
            probability = buy_prob
            active_label = "Buy"
            active_action = "BUY"
        elif sell_ok:
            probability = sell_prob
            active_label = "Sell"
            active_action = "SELL"
        else:
            # Neither passes threshold — use higher prob for display
            probability = max(buy_prob, sell_prob)
            return TradeSignal(
                action="HOLD",
                probability=probability,
                confidence_pct=probability * 100.0,
                signal_label="Hold",
                skip_reason="BELOW_THRESHOLD",
                buy_prob=buy_prob,
                sell_prob=sell_prob,
            )

        confidence_pct = probability * 100.0

        # 3. Position check
        if not self.allow_concurrent and current_position != 0:
            return TradeSignal(
                action="HOLD",
                probability=probability,
                confidence_pct=confidence_pct,
                signal_label=active_label,
                skip_reason="POSITION_OPEN",
                buy_prob=buy_prob,
                sell_prob=sell_prob,
            )

        # 4. ATR validation
        if atr_value is None or atr_value <= 0:
            return TradeSignal(
                action="HOLD",
                probability=probability,
                confidence_pct=confidence_pct,
                signal_label=active_label,
                skip_reason="ATR_INVALID",
                buy_prob=buy_prob,
                sell_prob=sell_prob,
            )

        # 5. Compute bracket levels (direction-aware)
        if active_action == "SELL":
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
            buy_prob=buy_prob,
            sell_prob=sell_prob,
        )

    # -- Internal helpers ----------------------------------------------------

    def _prob_to_lots(self, probability: float) -> int:
        """Map model probability to lot count using config sizing tiers."""
        for min_prob, lots in self.sizing_tiers:
            if probability >= min_prob:
                return lots
        return self.base_quantity

