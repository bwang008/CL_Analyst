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
from typing import Any


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
        )
