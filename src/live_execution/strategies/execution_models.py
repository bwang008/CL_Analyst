"""
Execution Strategy Models — Decoupled signal-parsing for BacktestEngine.

Provides a BaseExecutionStrategy ABC and concrete implementations that
the BacktestEngine delegates to for signal interpretation.  The FSM
execution logic (TP/SL/trailing/time-barrier) remains untouched in the
engine; these strategies only decide *when* to enter and in *which
direction*.

Performance note:
    on_bar() receives flat floats and a mutable EngineState that is
    allocated once and reused.  No dicts or DataFrames are created
    per-bar to keep the 100k+ iteration loop fast.

Concrete strategies:
    SingleModelStrategy          — backward-compat single-direction
    ConservativeEnsembleStrategy — dual-model, no position flipping
    AggressiveEnsembleStrategy   — dual-model with position flipping
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import numpy as np


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------


@dataclass
class EngineState:
    """Mutable state snapshot passed to on_bar().

    Allocated once by the engine and mutated in-place each bar
    to avoid per-bar allocation overhead.
    """

    position: int = 0       # 0=flat, 1=long, -1=short (single mode)
    side: int = 0            # current position side (+1/-1)
    bars_held: int = 0       # bars since entry
    open_positions: int = 0  # count of open positions (concurrent mode)


@dataclass
class Order:
    """Signal returned by a strategy for the engine to execute.

    Attributes:
        action: "BUY", "SELL", or "HOLD".
        side:   +1 for long entry, -1 for short entry.
        lots:   Number of contracts.
        reason: Human-readable reason for logging.
        tp_atr_mult: Per-trade TP override (None = use engine global).
        sl_atr_mult: Per-trade SL override (None = use engine global).
        trailing_atr_mult: Per-trade trailing override (None = use engine global).
        max_hold_bars: Per-trade time-barrier override (None = use engine global).
    """

    action: str    # "BUY" | "SELL" | "HOLD"
    side: int      # +1 | -1
    lots: int = 1
    reason: str = ""
    tp_atr_mult: Optional[float] = None
    sl_atr_mult: Optional[float] = None
    trailing_atr_mult: Optional[float] = None
    max_hold_bars: Optional[int] = None


# Sentinel for "do nothing this bar"
HOLD = [Order(action="HOLD", side=0, lots=0, reason="no_signal")]


# ---------------------------------------------------------------------------
# Abstract Base
# ---------------------------------------------------------------------------


class BaseExecutionStrategy(ABC):
    """Interface for signal-parsing strategies used by BacktestEngine.

    Subclasses decide whether to enter a trade (and in which direction)
    given the current bar's data and engine state.  The engine handles
    all FSM execution (TP, SL, trailing, cooldown, equity tracking).

    Args:
        config: Full strategy JSON config dict.
    """

    def __init__(self, config: dict) -> None:
        self.config = config
        self.nickname: str = config.get("nickname", "unnamed")
        self.sizing_tiers: list[tuple[float, int]] = self._parse_sizing_tiers(
            config.get("sizing_tiers", {})
        )

    @staticmethod
    def _parse_sizing_tiers(raw: dict) -> list[tuple[float, int]]:
        """Parse sizing_tiers dict into sorted (min_prob, lots) list.

        Example: {"0.80": 3, "0.70": 2} -> [(0.80, 3), (0.70, 2)]
        Sorted highest-first so first match wins.
        """
        if not raw:
            return []
        return sorted(
            [(float(k), int(v)) for k, v in raw.items()],
            key=lambda t: t[0],
            reverse=True,
        )

    def _prob_to_lots(self, probability: float) -> int:
        """Map a probability to lot count using sizing_tiers."""
        for min_prob, lots in self.sizing_tiers:
            if probability >= min_prob:
                return lots
        return 1

    @abstractmethod
    def on_bar(
        self,
        dt: object,
        open_: float,
        high: float,
        low: float,
        close: float,
        atr: float,
        prob_buy: float,
        prob_sell: float,
        state: EngineState,
    ) -> list[Order]:
        """Evaluate a single bar and return zero or more orders.

        Args:
            dt:        Bar timestamp.
            open_:     Bar open price.
            high:      Bar high price.
            low:       Bar low price.
            close:     Bar close price.
            atr:       Current ATR value (may be NaN).
            prob_buy:  Buy model probability (0.0 if absent/NaN).
            prob_sell: Sell model probability (0.0 if absent/NaN).
            state:     Mutable engine state (position, side, bars_held, etc.)

        Returns:
            List of Order objects.  Empty list or [HOLD] means no action.
        """
        ...


# ---------------------------------------------------------------------------
# Concrete Strategies
# ---------------------------------------------------------------------------


class SingleModelStrategy(BaseExecutionStrategy):
    """Single-direction strategy — backward compatible with manatee/koala configs.

    Reads config["direction"] to decide which probability column to use:
        LONG  → prob_buy
        SHORT → prob_sell

    Fires when probability >= threshold and engine is flat (or has room
    in concurrent mode).
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.direction: str = config.get("direction", "LONG").upper()
        self.threshold: float = config.get("entry_threshold", 0.45)
        self.max_concurrent: int = config.get("max_concurrent", 1)

    def on_bar(
        self,
        dt: object,
        open_: float,
        high: float,
        low: float,
        close: float,
        atr: float,
        prob_buy: float,
        prob_sell: float,
        state: EngineState,
    ) -> list[Order]:
        # NaN guards
        if np.isnan(prob_buy):
            prob_buy = 0.0
        if np.isnan(prob_sell):
            prob_sell = 0.0

        # Position guard
        if state.position != 0 and state.open_positions >= self.max_concurrent:
            return HOLD

        if self.direction == "LONG":
            if prob_buy >= self.threshold:
                lots = self._prob_to_lots(prob_buy)
                return [Order(action="BUY", side=1, lots=lots,
                              reason=f"LONG prob_buy={prob_buy:.4f}")]
        elif self.direction == "SHORT":
            if prob_sell >= self.threshold:
                lots = self._prob_to_lots(prob_sell)
                return [Order(action="SELL", side=-1, lots=lots,
                              reason=f"SHORT prob_sell={prob_sell:.4f}")]

        return HOLD


class ConservativeEnsembleStrategy(BaseExecutionStrategy):
    """Dual-model ensemble — no position flipping.

    If FLAT:
        - Check both prob_buy and prob_sell against their thresholds.
        - If both exceed: take the higher probability signal.
        - If only one exceeds: take that one.
    If IN_POSITION:
        - Ignore new signals (no flipping).

    Conflict resolution ported from backtest_cl_concurrent.py lines 374-397.
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        models = config.get("models", {})
        long_cfg = models.get("long", {})
        short_cfg = models.get("short", {})
        self.long_threshold: float = long_cfg.get(
            "threshold", config.get("entry_threshold", 0.45)
        )
        self.short_threshold: float = short_cfg.get(
            "threshold", config.get("entry_threshold", 0.45)
        )
        self.max_concurrent: int = config.get("max_concurrent", 1)

    def on_bar(
        self,
        dt: object,
        open_: float,
        high: float,
        low: float,
        close: float,
        atr: float,
        prob_buy: float,
        prob_sell: float,
        state: EngineState,
    ) -> list[Order]:
        # NaN guards
        if np.isnan(prob_buy):
            prob_buy = 0.0
        if np.isnan(prob_sell):
            prob_sell = 0.0

        # If already in a position, ignore new signals
        if state.position != 0:
            return HOLD
        if state.open_positions >= self.max_concurrent:
            return HOLD

        buy_ok = prob_buy >= self.long_threshold
        sell_ok = prob_sell >= self.short_threshold

        if buy_ok and sell_ok:
            # Same-bar conflict: higher probability wins
            if prob_buy >= prob_sell:
                lots = self._prob_to_lots(prob_buy)
                return [Order(action="BUY", side=1, lots=lots,
                              reason=f"ENSEMBLE_BUY (conflict: buy={prob_buy:.4f} >= sell={prob_sell:.4f})")]
            else:
                lots = self._prob_to_lots(prob_sell)
                return [Order(action="SELL", side=-1, lots=lots,
                              reason=f"ENSEMBLE_SELL (conflict: sell={prob_sell:.4f} > buy={prob_buy:.4f})")]
        elif buy_ok:
            lots = self._prob_to_lots(prob_buy)
            return [Order(action="BUY", side=1, lots=lots,
                          reason=f"ENSEMBLE_BUY prob_buy={prob_buy:.4f}")]
        elif sell_ok:
            lots = self._prob_to_lots(prob_sell)
            return [Order(action="SELL", side=-1, lots=lots,
                          reason=f"ENSEMBLE_SELL prob_sell={prob_sell:.4f}")]

        return HOLD


class AggressiveEnsembleStrategy(BaseExecutionStrategy):
    """Dual-model ensemble WITH position flipping.

    Same as ConservativeEnsembleStrategy for FLAT state.

    When IN_POSITION:
        - If LONG and a valid SELL signal fires → EXIT + SELL (flip)
        - If SHORT and a valid BUY signal fires → EXIT + BUY (flip)
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        models = config.get("models", {})
        long_cfg = models.get("long", {})
        short_cfg = models.get("short", {})
        self.long_threshold: float = long_cfg.get(
            "threshold", config.get("entry_threshold", 0.45)
        )
        self.short_threshold: float = short_cfg.get(
            "threshold", config.get("entry_threshold", 0.45)
        )
        self.max_concurrent: int = config.get("max_concurrent", 1)

    def on_bar(
        self,
        dt: object,
        open_: float,
        high: float,
        low: float,
        close: float,
        atr: float,
        prob_buy: float,
        prob_sell: float,
        state: EngineState,
    ) -> list[Order]:
        # NaN guards
        if np.isnan(prob_buy):
            prob_buy = 0.0
        if np.isnan(prob_sell):
            prob_sell = 0.0

        buy_ok = prob_buy >= self.long_threshold
        sell_ok = prob_sell >= self.short_threshold

        # If already in a position, check for flip
        if state.position != 0:
            if state.side == 1 and sell_ok:
                # LONG → SELL flip
                lots = self._prob_to_lots(prob_sell)
                return [
                    Order(action="EXIT", side=0, lots=1,
                          reason="FLIP_EXIT_LONG"),
                    Order(action="SELL", side=-1, lots=lots,
                          reason=f"FLIP_TO_SHORT prob_sell={prob_sell:.4f}"),
                ]
            elif state.side == -1 and buy_ok:
                # SHORT → BUY flip
                lots = self._prob_to_lots(prob_buy)
                return [
                    Order(action="EXIT", side=0, lots=1,
                          reason="FLIP_EXIT_SHORT"),
                    Order(action="BUY", side=1, lots=lots,
                          reason=f"FLIP_TO_LONG prob_buy={prob_buy:.4f}"),
                ]
            return HOLD

        # FLAT — same logic as ConservativeEnsembleStrategy
        if buy_ok and sell_ok:
            if prob_buy >= prob_sell:
                lots = self._prob_to_lots(prob_buy)
                return [Order(action="BUY", side=1, lots=lots,
                              reason=f"ENSEMBLE_BUY (conflict: buy={prob_buy:.4f} >= sell={prob_sell:.4f})")]
            else:
                lots = self._prob_to_lots(prob_sell)
                return [Order(action="SELL", side=-1, lots=lots,
                              reason=f"ENSEMBLE_SELL (conflict: sell={prob_sell:.4f} > buy={prob_buy:.4f})")]
        elif buy_ok:
            lots = self._prob_to_lots(prob_buy)
            return [Order(action="BUY", side=1, lots=lots,
                          reason=f"ENSEMBLE_BUY prob_buy={prob_buy:.4f}")]
        elif sell_ok:
            lots = self._prob_to_lots(prob_sell)
            return [Order(action="SELL", side=-1, lots=lots,
                          reason=f"ENSEMBLE_SELL prob_sell={prob_sell:.4f}")]

        return HOLD


class TieredEnsembleStrategy(BaseExecutionStrategy):
    """Dual-model ensemble with per-tier TP/SL/trailing/max_hold overrides.

    Supports SEPARATE buy and sell model configurations with SEPARATE
    probability tiers per side.  Each tier specifies its own lots and
    execution parameters (tp_atr_mult, sl_atr_mult, trailing_atr_mult,
    max_hold_bars) which are passed to the engine via Order fields.

    Config shape::

        {
            "long": {
                "experiment_id": "...",
                "tiers": [
                    {"min_prob": 0.75, "lots": 2, "tp_atr_mult": 3.0, ...},
                    {"min_prob": 0.60, "lots": 1, ...}
                ]
            },
            "short": { ... same shape ... }
        }

    Tier matching rules:
        - Tiers are evaluated highest min_prob first; first match wins.
        - If no tier matches, HOLD is returned.
        - When both buy and sell fire on the same bar, the higher
          probability wins (same conflict resolution as Conservative).
        - When in a position, no new entries (no-flip behaviour).
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        long_cfg = config.get("long", {})
        short_cfg = config.get("short", {})
        self.long_tiers = self._parse_tiers(long_cfg.get("tiers", []))
        self.short_tiers = self._parse_tiers(short_cfg.get("tiers", []))
        self.max_concurrent: int = config.get("max_concurrent", 1)

    @staticmethod
    def _parse_tiers(raw: list[dict]) -> list[dict]:
        """Parse and sort tiers by min_prob descending (first match wins)."""
        if not raw:
            return []
        tiers = []
        for t in raw:
            tiers.append({
                "min_prob": float(t.get("min_prob", 0.0)),
                "lots": int(t.get("lots", 1)),
                "tp_atr_mult": t.get("tp_atr_mult"),
                "sl_atr_mult": t.get("sl_atr_mult"),
                "trailing_atr_mult": t.get("trailing_atr_mult"),
                "max_hold_bars": t.get("max_hold_bars"),
                "label": t.get("label", ""),
            })
        tiers.sort(key=lambda x: x["min_prob"], reverse=True)
        return tiers

    def _match_tier(
        self, probability: float, tiers: list[dict]
    ) -> Optional[dict]:
        """Return the first tier whose min_prob <= probability, or None."""
        for tier in tiers:
            if probability >= tier["min_prob"]:
                return tier
        return None

    def _tier_to_order(
        self,
        tier: dict,
        action: str,
        side: int,
        probability: float,
    ) -> Order:
        """Build an Order from a matched tier, carrying per-trade overrides."""
        label = tier.get("label", "")
        tp = tier.get("tp_atr_mult")
        sl = tier.get("sl_atr_mult")
        trail = tier.get("trailing_atr_mult")
        mhb = tier.get("max_hold_bars")
        return Order(
            action=action,
            side=side,
            lots=tier["lots"],
            reason=(
                f"TIERED_{action} prob={probability:.4f} "
                f"tier={label} lots={tier['lots']}"
            ),
            tp_atr_mult=float(tp) if tp is not None else None,
            sl_atr_mult=float(sl) if sl is not None else None,
            trailing_atr_mult=float(trail) if trail is not None else None,
            max_hold_bars=int(mhb) if mhb is not None else None,
        )

    def on_bar(
        self,
        dt: object,
        open_: float,
        high: float,
        low: float,
        close: float,
        atr: float,
        prob_buy: float,
        prob_sell: float,
        state: EngineState,
    ) -> list[Order]:
        # NaN guards
        if np.isnan(prob_buy):
            prob_buy = 0.0
        if np.isnan(prob_sell):
            prob_sell = 0.0

        # If already in a position, ignore new signals (conservative)
        if state.position != 0:
            return HOLD
        if state.open_positions >= self.max_concurrent:
            return HOLD

        # Match tiers
        buy_tier = self._match_tier(prob_buy, self.long_tiers)
        sell_tier = self._match_tier(prob_sell, self.short_tiers)

        buy_ok = buy_tier is not None
        sell_ok = sell_tier is not None

        if buy_ok and sell_ok:
            # Same-bar conflict: higher probability wins
            if prob_buy >= prob_sell:
                return [self._tier_to_order(buy_tier, "BUY", 1, prob_buy)]
            else:
                return [self._tier_to_order(sell_tier, "SELL", -1, prob_sell)]
        elif buy_ok:
            return [self._tier_to_order(buy_tier, "BUY", 1, prob_buy)]
        elif sell_ok:
            return [self._tier_to_order(sell_tier, "SELL", -1, prob_sell)]

        return HOLD


# ---------------------------------------------------------------------------
# Strategy Registry / Factory
# ---------------------------------------------------------------------------

STRATEGY_REGISTRY: dict[str, type[BaseExecutionStrategy]] = {
    "SingleModelStrategy": SingleModelStrategy,
    "ConservativeEnsembleStrategy": ConservativeEnsembleStrategy,
    "AggressiveEnsembleStrategy": AggressiveEnsembleStrategy,
    "TieredEnsembleStrategy": TieredEnsembleStrategy,
}


def create_execution_strategy(config: dict) -> BaseExecutionStrategy:
    """Instantiate an execution strategy from a JSON config dict.

    Looks up the ``execution_class`` field in the config.  If absent,
    defaults to ``SingleModelStrategy`` for backward compatibility.

    Args:
        config: Full strategy JSON config dict.

    Returns:
        An instance of the requested BaseExecutionStrategy subclass.

    Raises:
        ValueError: If the execution_class is not found in the registry.
    """
    class_name = config.get("execution_class", "SingleModelStrategy")
    if class_name not in STRATEGY_REGISTRY:
        raise ValueError(
            f"Unknown execution_class '{class_name}'. "
            f"Available: {list(STRATEGY_REGISTRY.keys())}"
        )
    cls = STRATEGY_REGISTRY[class_name]
    return cls(config)
