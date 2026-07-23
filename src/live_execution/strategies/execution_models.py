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
    SingleModelStrategy              — backward-compat single-direction
    ConservativeEnsembleStrategy     — dual-model, no position flipping
    AggressiveEnsembleStrategy       — dual-model with position flipping
    IsolatedAsymmetricalStrategy     — independent long/short, concurrent positions
    JointPortfolioStrategy           — shared portfolio slot, conflict resolution
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
    last_exit_bars_ago_long: int = 9999
    last_exit_bars_ago_short: int = 9999
    # Exec-basis position economics (None when flat, or when the runtime does
    # not feed them — strategies that REQUIRE them must crash on None, never
    # silently degrade).  entry_price is the exec-basis entry FILL;
    # floating_pnl_points is sign-aware gross unrealized PnL in price points:
    # side * (exec_close - entry_fill).  Computed by the ENGINE on the exec
    # (raw) price basis — on_bar()'s own `close` arg is the BRAIN
    # (ratio-adjusted) close and must never be compared against entry_price.
    entry_price: Optional[float] = None
    floating_pnl_points: Optional[float] = None


@dataclass
class Order:
    """Signal returned by a strategy for the engine to execute.

    Attributes:
        action: "BUY", "SELL", "HOLD", or "EXIT".  EXIT requests the engine
                close the current position (used by JointPortfolioStrategy).
        side:   +1 for long entry, -1 for short entry.
        lots:   Number of contracts.
        reason: Human-readable reason for logging.
        tp_atr_mult: Per-trade TP override (None = use engine global).
        sl_atr_mult: Per-trade SL override (None = use engine global).
        trailing_atr_mult: Per-trade trailing override (None = use engine global).
        max_hold_bars: Per-trade time-barrier override (None = use engine global).
    """

    action: str    # "BUY" | "SELL" | "HOLD" | "EXIT"
    side: int      # +1 | -1
    lots: int = 1
    reason: str = ""
    tp_atr_mult: Optional[float] = None
    sl_atr_mult: Optional[float] = None
    trailing_atr_mult: Optional[float] = None
    max_hold_bars: Optional[int] = None
    override_entry_price: Optional[float] = None


# Sentinel for "do nothing this bar"
HOLD = [Order(action="HOLD", side=0, lots=0, reason="no_signal")]


# ---------------------------------------------------------------------------
# Shared Tranche Lot Allocator
# ---------------------------------------------------------------------------


def allocate_tranche_lots(total_lots: int, qty_pcts: list[float]) -> list[int]:
    """Split a position's lots across tiered-exit rungs (LIVE parity).

    Reproduces the live bracket-children allocator
    (live_trader.py ``_place_bracket_children_on_fill``) EXACTLY: each
    non-last rung gets ``min(max(1, int(round(total_lots * pct))),
    remaining)`` — Python banker's rounding intact (round(2.5) == 2) —
    the LAST rung gets the remainder, and zero-lot rungs are skipped.
    The returned list conserves ``total_lots`` and contains only nonzero
    tranches, in rung order.
    """
    remaining = total_lots
    allocated: list[int] = []
    for i, pct in enumerate(qty_pcts):
        if i == len(qty_pcts) - 1:
            rung_lots = remaining
        else:
            rung_lots = min(max(1, int(round(total_lots * pct))), remaining)
        if rung_lots > 0:
            allocated.append(rung_lots)
        remaining -= rung_lots
    return allocated


# Ticket trailing-sl-no-cooldown_07222026_2050: the post-exit re-entry
# cooldown arms ONLY on an original stop-loss exit. Exits that lock profit or
# end a trade for non-loss reasons (TRAILING_BE, TP, TIME_BARRIER,
# SIGNAL_EXIT, EOD/WEEKEND flattens, OOB/unknown closes) must not block
# re-entry.
COOLDOWN_ARMING_EXIT_REASONS = frozenset({"SL", "SL_HIT", "SL_HIT_OOB"})


def exit_reason_arms_cooldown(exit_reason: object) -> bool:
    """True iff this exit reason arms the per-side re-entry cooldown.

    Accepts ExitReason enum members or the live trader's reason strings.
    None/unknown reasons never arm — a cooldown must come from a proven
    original-SL exit.
    """
    value = getattr(exit_reason, "value", exit_reason)
    return isinstance(value, str) and value in COOLDOWN_ARMING_EXIT_REASONS


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

    def apply_trial_params(
        self, cfg: dict, params: dict, side: Optional[str] = None,
    ) -> dict:
        """Apply optimizer trial parameters to the config dict.

        Routes params to the correct config locations so that the values
        Optuna tests are the values the strategy actually reads.

        Default implementation writes to top-level keys (correct for
        SingleModelStrategy, ConservativeEnsembleStrategy, etc.).
        Subclasses that read from nested locations (e.g. tier blocks)
        must override this method.

        Args:
            cfg:    Mutable config dict (deep-copy of base config).
            params: Dict of Optuna-suggested parameter values.
            side:   Optional "long" or "short" to target one side only.
                    None writes to both sides (where applicable).

        Returns:
            The mutated config dict.
        """
        for key in ("tp_atr_mult", "sl_atr_mult", "trailing_atr_mult",
                    "cooldown_bars", "max_hold_bars",
                    "consecutive_signal_threshold"):
            if key in params:
                cfg[key] = params[key]

        if "entry_threshold" in params:
            cfg["entry_threshold"] = params["entry_threshold"]
            if "models" in cfg:
                for direction in cfg["models"]:
                    cfg["models"][direction]["threshold"] = params["entry_threshold"]

        return cfg

    def on_exit(
        self, side: int, exit_reason: object, bars_held: int,
    ) -> None:
        """Called by the engine when a position is closed.

        Override in subclasses that need to track per-side open/close
        state internally (e.g. IsolatedAsymmetricalStrategy).

        Args:
            side:        +1 for long, -1 for short.
            exit_reason: ExitReason enum value.
            bars_held:   Number of bars the position was held.
        """
        pass  # Default no-op

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
        self.max_concurrent: int = config.get("max_concurrent", 1)

        # Resolve threshold: check models.long/short.threshold first,
        # then top-level entry_threshold, then DEFAULT TO 1.0 (no trading).
        models = config.get("models", {})
        model_key = "long" if self.direction == "LONG" else "short"
        model_threshold = (models.get(model_key, {}) or {}).get("threshold")

        if model_threshold is not None:
            self.threshold: float = model_threshold
        elif "entry_threshold" in config:
            self.threshold = config["entry_threshold"]
        else:
            import warnings
            self.threshold = 1.0
            warnings.warn(
                f"[SingleModelStrategy] No threshold found in config! "
                f"Checked models.{model_key}.threshold and entry_threshold. "
                f"Defaulting to 1.0 (NO TRADES). Set a threshold explicitly.",
                UserWarning,
                stacklevel=2,
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

        buy_ok = self.direction == "LONG" and prob_buy >= self.threshold
        sell_ok = self.direction == "SHORT" and prob_sell >= self.threshold

        # Position guard
        if state.position != 0 and state.open_positions >= self.max_concurrent:
            if buy_ok:
                return [Order(action="HOLD", side=1, reason="POSITION_OPEN")]
            elif sell_ok:
                return [Order(action="HOLD", side=-1, reason="POSITION_OPEN")]
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

        buy_ok = prob_buy >= self.long_threshold
        sell_ok = prob_sell >= self.short_threshold

        # If already in a position, ignore new signals
        if state.position != 0 or state.open_positions >= self.max_concurrent:
            if buy_ok and sell_ok:
                side = 1 if prob_buy >= prob_sell else -1
                return [Order(action="HOLD", side=side, reason="POSITION_OPEN")]
            elif buy_ok:
                return [Order(action="HOLD", side=1, reason="POSITION_OPEN")]
            elif sell_ok:
                return [Order(action="HOLD", side=-1, reason="POSITION_OPEN")]
            return HOLD

        buy_ok = prob_buy >= self.long_threshold
        sell_ok = prob_sell >= self.short_threshold

        if buy_ok and sell_ok:
            # Same-bar conflict: higher probability wins; exact tie → HOLD
            if prob_buy > prob_sell:
                lots = self._prob_to_lots(prob_buy)
                return [Order(action="BUY", side=1, lots=lots,
                              reason=f"ENSEMBLE_BUY (conflict: buy={prob_buy:.4f} > sell={prob_sell:.4f})")]
            elif prob_sell > prob_buy:
                lots = self._prob_to_lots(prob_sell)
                return [Order(action="SELL", side=-1, lots=lots,
                              reason=f"ENSEMBLE_SELL (conflict: sell={prob_sell:.4f} > buy={prob_buy:.4f})")]
            else:
                return HOLD
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

        # Bracket-only exit rule: once in a position, ignore new signals.
        if state.position != 0:
            if buy_ok and sell_ok:
                side = 1 if prob_buy >= prob_sell else -1
                return [Order(action="HOLD", side=side, reason="POSITION_OPEN")]
            elif buy_ok:
                return [Order(action="HOLD", side=1, reason="POSITION_OPEN")]
            elif sell_ok:
                return [Order(action="HOLD", side=-1, reason="POSITION_OPEN")]
            return HOLD

        # FLAT — same logic as ConservativeEnsembleStrategy
        if buy_ok and sell_ok:
            if prob_buy > prob_sell:
                lots = self._prob_to_lots(prob_buy)
                return [Order(action="BUY", side=1, lots=lots,
                              reason=f"ENSEMBLE_BUY (conflict: buy={prob_buy:.4f} > sell={prob_sell:.4f})")]
            elif prob_sell > prob_buy:
                lots = self._prob_to_lots(prob_sell)
                return [Order(action="SELL", side=-1, lots=lots,
                              reason=f"ENSEMBLE_SELL (conflict: sell={prob_sell:.4f} > buy={prob_buy:.4f})")]
            else:
                return HOLD
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

    Conflict resolution when in-position and an opposing signal fires:
        ``hold`` (default)
            Ignore opposing signals — let TP/SL/trailing manage exit.
        ``close_existing_position``
            EXIT current trade when opposite signal fires.
        ``reverse_position``
            EXIT current trade + ENTER opposite in the same bar.
        ``close_existing_position_if_profit``
            EXIT when opposite fires, own side is silent, and the position
            is green (requires EngineState.floating_pnl_points).

    Config shape::

        {
            "conflict_resolution": "hold",  // optional, default="hold"
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
        - When in a position, behaviour depends on ``conflict_resolution``.
    """

    VALID_CONFLICT_MODES = (
        "hold",
        "close_existing_position",
        "reverse_position",
        "close_existing_position_if_profit",
    )

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        long_cfg = config.get("long", {})
        short_cfg = config.get("short", {})
        self.long_tiers = self._parse_tiers(long_cfg.get("tiers", []), long_cfg)
        self.short_tiers = self._parse_tiers(short_cfg.get("tiers", []), short_cfg)
        self.max_concurrent: int = config.get("max_concurrent", 1)
        self._consecutive_long_signals = 0
        self._consecutive_short_signals = 0
        self.long_cooldown_bars = long_cfg.get("cooldown_bars", config.get("cooldown_bars", 0))
        self.short_cooldown_bars = short_cfg.get("cooldown_bars", config.get("cooldown_bars", 0))
        self.long_consecutive_threshold = long_cfg.get("consecutive_signal_threshold", config.get("consecutive_signal_threshold", 0))
        self.short_consecutive_threshold = short_cfg.get("consecutive_signal_threshold", config.get("consecutive_signal_threshold", 0))

        # Conflict resolution mode
        self.conflict_resolution: str = config.get(
            "conflict_resolution", "hold"
        )
        if self.conflict_resolution not in self.VALID_CONFLICT_MODES:
            raise ValueError(
                f"Invalid conflict_resolution '{self.conflict_resolution}'. "
                f"Must be one of {self.VALID_CONFLICT_MODES}"
            )

        # Derive effective thresholds from tiers (single source of truth).
        # The effective threshold for a side is the lowest min_prob across
        # its tiers — i.e., the minimum probability required to trade at all.
        self.long_threshold: float = (
            min(t["min_prob"] for t in self.long_tiers)
            if self.long_tiers else 1.0
        )
        self.short_threshold: float = (
            min(t["min_prob"] for t in self.short_tiers)
            if self.short_tiers else 1.0
        )

        # Validate: warn if models.*.threshold diverges from effective tier
        # threshold.  models.*.threshold is cosmetic/informational — the
        # tiers[*].min_prob values are what actually control execution.
        import warnings
        models = config.get("models", {})
        for side_key, eff_thr in (("long", self.long_threshold), ("short", self.short_threshold)):
            model_thr = (models.get(side_key, {}) or {}).get("threshold")
            if model_thr is not None and abs(model_thr - eff_thr) > 1e-9:
                warnings.warn(
                    f"[TieredEnsembleStrategy] models.{side_key}.threshold "
                    f"({model_thr}) differs from effective tier min_prob "
                    f"({eff_thr}). The tier min_prob is used for execution. "
                    f"Update {side_key}.tiers[*].min_prob to change the "
                    f"actual entry threshold.",
                    UserWarning,
                    stacklevel=2,
                )

    @staticmethod
    def _parse_tiers(raw: list[dict], base_cfg: dict = None) -> list[dict]:
        """Parse and sort tiers by min_prob descending (first match wins)."""
        if not raw:
            return []
        if base_cfg is None:
            base_cfg = {}
            
        def _get_override(key: str):
            val = base_cfg.get(key)
            if val is not None:
                return val
            tiered_exits = base_cfg.get("tiered_exits", [])
            if tiered_exits and len(tiered_exits) > 0:
                return tiered_exits[0].get(key)
            return None

        base_tp = _get_override("tp_atr_mult")
        base_sl = _get_override("sl_atr_mult")
        base_trail = _get_override("trailing_atr_mult")
        base_mhb = _get_override("max_hold_bars")

        tiers = []
        for t in raw:
            tiers.append({
                "min_prob": float(t.get("min_prob", 0.0)),
                "lots": int(t.get("lots", 1)),
                "tp_atr_mult": t.get("tp_atr_mult", base_tp),
                "sl_atr_mult": t.get("sl_atr_mult", base_sl),
                "trailing_atr_mult": t.get("trailing_atr_mult", base_trail),
                "max_hold_bars": t.get("max_hold_bars", base_mhb),
                "label": t.get("label", ""),
            })
        tiers.sort(key=lambda x: x["min_prob"], reverse=True)
        return tiers

    def apply_trial_params(
        self, cfg: dict, params: dict, side: Optional[str] = None,
    ) -> dict:
        """Route optimizer params into tier blocks where TieredEnsembleStrategy reads.

        Writes TP, SL, threshold, trailing, and max_hold into the
        per-side ``tiers`` and ``tiered_exits`` arrays so the values
        Optuna tests are the values that actually execute.

        Also writes to top-level keys for BacktestEngine globals
        (cooldown, trailing fallback) and to models.*.threshold.
        """
        tp = params.get("tp_atr_mult")
        sl = params.get("sl_atr_mult")
        threshold = params.get("entry_threshold")
        trailing = params.get("trailing_atr_mult")
        max_hold = params.get("max_hold_bars")

        sides = [side] if side else ["long", "short"]
        for s in sides:
            side_cfg = cfg.get(s, {})

            # Write params at the side level
            if tp is not None:
                side_cfg["tp_atr_mult"] = tp
            if sl is not None:
                side_cfg["sl_atr_mult"] = sl
            if trailing is not None:
                side_cfg["trailing_atr_mult"] = trailing
            if max_hold is not None:
                side_cfg["max_hold_bars"] = max_hold
            if "cooldown_bars" in params:
                side_cfg["cooldown_bars"] = params["cooldown_bars"]
            if "consecutive_signal_threshold" in params:
                side_cfg["consecutive_signal_threshold"] = params["consecutive_signal_threshold"]
            if "atr_period" in params:
                side_cfg["atr_period"] = params["atr_period"]
            for _tso_key in ("trailing_sl_atr_offset", "trailing_activation_mult"):
                if _tso_key in params:
                    side_cfg["trailing_sl_atr_offset"] = params[_tso_key]
                    break

            # Trailing ladder is PER-SIDE ONLY (never top-level, never in
            # tiers). None = explicit removal so a disabled trial strips any
            # stale ladder inherited from a warm-start config.
            if "trailing_ladder" in params:
                if params["trailing_ladder"] is None:
                    side_cfg.pop("trailing_ladder", None)
                else:
                    side_cfg["trailing_ladder"] = [
                        dict(rung) for rung in params["trailing_ladder"]
                    ]

            # Write into tiered_exits blocks
            for exit_tier in side_cfg.get("tiered_exits", []):
                if tp is not None:
                    exit_tier["tp_atr_mult"] = tp

            # Write into each tier entry
            for tier in side_cfg.get("tiers", []):
                if threshold is not None:
                    tier["min_prob"] = threshold
                if tp is not None:
                    tier["tp_atr_mult"] = tp
                if sl is not None:
                    tier["sl_atr_mult"] = sl
                if trailing is not None:
                    tier["trailing_atr_mult"] = trailing
                if max_hold is not None:
                    tier["max_hold_bars"] = max_hold

            cfg[s] = side_cfg

        # Also write top-level for engine globals (cooldown, trailing fallback)
        for key in ("tp_atr_mult", "sl_atr_mult", "trailing_atr_mult",
                    "cooldown_bars", "max_hold_bars",
                    "consecutive_signal_threshold", "atr_period", "trailing_sl_atr_offset"):
            if key in params:
                cfg[key] = params[key]

        if threshold is not None:
            cfg["entry_threshold"] = threshold
            if "models" in cfg:
                for direction in cfg["models"]:
                    cfg["models"][direction]["threshold"] = threshold

        if "conflict_resolution" in params:
            cfg["conflict_resolution"] = params["conflict_resolution"]

        return cfg

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

        # Match tiers
        buy_tier = self._match_tier(prob_buy, self.long_tiers)
        sell_tier = self._match_tier(prob_sell, self.short_tiers)

        buy_ok = buy_tier is not None
        sell_ok = sell_tier is not None

        # Track consecutive signals — only when FLAT and evaluating for entry.
        # While IN_POSITION, freeze counters so the blocked side doesn't
        # lose its accumulated consecutive count.
        if state.position == 0:
            if buy_ok:
                self._consecutive_long_signals += 1
            elif prob_buy > 0:
                self._consecutive_long_signals = 0

            if sell_ok:
                self._consecutive_short_signals += 1
            elif prob_sell > 0:
                self._consecutive_short_signals = 0
            
        # Check consecutive threshold rules
        if self.long_consecutive_threshold > 0 and self._consecutive_long_signals < self.long_consecutive_threshold:
            buy_ok = False
            buy_tier = None
        if self.short_consecutive_threshold > 0 and self._consecutive_short_signals < self.short_consecutive_threshold:
            sell_ok = False
            sell_tier = None

        # Check cooldown rules
        if state.last_exit_bars_ago_long <= self.long_cooldown_bars:
            buy_ok = False
            buy_tier = None
        if state.last_exit_bars_ago_short <= self.short_cooldown_bars:
            sell_ok = False
            sell_tier = None

        # ── IN POSITION ──
        if state.position != 0 or state.open_positions >= self.max_concurrent:
            if self.conflict_resolution == "hold":
                # Default: ignore all new signals while in position
                if buy_ok and sell_ok:
                    side = 1 if prob_buy >= prob_sell else -1
                    return [Order(action="HOLD", side=side, reason="POSITION_OPEN")]
                elif buy_ok:
                    return [Order(action="HOLD", side=1, reason="POSITION_OPEN")]
                elif sell_ok:
                    return [Order(action="HOLD", side=-1, reason="POSITION_OPEN")]
                return HOLD

            # Conflict resolution modes that react to opposing signals
            current_side = state.side
            opposite_ok = (sell_ok if current_side == 1 else buy_ok)
            opposite_tier = (sell_tier if current_side == 1 else buy_tier)
            opposite_prob = (prob_sell if current_side == 1 else prob_buy)

            if self.conflict_resolution == "close_existing_position":
                if opposite_ok:
                    return [Order(
                        action="EXIT", side=current_side,
                        reason=f"TIERED_EXIT opposite signal ({opposite_prob:.4f})",
                    )]
                return HOLD

            elif self.conflict_resolution == "reverse_position":
                if opposite_ok and opposite_tier is not None:
                    exit_order = Order(
                        action="EXIT", side=current_side,
                        reason=f"TIERED_REVERSE exit ({opposite_prob:.4f})",
                    )
                    if current_side == 1:
                        enter_order = self._tier_to_order(
                            opposite_tier, "SELL", -1, opposite_prob
                        )
                    else:
                        enter_order = self._tier_to_order(
                            opposite_tier, "BUY", 1, opposite_prob
                        )
                    return [exit_order, enter_order]
                return HOLD

            elif self.conflict_resolution == "close_existing_position_if_profit":
                # EXIT iff the opposite side fires AND our own side has
                # stopped confirming AND the position is green (gross, exec
                # basis).  Both firing -> HOLD (ignore); losing -> HOLD (let
                # brackets manage it).  Note: exit slippage/commission may
                # flip a marginal winner — accepted for v1 (gross gate).
                if current_side != 0 and opposite_ok:
                    if state.floating_pnl_points is None:
                        # Binding impact-review condition (ticket
                        # exit-triggers-eod-oppsignal_07072026_1924): a
                        # runtime that does not feed exec-basis floating PnL
                        # (e.g. today's live path) must CRASH here, never
                        # silently degrade to hold semantics.
                        raise RuntimeError(
                            "conflict_resolution="
                            "'close_existing_position_if_profit' requires "
                            "EngineState.floating_pnl_points, but it is None "
                            "while in position. This runtime does not feed "
                            "exec-basis floating PnL — refusing to silently "
                            "degrade to 'hold'."
                        )
                    same_ok = (buy_ok if current_side == 1 else sell_ok)
                    if (not same_ok) and state.floating_pnl_points > 0:
                        return [Order(
                            action="EXIT", side=current_side,
                            reason=(
                                f"TIERED_PROFIT_CLOSE opposite signal "
                                f"({opposite_prob:.4f}), own side silent, "
                                f"unrealized {state.floating_pnl_points:+.4f} pts"
                            ),
                        )]
                return HOLD

            return HOLD

        # ── FLAT ──
        if buy_ok and sell_ok:
            # Same-bar conflict: higher probability wins; exact tie → HOLD
            if prob_buy > prob_sell:
                return [self._tier_to_order(buy_tier, "BUY", 1, prob_buy)]
            elif prob_sell > prob_buy:
                return [self._tier_to_order(sell_tier, "SELL", -1, prob_sell)]
            else:
                return HOLD
        elif buy_ok:
            return [self._tier_to_order(buy_tier, "BUY", 1, prob_buy)]
        elif sell_ok:
            return [self._tier_to_order(sell_tier, "SELL", -1, prob_sell)]

        return HOLD

class BreakoutStraddleStrategy(BaseExecutionStrategy):
    """Non-directional breakout straddle for magnitude prediction models.
    
    When prob_buy (magnitude prediction) >= threshold, this strategy
    records the current bar's High and Low as pending breakout barriers.
    
    On subsequent bars (up to breakout_window), if the High exceeds the
    buy_stop, it returns a BUY order entering exactly at the buy_stop.
    If the Low pierces the sell_stop, it returns a SELL order exactly at
    the sell_stop. Whichever triggers first cancels the other.
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.threshold: float = config.get("entry_threshold", 0.60)
        self.breakout_window: int = config.get("breakout_window", 6)
        
        # Pending state
        self.pending = False
        self.buy_stop: float = 0.0
        self.sell_stop: float = 0.0
        self.bars_waiting: int = 0
        self.trigger_atr: float = 0.0

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
        if np.isnan(prob_buy):
            prob_buy = 0.0
            
        # 1. No pending orders. Should we create them?
        if not self.pending:
            if state.position == 0 and prob_buy >= self.threshold:
                # Signal triggered: setup straddle stops on the NEXT bar
                self.pending = True
                self.buy_stop = high
                self.sell_stop = low
                self.bars_waiting = 0
                self.trigger_atr = atr
            return HOLD
            
        # 2. Orders are pending. Check for execution first, then expiration.
        self.bars_waiting += 1
        
        # Did the current bar trigger our pending orders?
        buy_triggered = high >= self.buy_stop
        sell_triggered = low <= self.sell_stop
        
        if buy_triggered and sell_triggered:
            # Whipsaw: both hit in same bar. For backtesting safety, pick the worst one,
            # or realistically, pick random. Let's long for simplicity but note it.
            self.pending = False
            return [Order(action="BUY", side=1, lots=1, 
                          reason=f"STRADDLE_BUY_WHIPSAW wait={self.bars_waiting}",
                          override_entry_price=self.buy_stop)]
                          
        elif buy_triggered:
            self.pending = False
            return [Order(action="BUY", side=1, lots=1, 
                          reason=f"STRADDLE_BUY wait={self.bars_waiting}",
                          override_entry_price=self.buy_stop)]
                          
        elif sell_triggered:
            self.pending = False
            return [Order(action="SELL", side=-1, lots=1, 
                          reason=f"STRADDLE_SELL wait={self.bars_waiting}",
                          override_entry_price=self.sell_stop)]
                          
        # 3. Expiration check
        if self.bars_waiting >= self.breakout_window:
            self.pending = False
            
        return HOLD

class IsolatedAsymmetricalStrategy(BaseExecutionStrategy):
    """Independent long/short models with concurrent positions.

    Each side tracks its own cooldowns, consecutive signal counters,
    and open-position state.  Both can be in position simultaneously
    (engine must use ``allow_concurrent=True, max_concurrent=2``).

    No conflict resolution is needed — the two sides are fully
    independent agents sharing the same price feed.

    Config shape::

        {
            "execution_class": "IsolatedAsymmetricalStrategy",
            "allow_concurrent": true,
            "max_concurrent": 2,
            "long": {
                "tiers": [{"min_prob": 0.58, "lots": 1, ...}],
                "cooldown_bars": 9,
                "consecutive_signal_threshold": 0
            },
            "short": { ... same shape ... }
        }
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        long_cfg = config.get("long", {})
        short_cfg = config.get("short", {})
        self.long_tiers = TieredEnsembleStrategy._parse_tiers(
            long_cfg.get("tiers", []), long_cfg
        )
        self.short_tiers = TieredEnsembleStrategy._parse_tiers(
            short_cfg.get("tiers", []), short_cfg
        )
        self.long_cooldown_bars = long_cfg.get(
            "cooldown_bars", config.get("cooldown_bars", 0)
        )
        self.short_cooldown_bars = short_cfg.get(
            "cooldown_bars", config.get("cooldown_bars", 0)
        )
        self.long_consecutive_threshold = long_cfg.get(
            "consecutive_signal_threshold",
            config.get("consecutive_signal_threshold", 0),
        )
        self.short_consecutive_threshold = short_cfg.get(
            "consecutive_signal_threshold",
            config.get("consecutive_signal_threshold", 0),
        )

        # Derive effective thresholds from tiers
        self.long_threshold: float = (
            min(t["min_prob"] for t in self.long_tiers)
            if self.long_tiers else 1.0
        )
        self.short_threshold: float = (
            min(t["min_prob"] for t in self.short_tiers)
            if self.short_tiers else 1.0
        )

        # Internal per-side state
        self._consecutive_long_signals: int = 0
        self._consecutive_short_signals: int = 0
        self._long_is_open: bool = False
        self._short_is_open: bool = False
        self._bars_since_long_exit: int = 9999
        self._bars_since_short_exit: int = 9999

    def on_exit(self, side: int, exit_reason: object, bars_held: int) -> None:
        """Track per-side position closure. Cooldown counters reset only on
        an original SL (trailing-sl-no-cooldown_07222026_2050)."""
        arms = exit_reason_arms_cooldown(exit_reason)
        if side == 1:
            self._long_is_open = False
            if arms:
                self._bars_since_long_exit = 0
        elif side == -1:
            self._short_is_open = False
            if arms:
                self._bars_since_short_exit = 0

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

        # Increment internal cooldown counters
        self._bars_since_long_exit += 1
        self._bars_since_short_exit += 1

        # ── Long side evaluation ──
        buy_tier = TieredEnsembleStrategy._match_tier(self, prob_buy, self.long_tiers)
        buy_ok = buy_tier is not None

        if not self._long_is_open:
            if buy_ok:
                self._consecutive_long_signals += 1
            elif prob_buy > 0:
                self._consecutive_long_signals = 0

        if (self.long_consecutive_threshold > 0
                and self._consecutive_long_signals < self.long_consecutive_threshold):
            buy_ok = False
            buy_tier = None

        if self._bars_since_long_exit <= self.long_cooldown_bars:
            buy_ok = False
            buy_tier = None

        if self._long_is_open:
            buy_ok = False
            buy_tier = None

        # ── Short side evaluation ──
        sell_tier = TieredEnsembleStrategy._match_tier(self, prob_sell, self.short_tiers)
        sell_ok = sell_tier is not None

        if not self._short_is_open:
            if sell_ok:
                self._consecutive_short_signals += 1
            elif prob_sell > 0:
                self._consecutive_short_signals = 0

        if (self.short_consecutive_threshold > 0
                and self._consecutive_short_signals < self.short_consecutive_threshold):
            sell_ok = False
            sell_tier = None

        if self._bars_since_short_exit <= self.short_cooldown_bars:
            sell_ok = False
            sell_tier = None

        if self._short_is_open:
            sell_ok = False
            sell_tier = None

        # ── Build independent orders ──
        orders: list[Order] = []

        if buy_ok and buy_tier is not None:
            self._long_is_open = True
            orders.append(TieredEnsembleStrategy._tier_to_order(
                self, buy_tier, "BUY", 1, prob_buy
            ))

        if sell_ok and sell_tier is not None:
            self._short_is_open = True
            orders.append(TieredEnsembleStrategy._tier_to_order(
                self, sell_tier, "SELL", -1, prob_sell
            ))

        return orders if orders else HOLD

    def apply_trial_params(
        self, cfg: dict, params: dict, side: Optional[str] = None,
    ) -> dict:
        """Route optimizer params — delegates to TieredEnsembleStrategy logic."""
        return TieredEnsembleStrategy.apply_trial_params(self, cfg, params, side)


class JointPortfolioStrategy(BaseExecutionStrategy):
    """Shared portfolio slot with configurable conflict resolution.

    .. deprecated::
        Use ``TieredEnsembleStrategy`` with ``conflict_resolution`` instead.
        This class is kept for backward compatibility with existing configs
        but all new configs should use ``TieredEnsembleStrategy``.

    Models compete for a single position (``allow_concurrent=False,
    max_concurrent=1``).  When both signals fire simultaneously or
    an opposite signal fires while in position, the ``conflict_resolution``
    config parameter determines the outcome.

    Conflict resolution modes:
        ``ignore_both``
            Both fire while flat → HOLD.  In position → let TP/SL manage exit.
        ``close_existing_position``
            Opposite signal while in position → EXIT current trade.
            Both fire while flat → higher probability wins.
        ``reverse_position``
            Opposite signal while in position → EXIT + ENTER opposite
            in the same bar.  Both fire while flat → higher prob wins.

    Config shape::

        {
            "execution_class": "JointPortfolioStrategy",
            "allow_concurrent": false,
            "max_concurrent": 1,
            "conflict_resolution": "close_existing_position",
            "long": { "tiers": [...], "cooldown_bars": 9, ... },
            "short": { "tiers": [...], "cooldown_bars": 17, ... }
        }
    """

    VALID_CONFLICT_MODES = ("ignore_both", "close_existing_position", "reverse_position")

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        long_cfg = config.get("long", {})
        short_cfg = config.get("short", {})
        self.long_tiers = TieredEnsembleStrategy._parse_tiers(
            long_cfg.get("tiers", []), long_cfg
        )
        self.short_tiers = TieredEnsembleStrategy._parse_tiers(
            short_cfg.get("tiers", []), short_cfg
        )
        self.long_cooldown_bars = long_cfg.get(
            "cooldown_bars", config.get("cooldown_bars", 0)
        )
        self.short_cooldown_bars = short_cfg.get(
            "cooldown_bars", config.get("cooldown_bars", 0)
        )
        self.long_consecutive_threshold = long_cfg.get(
            "consecutive_signal_threshold",
            config.get("consecutive_signal_threshold", 0),
        )
        self.short_consecutive_threshold = short_cfg.get(
            "consecutive_signal_threshold",
            config.get("consecutive_signal_threshold", 0),
        )

        # Conflict resolution mode
        self.conflict_resolution: str = config.get(
            "conflict_resolution", "close_existing_position"
        )
        if self.conflict_resolution not in self.VALID_CONFLICT_MODES:
            raise ValueError(
                f"Invalid conflict_resolution '{self.conflict_resolution}'. "
                f"Must be one of {self.VALID_CONFLICT_MODES}"
            )

        # Derive effective thresholds from tiers
        self.long_threshold: float = (
            min(t["min_prob"] for t in self.long_tiers)
            if self.long_tiers else 1.0
        )
        self.short_threshold: float = (
            min(t["min_prob"] for t in self.short_tiers)
            if self.short_tiers else 1.0
        )

        # Internal per-side state
        self._consecutive_long_signals: int = 0
        self._consecutive_short_signals: int = 0
        self._current_side: int = 0  # 0=flat, 1=long, -1=short
        self._bars_since_long_exit: int = 9999
        self._bars_since_short_exit: int = 9999

    def on_exit(self, side: int, exit_reason: object, bars_held: int) -> None:
        """Track position closure. Cooldown counters reset only on an
        original SL (trailing-sl-no-cooldown_07222026_2050)."""
        self._current_side = 0
        if not exit_reason_arms_cooldown(exit_reason):
            return
        if side == 1:
            self._bars_since_long_exit = 0
        elif side == -1:
            self._bars_since_short_exit = 0

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

        # Increment internal cooldown counters
        self._bars_since_long_exit += 1
        self._bars_since_short_exit += 1

        # ── Evaluate both sides ──
        buy_tier = TieredEnsembleStrategy._match_tier(self, prob_buy, self.long_tiers)
        sell_tier = TieredEnsembleStrategy._match_tier(self, prob_sell, self.short_tiers)
        buy_ok = buy_tier is not None
        sell_ok = sell_tier is not None

        # Consecutive signal tracking — only when this side is flat.
        # Freeze counters while in position so the blocked side keeps its count.
        if self._current_side != 1:
            if buy_ok:
                self._consecutive_long_signals += 1
            elif prob_buy > 0:
                self._consecutive_long_signals = 0
        if self._current_side != -1:
            if sell_ok:
                self._consecutive_short_signals += 1
            elif prob_sell > 0:
                self._consecutive_short_signals = 0

        # Consecutive threshold filtering
        if (self.long_consecutive_threshold > 0
                and self._consecutive_long_signals < self.long_consecutive_threshold):
            buy_ok = False
            buy_tier = None
        if (self.short_consecutive_threshold > 0
                and self._consecutive_short_signals < self.short_consecutive_threshold):
            sell_ok = False
            sell_tier = None

        # Cooldown filtering
        if self._bars_since_long_exit <= self.long_cooldown_bars:
            buy_ok = False
            buy_tier = None
        if self._bars_since_short_exit <= self.short_cooldown_bars:
            sell_ok = False
            sell_tier = None

        # ── IN POSITION ──
        if state.position != 0:
            current_side = state.side
            opposite_ok = (sell_ok if current_side == 1 else buy_ok)
            opposite_tier = (sell_tier if current_side == 1 else buy_tier)
            opposite_prob = (prob_sell if current_side == 1 else prob_buy)

            if self.conflict_resolution == "ignore_both":
                # Let TP/SL/trailing manage the exit entirely
                return HOLD

            elif self.conflict_resolution == "close_existing_position":
                if opposite_ok:
                    # Exit current position; strategy can enter on next bar
                    return [Order(
                        action="EXIT", side=current_side,
                        reason=f"JOINT_EXIT opposite signal ({opposite_prob:.4f})",
                    )]
                return HOLD

            elif self.conflict_resolution == "reverse_position":
                if opposite_ok and opposite_tier is not None:
                    # Exit + enter opposite in same bar
                    exit_order = Order(
                        action="EXIT", side=current_side,
                        reason=f"JOINT_REVERSE exit ({opposite_prob:.4f})",
                    )
                    if current_side == 1:
                        # Was long, reversing to short
                        self._current_side = -1
                        enter_order = TieredEnsembleStrategy._tier_to_order(
                            self, opposite_tier, "SELL", -1, opposite_prob
                        )
                    else:
                        # Was short, reversing to long
                        self._current_side = 1
                        enter_order = TieredEnsembleStrategy._tier_to_order(
                            self, opposite_tier, "BUY", 1, opposite_prob
                        )
                    return [exit_order, enter_order]
                return HOLD

        # ── FLAT ──
        if buy_ok and sell_ok:
            if self.conflict_resolution == "ignore_both":
                # Both fire while flat → abstain
                return HOLD
            # Higher probability wins
            if prob_buy > prob_sell and buy_tier is not None:
                self._current_side = 1
                return [TieredEnsembleStrategy._tier_to_order(
                    self, buy_tier, "BUY", 1, prob_buy
                )]
            elif prob_sell > prob_buy and sell_tier is not None:
                self._current_side = -1
                return [TieredEnsembleStrategy._tier_to_order(
                    self, sell_tier, "SELL", -1, prob_sell
                )]
            else:
                return HOLD

        if buy_ok and buy_tier is not None:
            self._current_side = 1
            return [TieredEnsembleStrategy._tier_to_order(
                self, buy_tier, "BUY", 1, prob_buy
            )]

        if sell_ok and sell_tier is not None:
            self._current_side = -1
            return [TieredEnsembleStrategy._tier_to_order(
                self, sell_tier, "SELL", -1, prob_sell
            )]

        return HOLD

    def apply_trial_params(
        self, cfg: dict, params: dict, side: Optional[str] = None,
    ) -> dict:
        """Route optimizer params — extends TieredEnsembleStrategy with conflict_resolution."""
        cfg = TieredEnsembleStrategy.apply_trial_params(self, cfg, params, side)
        if "conflict_resolution" in params:
            cfg["conflict_resolution"] = params["conflict_resolution"]
        return cfg


# ---------------------------------------------------------------------------
# Strategy Registry / Factory
# ---------------------------------------------------------------------------

STRATEGY_REGISTRY: dict[str, type[BaseExecutionStrategy]] = {
    "SingleModelStrategy": SingleModelStrategy,
    "ConservativeEnsembleStrategy": ConservativeEnsembleStrategy,
    "AggressiveEnsembleStrategy": AggressiveEnsembleStrategy,
    "TieredEnsembleStrategy": TieredEnsembleStrategy,
    "BreakoutStraddleStrategy": BreakoutStraddleStrategy,
    "IsolatedAsymmetricalStrategy": IsolatedAsymmetricalStrategy,
    "JointPortfolioStrategy": JointPortfolioStrategy,
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
