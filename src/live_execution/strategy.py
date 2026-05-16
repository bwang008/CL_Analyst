"""
Strategy abstraction for the Live Execution Engine.

Defines the interface that all trading strategies must implement,
plus the TradeSignal dataclass returned by strategy evaluation.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd


@dataclass
class TradeSignal:
    """Decision output from a strategy's evaluate() method.

    The LiveTrader engine reads these fields to decide whether and how
    to place an order.

    Attributes:
        action:         "BUY", "SELL", or "HOLD".
        probability:    Raw model probability (0-1 range after sigmoid).
        confidence_pct: probability * 100.
        tp_price:       Take-profit price for the bracket order.
        sl_price:       Stop-loss price for the bracket order.
        lots:           Number of contracts to trade.
        signal_label:   Human-readable label for telemetry ("Buy", "Sell", "Hold").
        skip_reason:    If HOLD, why (e.g. "BELOW_THRESHOLD", "POSITION_OPEN",
                        "ATR_INVALID").  None when action != HOLD.
        tp_atr_mult:    Per-trade TP override (None = use engine/config global).
        sl_atr_mult:    Per-trade SL override (None = use engine/config global).
        trailing_atr_mult: Per-trade trailing override (None = use engine/config global).
        max_hold_bars:  Per-trade time-barrier override (None = use engine/config global).
        atr_at_entry:   The side-specific ATR value used for this trade's bracket
                        sizing (for parity with BacktestEngine per-side ATR).
    """

    action: str  # "BUY" | "SELL" | "HOLD"
    probability: float = 0.0
    confidence_pct: float = 0.0
    tp_price: float = 0.0
    sl_price: float = 0.0
    lots: int = 1
    signal_label: str = "Hold"
    skip_reason: Optional[str] = None
    buy_prob: Optional[float] = None
    sell_prob: Optional[float] = None
    # Per-trade overrides (tier system — None = use global config)
    tp_atr_mult: Optional[float] = None
    sl_atr_mult: Optional[float] = None
    trailing_atr_mult: Optional[float] = None
    max_hold_bars: Optional[int] = None
    # Fractional Take-Profit offsets list of (qty_pct, tp_offset amount).
    # None implies SINGLE exit_mode, delegating behavior back to tp_price float.
    tiered_tp_offsets: Optional[list[tuple[float, float]]] = None
    # Side-specific ATR value used for bracket sizing (parity with BacktestEngine)
    atr_at_entry: Optional[float] = None


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class Strategy(ABC):
    """Interface every live-execution strategy must implement."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique strategy identifier, e.g. 'Buy70_Sized_Manatee'."""
        ...

    @property
    @abstractmethod
    def direction(self) -> str:
        """Primary trade direction: 'LONG', 'SHORT', or 'BOTH'."""
        ...

    @property
    @abstractmethod
    def feature_names(self) -> list[str]:
        """List of feature column names the strategy's model expects."""
        ...

    @abstractmethod
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
        """Given features and market state, return a TradeSignal.

        Args:
            features:         Single-row DataFrame of model features.
            current_price:    Latest bar close price.
            atr_value:        Global ATR value (may be None if unavailable).
            current_position: Current CL position in contracts (signed).
                              0 = flat, >0 = long, <0 = short.
            atr_value_long:   Per-side ATR for long brackets (None = use atr_value).
            atr_value_short:  Per-side ATR for short brackets (None = use atr_value).

        Returns:
            TradeSignal with action, bracket levels, sizing, etc.
        """
        ...
