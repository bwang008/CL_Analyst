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
        "experiment_id":    str   — Registry folder name (e.g. "HS11_3x1_6H"),
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
from src.live_execution.strategies.execution_models import create_execution_strategy, EngineState
from src.live_execution.execution_guard import ExecutionGuard


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
        from src.live_execution.config_loader import load_strategy_config
        self.config: dict = load_strategy_config(config_file)

        self._nickname: str = self.config["nickname"]
        self._direction: str = self.config.get("direction", "BOTH").upper()
        self.allow_concurrent: bool = self.config.get("allow_concurrent", False)

        # Detect config mode: tiered > ensemble > single-model
        self._is_tiered: bool = (
            "long" in self.config
            and isinstance(self.config["long"], dict)
            and "tiers" in self.config.get("long", {})
        )
        self._is_ensemble: bool = "models" in self.config

        # Internal state for execution strategy cooldowns
        self._last_exit_bars_ago_long: int = 9999
        self._last_exit_bars_ago_short: int = 9999

        # ── Tiered config ─────────────────────────────────────────────
        if self._is_tiered:
            self._direction = "BOTH"
            long_cfg = self.config.get("long", {})
            short_cfg = self.config.get("short", {})

            # Parse tiers: sorted highest min_prob first
            self._long_tiers: list[dict] = sorted(
                long_cfg.get("tiers", []),
                key=lambda t: float(t.get("min_prob", 0)),
                reverse=True,
            )
            self._short_tiers: list[dict] = sorted(
                short_cfg.get("tiers", []),
                key=lambda t: float(t.get("min_prob", 0)),
                reverse=True,
            )

            # Thresholds = lowest tier min_prob on each side
            self._long_threshold: float = float(
                self._long_tiers[-1]["min_prob"]
            ) if self._long_tiers else 1.0
            self._short_threshold: float = float(
                self._short_tiers[-1]["min_prob"]
            ) if self._short_tiers else 1.0
            self.entry_threshold: float = min(
                self._long_threshold, self._short_threshold
            )

            log.info(
                "[%s] Tiered config: %d long tiers (min=%.2f), "
                "%d short tiers (min=%.2f)",
                self._nickname,
                len(self._long_tiers), self._long_threshold,
                len(self._short_tiers), self._short_threshold,
            )

        # ── Ensemble config (no tiers) ────────────────────────────────
        elif self._is_ensemble:
            self._long_tiers = []
            self._short_tiers = []
            models_cfg = self.config["models"]

            # Without tiers, models.<side>.threshold IS the execution source
            # of truth and must be explicit — no silent 0.50 default (ticket
            # threshold-min-prob-consolidation_07222026_1230; tiered configs
            # derive from tiers[].min_prob and may omit it entirely). A side
            # absent from `models` can never fire: fail-closed 1.0, same as
            # the tiered branch's missing-side sentinel.
            def _side_threshold(side: str) -> float:
                side_cfg = models_cfg.get(side) or {}
                if not side_cfg:
                    return 1.0
                if "threshold" not in side_cfg:
                    raise ValueError(
                        f"[{self._nickname}] non-tiered ensemble config must "
                        f"set models.{side}.threshold explicitly (no silent "
                        f"default); tiered configs use tiers[].min_prob"
                    )
                return float(side_cfg["threshold"])

            self._long_threshold = _side_threshold("long")
            self._short_threshold = _side_threshold("short")
            self.entry_threshold = min(
                self._long_threshold, self._short_threshold
            )

        # ── Single-model config ───────────────────────────────────────
        else:
            self._long_tiers = []
            self._short_tiers = []
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

        # Exit Strategy logic
        self.exit_mode: str = self.config.get("exit_mode", "SINGLE").upper()
        
        if self.exit_mode == "SINGLE":
            self.tp_atr_mult: float = float(self.config.get("tp_atr_mult", 2.0))
        else:
            self.tp_atr_mult: float = 2.0  # Fallback for evaluate placeholder logic

        # Stop-Loss remains a single global/tiered float
        self.sl_atr_mult: float = float(self.config.get("sl_atr_mult", 1.0))

        # Configurable fractional exits for TIERED mode
        self._long_tiered_exits = self.config.get("long", {}).get("tiered_exits")
        self._short_tiered_exits = self.config.get("short", {}).get("tiered_exits")

        # Sizing tiers: {"0.80": 3, "0.70": 2, ...} → [(0.80, 3), (0.70, 2), ...]
        raw_tiers = self.config.get("sizing_tiers", {})
        self.sizing_tiers: list[tuple[float, int]] = sorted(
            [(float(k), int(v)) for k, v in raw_tiers.items()],
            reverse=True,
        )

        # Load model(s) from registry
        live_cfg = self.config.get("live_config", {})

        if self._is_tiered:
            # Check models block first, fallback to root long/short for backward compat
            models_cfg = self.config.get("models", {})
            long_model_cfg = models_cfg.get("long") or self.config.get("long", {})
            short_model_cfg = models_cfg.get("short") or self.config.get("short", {})
            
            long_exp_id = (
                long_model_cfg.get("experiment_id")
                or live_cfg.get("experiment_id")
            )
            short_exp_id = short_model_cfg.get("experiment_id")

            long_model_path = long_model_cfg.get("model_path")
            short_model_path = short_model_cfg.get("model_path")

            if long_exp_id:
                self._long_learner = self._load_model(
                    long_exp_id, "LONG", model_path=long_model_path
                )
            else:
                self._long_learner = None
                log.warning("[%s] No LONG model experiment_id — long signals disabled", self._nickname)

            if short_exp_id:
                self._short_learner = self._load_model(
                    short_exp_id, "SHORT", model_path=short_model_path
                )
            else:
                self._short_learner = None
                log.warning("[%s] No SHORT model experiment_id — short signals disabled", self._nickname)

            primary = self._long_learner or self._short_learner
            if primary is None:
                raise ValueError(f"[{self._nickname}] Tiered config has no valid models")
            self.learner = primary
            self._feature_names: list[str] = primary.feature_names

        elif self._is_ensemble:
            models_cfg = self.config["models"]
            long_cfg_entry = models_cfg.get("long", {})
            long_exp_id = (
                long_cfg_entry.get("experiment_id")
                or live_cfg.get("experiment_id")
            )
            long_model_path = long_cfg_entry.get("model_path")
            if long_exp_id:
                self._long_learner = self._load_model(
                    long_exp_id, "LONG", model_path=long_model_path,
                )
            else:
                self._long_learner = None
                log.warning("[%s] No LONG model experiment_id — long signals disabled", self._nickname)

            short_cfg_entry = models_cfg.get("short", {})
            short_exp_id = short_cfg_entry.get("experiment_id")
            short_model_path = short_cfg_entry.get("model_path")
            if short_exp_id:
                self._short_learner = self._load_model(
                    short_exp_id, "SHORT", model_path=short_model_path,
                )
            else:
                self._short_learner = None
                log.warning("[%s] No SHORT model experiment_id — short signals disabled", self._nickname)

            primary = self._long_learner or self._short_learner
            if primary is None:
                raise ValueError(f"[{self._nickname}] Ensemble config has no valid models")
            self.learner = primary
            self._feature_names = primary.feature_names
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

        mode_str = "tiered" if self._is_tiered else ("ensemble" if self._is_ensemble else "single")
        log.info(
            "[%s] Config: mode=%s  direction=%s  long_thresh=%.2f  short_thresh=%.2f  "
            "TP=%.1fx  SL=%.1fx  concurrent=%s  sizing_tiers=%s",
            self._nickname,
            mode_str,
            self._direction,
            self._long_threshold,
            self._short_threshold,
            self.tp_atr_mult,
            self.sl_atr_mult,
            self.allow_concurrent,
            self.sizing_tiers,
        )

        self.base_quantity = base_quantity

        # ── Execution Strategy ──────────────────────────────────────────
        self._exec_strategy = create_execution_strategy(self.config)

        # ── Execution Guard ─────────────────────────────────────────────
        if self.config.get("blocked_entry_hours_est") or self.config.get("block_long_weekends") or self.config.get("blocked_entry_hours_by_day"):
            self._execution_guard = ExecutionGuard(self.config)
            log.info("[%s] ExecutionGuard active: blocked_hours=%s, blocked_hours_by_day=%s, block_long_weekends=%s",
                     self._nickname,
                     self.config.get('blocked_entry_hours_est', []),
                     self.config.get('blocked_entry_hours_by_day', {}),
                     self.config.get('block_long_weekends', False))
        else:
            self._execution_guard = None
            log.warning(
                "[%s] ExecutionGuard is DISABLED — no blocked_entry_hours_est, "
                "blocked_entry_hours_by_day, or block_long_weekends found in config. "
                "Trades will NOT be blocked during toxic hours. "
                "Check that configs/global_risk_filters.json exists and is being loaded.",
                self._nickname,
            )

    def _load_model(self, experiment_id: str, label: str, model_path: str | None = None) -> LGBMLearner:
        """Load a LGBMLearner from the model registry or a direct path.

        Args:
            experiment_id: Registry folder name (e.g. "HS11_3x1_6H").
            label: Human-readable label for logging ("LONG" or "SHORT").
            model_path: Optional direct path to the model PKL file.
                If provided, used instead of the registry path.
        """
        if model_path:
            # Direct path mode — resolve relative to project root
            path = Path(model_path)
            if not path.is_absolute():
                path = _PROJECT_ROOT / path
            if not path.exists():
                raise FileNotFoundError(
                    f"Model not found: {path} "
                    f"(model_path={model_path})"
                )
            log.info("[%s] Loading %s model from %s", self._nickname, label, path)
        else:
            # Registry mode
            model_dir = _dp_model_path(f"registry/{experiment_id}")
            path = model_dir / "final_model.pkl"
            if not path.exists():
                raise FileNotFoundError(
                    f"Model not found: {path} "
                    f"(experiment_id={experiment_id})"
                )
            log.info("[%s] Loading %s model from %s", self._nickname, label, path)
        learner = LGBMLearner.__new__(LGBMLearner)
        learner.load(str(path))
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
        """Run model inference and return a probability.

        Models trained with custom focal loss (saved as _pure.txt) always
        return raw logits from booster.predict(), even when values fall in
        [0, 1].  Unconditional sigmoid is required to convert to probability.
        Verified by tmp/test_prob_calibration.py (0.000000 error vs OOS).
        """
        raw_pred = learner.model.predict(features)
        raw_val = float(np.asarray(raw_pred).ravel()[0])
        return _sigmoid(raw_val)

    def evaluate(
        self,
        features: pd.DataFrame,
        current_price: float,
        atr_value: Optional[float],
        current_position: int,
        *,
        atr_value_long: Optional[float] = None,
        atr_value_short: Optional[float] = None,
    ) -> TradeSignal:
        """Run inference and return a TradeSignal."""
        # 1. Run inference on available models
        buy_prob = 0.0
        sell_prob = 0.0
        if self._long_learner is not None:
            buy_prob = self._run_inference(self._long_learner, features)
        if self._short_learner is not None:
            sell_prob = self._run_inference(self._short_learner, features)

        # 2. Delegate decision to the execution strategy
        side = 0
        if current_position > 0:
            side = 1
        elif current_position < 0:
            side = -1

        # ExecutionGuard: block new entries during toxic periods
        dt = pd.Timestamp.now(tz="America/New_York")
        if current_position == 0 and self._execution_guard:
            if not self._execution_guard.is_entry_allowed(dt):
                return TradeSignal(
                    action="HOLD",
                    probability=max(buy_prob, sell_prob),
                    confidence_pct=max(buy_prob, sell_prob) * 100.0,
                    signal_label="Hold",
                    skip_reason="EXECUTION_GUARD",
                    buy_prob=buy_prob,
                    sell_prob=sell_prob,
                )

        # Update cooldown counters BEFORE the gate (backtest start-of-bar
        # convention: increment at the top of the bar loop, reset to 0 in
        # _close_trade — the gate reads the post-increment value).
        if current_position == 0:
            self._last_exit_bars_ago_long += 1
            self._last_exit_bars_ago_short += 1
        elif current_position > 0:
            self._last_exit_bars_ago_short += 1
        elif current_position < 0:
            self._last_exit_bars_ago_long += 1

        # Cooldown gate — the SOLE live authority, exactly mirroring the
        # backtest (cooldown-single-authority-wiring_07222026_1051): the
        # backtest enforces ONLY the flavor-blind per-side ``cooldown_bars``
        # (TieredEnsembleStrategy re-gate, resolution side_cfg -> top-level
        # -> 0; armed by _close_trade for EVERY exit reason). The flavored
        # sl/tp_cooldown_bars pair was removed from the engine in 3d95040
        # (2026-05-12) and is dead vocabulary — enforcing it here made live
        # stricter than the backtest wherever the hand-template 7 exceeded
        # the Optuna-searched per-side value.
        if current_position == 0:
            long_cfg = self.config.get("long", {})
            long_cooldown = int(
                long_cfg.get("cooldown_bars", self.config.get("cooldown_bars", 0))
            )
            if self._last_exit_bars_ago_long <= long_cooldown:
                buy_prob = 0.0

            short_cfg = self.config.get("short", {})
            short_cooldown = int(
                short_cfg.get("cooldown_bars", self.config.get("cooldown_bars", 0))
            )
            if self._last_exit_bars_ago_short <= short_cooldown:
                sell_prob = 0.0

        state = EngineState(
            position=1 if current_position != 0 else 0,
            side=side,
            bars_held=0,  # Live trader tracks its own state, so we just provide FSM equivalents
            open_positions=1 if current_position != 0 else 0,
            # Neutralizing sentinel: cooldown is enforced ONLY by the gate
            # above; the execution strategy's re-gate (bars_ago <= cooldown)
            # must never fire, so feed a value above any configured cooldown.
            last_exit_bars_ago_long=9999,
            last_exit_bars_ago_short=9999,
        )

        orders = self._exec_strategy.on_bar(
            dt=dt,
            open_=current_price,
            high=current_price,
            low=current_price,
            close=current_price,
            atr=atr_value if atr_value is not None else 0.0,
            prob_buy=buy_prob,
            prob_sell=sell_prob,
            state=state,
        )

        # 3. Handle HOLD
        if not orders or orders[0].action == "HOLD":
            probability = max(buy_prob, sell_prob)
            skip_reason = "ENGINE_HOLD"
            signal_label = "Hold"
            if orders and orders[0].reason and orders[0].reason != "no_signal":
                skip_reason = orders[0].reason
                if orders[0].side == 1:
                    probability = buy_prob
                    signal_label = "Buy"
                elif orders[0].side == -1:
                    probability = sell_prob
                    signal_label = "Sell"

            return TradeSignal(
                action="HOLD",
                probability=probability,
                confidence_pct=probability * 100.0,
                signal_label=signal_label,
                skip_reason=skip_reason,
                buy_prob=buy_prob,
                sell_prob=sell_prob,
            )

        # 4. Map first actionable order to TradeSignal
        order = orders[0]
        
        # Determine actual probability used
        if order.action == "BUY":
            probability = buy_prob
            active_label = "Buy"
        elif order.action == "SELL":
            probability = sell_prob
            active_label = "Sell"
        else:
            # Safety fallback — should not occur with bracket-only exits
            probability = max(buy_prob, sell_prob)
            active_label = "Hold"

        # Apply per-trade overrides from Order object
        tier_overrides = {}
        tp_mult = order.tp_atr_mult if order.tp_atr_mult is not None else self.tp_atr_mult
        sl_mult = order.sl_atr_mult if order.sl_atr_mult is not None else self.sl_atr_mult
        
        if order.trailing_atr_mult is not None:
            tier_overrides["trailing_atr_mult"] = order.trailing_atr_mult
        if order.max_hold_bars is not None:
            tier_overrides["max_hold_bars"] = order.max_hold_bars
        tier_overrides["tp_atr_mult"] = tp_mult
        tier_overrides["sl_atr_mult"] = sl_mult

        # Select the correct per-side ATR for bracket sizing
        # BUY → use long ATR, SELL → use short ATR
        if order.action == "BUY" or order.side == 1:
            side_atr = atr_value_long if atr_value_long is not None else atr_value
        elif order.action == "SELL" or order.side == -1:
            side_atr = atr_value_short if atr_value_short is not None else atr_value
        else:
            side_atr = atr_value

        tiered_tp_offsets = None
        if self.exit_mode == "TIERED":
            exits_cfg = self._long_tiered_exits if order.action == "BUY" else self._short_tiered_exits
            if exits_cfg and side_atr is not None and side_atr > 0:
                tiered_tp_offsets = []
                avg_mult = 0.0
                total_pct = 0.0
                for ex in exits_cfg:
                    pct = float(ex["qty_pct"])
                    mult = float(ex["tp_atr_mult"])
                    offset_amount = round(mult * side_atr, 4)
                    tiered_tp_offsets.append((pct, offset_amount))
                    avg_mult += pct * mult
                    total_pct += pct
                if total_pct > 0:
                    tp_mult = avg_mult / total_pct

        # Compute bracket prices using side-specific ATR
        tp_price = 0.0
        sl_price = 0.0
        if side_atr is not None and side_atr > 0:
            if order.side == -1 or (order.action == "SELL"):
                tp_price = round(current_price - tp_mult * side_atr, 2)
                sl_price = round(current_price + sl_mult * side_atr, 2)
            elif order.side == 1 or (order.action == "BUY"):
                tp_price = round(current_price + tp_mult * side_atr, 2)
                sl_price = round(current_price - sl_mult * side_atr, 2)

        return TradeSignal(
            action=order.action,
            probability=probability,
            confidence_pct=probability * 100.0,
            tp_price=tp_price,
            sl_price=sl_price,
            lots=order.lots,
            signal_label=active_label,
            buy_prob=buy_prob,
            sell_prob=sell_prob,
            tiered_tp_offsets=tiered_tp_offsets,
            atr_at_entry=side_atr,
            **tier_overrides,
        )

    def on_exit(self, side: int, exit_reason: object, bars_held: int) -> None:
        """Forward position closure to the underlying execution strategy.

        Counter reset is -1 (not 0): the exit-bar evaluate() ALWAYS runs
        (fills arrive before the bar-close evaluation; time-barrier exits no
        longer skip evaluation) and its pre-gate increment yields 0 — the
        exact value the backtest gate reads on the exit bar. Release then
        happens at exit+N+1 reading N+1, matching the backtest for every
        cooldown including 0 (B(b)+F ticket, human-authorized 2026-07-03).
        """
        if side == 1:
            self._last_exit_bars_ago_long = -1
        elif side == -1:
            self._last_exit_bars_ago_short = -1

        if hasattr(self._exec_strategy, 'on_exit'):
            self._exec_strategy.on_exit(side, exit_reason, bars_held)

    # -- Strategy interface --------------------------------------------------
