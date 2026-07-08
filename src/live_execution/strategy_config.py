"""Centralized, strongly-typed strategy configuration.

Single source of truth for all execution parameters consumed by both
BacktestEngine and LiveTrader.  Eliminates duplicated dict-parsing
logic and prevents parity drift between simulation and live trading.

Key resolution for the trailing SL offset (the SL placement after
trailing stop activation):
  1. ``trailing_sl_atr_offset`` — new canonical key (preferred)
  2. ``trailing_activation_mult`` — legacy key (backward-compatible)
  3. ``0.25`` — hardcoded default for configs that predate the parameter

Usage::

    from src.live_execution.strategy_config import StrategyConfig

    sc = StrategyConfig.from_dict(json.load(open("config.json")))
    print(sc.long.trailing_sl_atr_offset)   # per-side value
    print(sc.short.trailing_sl_atr_offset)  # per-side value
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Defaults (single place for magic numbers)
# ---------------------------------------------------------------------------

_DEFAULT_TP_ATR_MULT: float = 2.0
_DEFAULT_SL_ATR_MULT: float = 1.0
_DEFAULT_TRAILING_ATR_MULT: float = 100.0  # effectively disabled
_DEFAULT_TRAILING_SL_ATR_OFFSET: float = 1.0
_DEFAULT_ATR_PERIOD: int = 14
_DEFAULT_MAX_HOLD_BARS: int = 288
_DEFAULT_COOLDOWN_BARS: int = 0
_DEFAULT_CONSECUTIVE_SIGNAL_THRESHOLD: int = 0
_DEFAULT_ENTRY_THRESHOLD: float = 0.45
_DEFAULT_MAX_CONCURRENT: int = 1


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _resolve_trailing_offset(block: dict, global_fallback: float) -> float:
    """Resolve the trailing SL offset from a config block.

    Priority:
      1. ``trailing_sl_atr_offset`` (new canonical key)
      2. ``trailing_activation_mult`` (legacy key)
      3. *global_fallback* (inherited from top-level or hardcoded default)
    """
    if "trailing_sl_atr_offset" in block:
        return float(block["trailing_sl_atr_offset"])
    if "trailing_activation_mult" in block:
        return float(block["trailing_activation_mult"])
    return global_fallback


# ---------------------------------------------------------------------------
# DataClasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SideConfig:
    """Per-side (long/short) execution parameters."""

    tp_atr_mult: float
    sl_atr_mult: float
    trailing_atr_mult: float
    trailing_sl_atr_offset: float
    atr_period: int
    max_hold_bars: int
    cooldown_bars: int
    consecutive_signal_threshold: int


@dataclass(frozen=True)
class WeekendFlattenConfig:
    """Optional weekend-carry flatten overlay (default OFF).

    When ``enabled``, a position that is still open and profitable by at least
    ``profit_atr_mult`` × (ATR-at-entry) is flattened on the last bar before a
    weekend/holiday market gap.  A "gap bar" is any bar whose distance to the
    next bar in the data is >= ``min_gap_hours`` — this catches Friday→Sunday
    weekends and holiday-extended weekends directly from the data's own bar
    spacing (no calendar needed, no lookahead), and is symbol-agnostic.

    Purely additive: when the ``weekend_flatten`` block is absent from a config
    this object is ``None`` and the engine behaves byte-for-byte as before.
    """

    enabled: bool
    profit_atr_mult: float
    min_gap_hours: float


# Structural default: a weekend gap is ~49h (Fri 17:00 → Sun 18:00 ET); a
# single daily maintenance halt is ~1h and a lone mid-week holiday ~24h.  40h
# cleanly isolates weekend / long-weekend gaps from those shorter gaps.
_DEFAULT_WEEKEND_MIN_GAP_HOURS: float = 40.0


def parse_weekend_flatten(cfg: dict) -> Optional[WeekendFlattenConfig]:
    """Parse the optional ``weekend_flatten`` config block.

    Returns ``None`` when the block is absent (feature OFF — unchanged
    behavior).  When present and enabled, ``profit_atr_mult`` is REQUIRED and
    raises if missing (no silent null defaults for an active feature).
    """
    block = cfg.get("weekend_flatten")
    if block is None:
        return None
    if not isinstance(block, dict):
        raise ValueError("weekend_flatten must be a JSON object")
    enabled = bool(block.get("enabled", False))
    if not enabled:
        return WeekendFlattenConfig(
            enabled=False,
            profit_atr_mult=0.0,
            min_gap_hours=_DEFAULT_WEEKEND_MIN_GAP_HOURS,
        )
    if "profit_atr_mult" not in block:
        raise ValueError(
            "weekend_flatten.enabled=true requires an explicit 'profit_atr_mult' "
            "(the unrealized-profit threshold in ATR multiples required to "
            "flatten a winner before a weekend gap)."
        )
    return WeekendFlattenConfig(
        enabled=True,
        profit_atr_mult=float(block["profit_atr_mult"]),
        min_gap_hours=float(
            block.get("min_gap_hours", _DEFAULT_WEEKEND_MIN_GAP_HOURS)
        ),
    )


@dataclass(frozen=True)
class StrategyConfig:
    """Centralized, strongly-typed strategy configuration.

    Both ``BacktestEngine.from_config()`` and ``LiveTrader.__init__()``
    consume this object instead of parsing the raw JSON dict independently.
    """

    # --- Global fallbacks ---------------------------------------------------
    tp_atr_mult: float
    sl_atr_mult: float
    trailing_atr_mult: float
    trailing_sl_atr_offset: float
    atr_period: int
    max_hold_bars: int
    cooldown_bars: int
    entry_threshold: float
    allow_concurrent: bool
    max_concurrent: int

    # --- Per-side configs ---------------------------------------------------
    long: SideConfig
    short: SideConfig

    # --- Raw dict (for downstream consumers that still need it) -------------
    raw: dict

    # --- Optional overlays (default None = feature off) ---------------------
    weekend_flatten: Optional[WeekendFlattenConfig] = None

    # -----------------------------------------------------------------------
    # Factory
    # -----------------------------------------------------------------------

    @classmethod
    def from_dict(cls, cfg: dict) -> "StrategyConfig":
        """Parse a raw JSON config dict into a StrategyConfig.

        The raw dict is preserved as ``self.raw`` for downstream consumers
        (model loading, strategy object creation) that still require it.
        """
        # --- Global values --------------------------------------------------
        g_tp = float(cfg.get("tp_atr_mult", _DEFAULT_TP_ATR_MULT))
        g_sl = float(cfg.get("sl_atr_mult", _DEFAULT_SL_ATR_MULT))
        g_trailing = float(cfg.get("trailing_atr_mult", _DEFAULT_TRAILING_ATR_MULT))
        g_offset = _resolve_trailing_offset(cfg, _DEFAULT_TRAILING_SL_ATR_OFFSET)
        g_atr = int(cfg.get("atr_period", _DEFAULT_ATR_PERIOD))
        g_max_hold = int(cfg.get("max_hold_bars", _DEFAULT_MAX_HOLD_BARS))
        g_cooldown = int(cfg.get("cooldown_bars", _DEFAULT_COOLDOWN_BARS))
        g_threshold = float(cfg.get("entry_threshold", _DEFAULT_ENTRY_THRESHOLD))
        g_concurrent = bool(cfg.get("allow_concurrent", False))
        g_max_conc = int(cfg.get("max_concurrent", _DEFAULT_MAX_CONCURRENT))

        # --- Per-side values ------------------------------------------------
        def _build_side(side_key: str) -> SideConfig:
            block = cfg.get(side_key, {})
            if not isinstance(block, dict):
                block = {}
            return SideConfig(
                tp_atr_mult=float(block.get("tp_atr_mult", g_tp)),
                sl_atr_mult=float(block.get("sl_atr_mult", g_sl)),
                trailing_atr_mult=float(block.get("trailing_atr_mult", g_trailing)),
                trailing_sl_atr_offset=_resolve_trailing_offset(block, g_offset),
                atr_period=int(block.get("atr_period", g_atr)),
                max_hold_bars=int(block.get("max_hold_bars", g_max_hold)),
                cooldown_bars=int(block.get("cooldown_bars", g_cooldown)),
                consecutive_signal_threshold=int(
                    block.get("consecutive_signal_threshold",
                              _DEFAULT_CONSECUTIVE_SIGNAL_THRESHOLD)
                ),
            )

        return cls(
            tp_atr_mult=g_tp,
            sl_atr_mult=g_sl,
            trailing_atr_mult=g_trailing,
            trailing_sl_atr_offset=g_offset,
            atr_period=g_atr,
            max_hold_bars=g_max_hold,
            cooldown_bars=g_cooldown,
            entry_threshold=g_threshold,
            allow_concurrent=g_concurrent,
            max_concurrent=g_max_conc,
            long=_build_side("long"),
            short=_build_side("short"),
            raw=cfg,
            weekend_flatten=parse_weekend_flatten(cfg),
        )
