"""
BacktestEngine — Config-Driven Trade Management Backtester.

Master backtesting engine for CL futures strategies.  All strategy
parameters are loaded from a JSON config file (configs/strategies/*.json).

Capabilities:
- Single-position FSM mode (FLAT / IN_POSITION / COOLDOWN)
- Concurrent multi-position mode (configurable max open positions)
- Time-barrier exit (configurable max hold bars)
- Trailing stop to breakeven (+N×ATR in favor → SL moves to entry)
- Post-stop-out cooldown period (configurable bars)
- Gap-aware slippage (fills at Open when bar gaps past stop)

Usage:
    conda activate trader
    python agent/backtest_engine.py --config configs/strategies/manatee.json --predictions reports/exp017_long_predictions.csv --data data/processed/CL_set_06.parquet
    python agent/backtest_engine.py --config configs/strategies/manatee.json --predictions ... --live-data data/live_session_feed.parquet

Telemetry DBs (for comparing backtest vs live):
    live_telemetry.db        - Manatee (client_id default), Feb 25 - Mar 2 2026
    live_telemetry_cid10.db  - Manatee (client_id 10), Mar 3 - Mar 5 2026
    live_telemetry_cid13.db  - ManateeKoala_Conservative ensemble (client_id 13), Mar 2 - Mar 6 2026
    Tables: market_bars, trade_ledger, raw_front_month_bars, shadow_log (features_json + prob_buy/prob_sell), tradebook_events

Known Parity Gap (2026-03-08 analysis):
    52/80 features diverge >2σ between live system and processed parquet.
    Worst: LIQ_AMIHUD_10080 (163x), VOL_YZ_4032 (136x), MACRO_WIDTH_3M (63x).
    Root causes:
      1. Lookback mismatch: live warm start = 60 days, parquet = 15+ years of history
         → long-window indicators (10080 bars = 35 days) are most affected
      2. OHLCV source: live uses IBKR continuous contract, parquet uses cl-5m_bk.csv
      3. Normalization: parquet normalizes globally; live computes features on-the-fly
    Impact: Buy model prob_buy maxes at 0.45 live (never fires at 0.6 threshold),
            but backtest shows 68% WR on the same model. Sell model IS firing live.

Author: CL Analyst
"""


from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

import numpy as np
import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.live_execution.strategies.execution_models import (
    BaseExecutionStrategy,
    EngineState,
    Order,
    HOLD,
    create_execution_strategy,
)


# ---------------------------------------------------------------------------
# Enums & Data Structures
# ---------------------------------------------------------------------------


class TradeState(Enum):
    """Finite State Machine states for trade management.

    Transitions:
        FLAT ──[buy signal ≥ threshold]──→ IN_POSITION
        IN_POSITION ──[TP hit]──→ COOLDOWN (tp_cooldown_bars)
        IN_POSITION ──[SL hit]──→ COOLDOWN (sl_cooldown_bars)
        IN_POSITION ──[trailing stop hit]──→ COOLDOWN (tp_cooldown_bars)
        IN_POSITION ──[288 bars elapsed]──→ FLAT
        COOLDOWN ──[cooldown elapsed]──→ FLAT
    """

    FLAT = auto()
    IN_POSITION = auto()
    COOLDOWN = auto()


class ExitReason(Enum):
    """Why a trade was closed."""

    TP = "TP"
    SL = "SL"
    TRAILING_BE = "TRAILING_BE"  # Trailing stop hit at breakeven
    TIME_BARRIER = "TIME_BARRIER"  # 288 bars elapsed


@dataclass
class TradeRecord:
    """Result of a single completed trade."""

    entry_dt: pd.Timestamp
    exit_dt: pd.Timestamp
    entry_price: float
    exit_price: float
    entry_fill: float
    exit_fill: float
    side: int  # +1 long, -1 short
    atr_at_entry: float
    exit_reason: ExitReason
    duration_bars: int
    gross_pnl_dollars: float
    commission_dollars: float
    net_pnl_dollars: float
    lots: int = 1


@dataclass
class BacktestResult:
    """Aggregate results from a backtest run."""

    trades: list[TradeRecord] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    label: str = ""
    start_dt: pd.Timestamp | None = None
    end_dt: pd.Timestamp | None = None

    @property
    def total_pnl(self) -> float:
        return sum(t.net_pnl_dollars for t in self.trades)

    @property
    def trade_count(self) -> int:
        return len(self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        wins = sum(1 for t in self.trades if t.net_pnl_dollars > 0)
        return wins / len(self.trades)

    @property
    def profit_factor(self) -> float:
        gross_profit = sum(
            t.net_pnl_dollars for t in self.trades if t.net_pnl_dollars > 0
        )
        gross_loss = abs(
            sum(t.net_pnl_dollars for t in self.trades if t.net_pnl_dollars < 0)
        )
        if gross_loss == 0:
            return float("inf") if gross_profit > 0 else 0.0
        return gross_profit / gross_loss

    @property
    def max_drawdown(self) -> float:
        """Peak-to-trough drawdown from the bar-by-bar equity curve.

        Uses the full equity curve (realized + floating PnL) when
        available, falling back to closed-trade PnL when it is not.
        """
        if self.equity_curve:
            curve = np.array(self.equity_curve)
            running_max = np.maximum.accumulate(curve)
            drawdowns = curve - running_max
            return float(np.min(drawdowns))
        if not self.trades:
            return 0.0
        cum_pnl = np.cumsum([t.net_pnl_dollars for t in self.trades])
        running_max = np.maximum.accumulate(cum_pnl)
        drawdowns = cum_pnl - running_max
        return float(np.min(drawdowns))

    @property
    def exit_distribution(self) -> dict[str, dict[str, float]]:
        """Return count and percentage for each exit reason."""
        total = len(self.trades)
        if total == 0:
            return {}
        counts: dict[str, int] = {}
        for t in self.trades:
            key = t.exit_reason.value
            counts[key] = counts.get(key, 0) + 1
        return {
            reason: {"count": count, "pct": count / total * 100}
            for reason, count in counts.items()
        }


# ---------------------------------------------------------------------------
# Open Position Tracking (for concurrent mode)
# ---------------------------------------------------------------------------


@dataclass
class _OpenPosition:
    """State for a single open position (used in concurrent mode)."""

    entry_dt: pd.Timestamp
    entry_price: float
    entry_fill: float
    atr_at_entry: float
    side: int
    tp_price: float
    sl_price: float
    original_sl_price: float
    trailing_activated: bool = False
    bars_held: int = 0
    highest_high: float = 0.0
    lowest_low: float = float("inf")
    lots: int = 1
    # Per-trade overrides (None = use engine global)
    pos_max_horizon: Optional[int] = None
    pos_trailing_atr_mult: Optional[float] = None


# ---------------------------------------------------------------------------
# Backtest Engine
# ---------------------------------------------------------------------------


# Keep old name as alias for backward-compatibility with existing imports
class BacktestEngine:
    """Config-driven bar-by-bar backtester with FSM trade management.

    Supports two modes controlled by ``allow_concurrent``:

    **Single-position mode** (default, ``allow_concurrent=False``):
        FLAT → IN_POSITION → COOLDOWN → FLAT
        Only one trade at a time.  Matches live trader behaviour.

    **Concurrent mode** (``allow_concurrent=True``):
        Accepts new signals while positions are open (up to
        ``max_concurrent``).  Each position is independently managed
        with TP/SL/trailing/time-barrier.  No cooldown between trades.

    Args:
        tp_atr_mult: ATR multiplier for take-profit barrier.
        sl_atr_mult: ATR multiplier for stop-loss barrier.
        max_horizon: Max bars to hold a position (time barrier).
        cooldown_bars: Deprecated alias — if tp/sl not set, used for both.
        tp_cooldown_bars: Bars to wait after a TP or trailing exit.
        sl_cooldown_bars: Bars to wait after a SL exit.
        trailing_atr_mult: ATR move in favor to trigger trailing stop.
        trailing_sl_atr_offset: After trailing triggers, SL moves to
                           entry + offset×ATR (0 = breakeven, 0.25 = small profit).
        atr_period: Period for ATR calculation.
        commission_per_side: Flat commission per side in dollars.
        slippage_per_side: Slippage penalty per side in price units.
        contract_multiplier: Dollar value per 1.0 price move (CL = 1000).
        prob_threshold: Minimum probability to accept a buy signal.
        allow_concurrent: If True, allow multiple simultaneous positions.
        max_concurrent: Max open positions in concurrent mode.
    """

    def __init__(
        self,
        *,
        tp_atr_mult: float = 2.0,
        sl_atr_mult: float = 1.0,
        max_horizon: int = 288,
        cooldown_bars: int = 5,
        tp_cooldown_bars: Optional[int] = None,
        sl_cooldown_bars: Optional[int] = None,
        trailing_atr_mult: float = 1.0,
        trailing_sl_atr_offset: float = 0.25,
        atr_period: int = 14,
        commission_per_side: float = 2.50,
        slippage_per_side: float = 0.03,
        contract_multiplier: float = 1000.0,
        prob_threshold: float = 0.45,
        allow_concurrent: bool = False,
        max_concurrent: int = 1,
        execution_strategy: Optional[BaseExecutionStrategy] = None,
        consecutive_signal_threshold: int = 0,
    ) -> None:
        self.tp_atr_mult = tp_atr_mult
        self.sl_atr_mult = sl_atr_mult
        self.max_horizon = max_horizon
        self.cooldown_bars = cooldown_bars  # backward compat
        self.tp_cooldown_bars = tp_cooldown_bars if tp_cooldown_bars is not None else cooldown_bars
        self.sl_cooldown_bars = sl_cooldown_bars if sl_cooldown_bars is not None else cooldown_bars
        self.trailing_atr_mult = trailing_atr_mult
        self.trailing_sl_atr_offset = trailing_sl_atr_offset
        self.atr_period = atr_period
        self.commission_per_side = commission_per_side
        self.slippage_per_side = slippage_per_side
        self.contract_multiplier = contract_multiplier
        self.prob_threshold = prob_threshold
        self.allow_concurrent = allow_concurrent
        self.max_concurrent = max(1, max_concurrent)

        # Consecutive signal threshold: require N consecutive above-threshold
        # signals before executing a trade (0 = disabled, immediate entry).
        self.consecutive_signal_threshold = consecutive_signal_threshold

        # Pluggable execution strategy (None = legacy signal_sides fallback)
        self._execution_strategy = execution_strategy

        # Mutable engine state (allocated once, reused across bars)
        self._engine_state = EngineState()

        # FSM state — single-position mode (reset per run)
        self._state: TradeState = TradeState.FLAT
        self._entry_dt: Optional[pd.Timestamp] = None
        self._entry_price: float = 0.0
        self._entry_fill: float = 0.0
        self._atr_at_entry: float = 0.0
        self._side: int = 0
        self._tp_price: float = 0.0
        self._sl_price: float = 0.0
        self._original_sl_price: float = 0.0
        self._trailing_activated: bool = False
        self._bars_held: int = 0
        self._highest_high: float = 0.0
        self._lowest_low: float = float("inf")
        self._cooldown_remaining: int = 0
        self._lots: int = 1
        self._trades: list[TradeRecord] = []
        # Per-trade overrides (set at entry from Order, reset on close)
        self._trade_max_horizon: int = max_horizon
        self._trade_trailing_atr_mult: float = trailing_atr_mult

        # Concurrent mode state
        self._open_positions: list[_OpenPosition] = []

    @classmethod
    def from_config(cls, cfg: dict, **overrides) -> "BacktestEngine":
        """Create a BacktestEngine from a strategy config dict.

        Reads all supported fields from the JSON config, with CLI overrides
        taking precedence.  Also instantiates the appropriate execution
        strategy via the registry/factory pattern.
        """
        # Instantiate the execution strategy from config
        strategy = create_execution_strategy(cfg)

        kwargs = {
            "tp_atr_mult": cfg.get("tp_atr_mult", 2.0),
            "sl_atr_mult": cfg.get("sl_atr_mult", 1.0),
            "prob_threshold": cfg.get("entry_threshold", 0.45),
            "allow_concurrent": cfg.get("allow_concurrent", False),
            "max_concurrent": cfg.get("max_concurrent", 1),
            "cooldown_bars": cfg.get("cooldown_bars", 5),
            "tp_cooldown_bars": cfg.get("tp_cooldown_bars"),
            "sl_cooldown_bars": cfg.get("sl_cooldown_bars"),
            "trailing_atr_mult": cfg.get("trailing_atr_mult", 1.0),
            "trailing_sl_atr_offset": cfg.get("trailing_sl_atr_offset", 0.25),
            "max_horizon": cfg.get("max_hold_bars", 288),
            "execution_strategy": strategy,
        }
        kwargs["consecutive_signal_threshold"] = cfg.get(
            "consecutive_signal_threshold", 0
        )
        kwargs.update(overrides)
        return cls(**kwargs)

    def _reset_state(self) -> None:
        """Reset all FSM state for a new run."""
        self._state = TradeState.FLAT
        self._entry_dt = None
        self._entry_price = 0.0
        self._entry_fill = 0.0
        self._atr_at_entry = 0.0
        self._side = 0
        self._tp_price = 0.0
        self._sl_price = 0.0
        self._original_sl_price = 0.0
        self._trailing_activated = False
        self._bars_held = 0
        self._highest_high = 0.0
        self._lowest_low = float("inf")
        self._cooldown_remaining = 0
        self._trades = []
        self._realized_pnl: float = 0.0
        self._equity_curve: list[float] = []
        self._open_positions = []
        self._trade_max_horizon = self.max_horizon
        self._trade_trailing_atr_mult = self.trailing_atr_mult

        # Consecutive signal counters
        self._consecutive_buy_count: int = 0
        self._consecutive_sell_count: int = 0

        # Reset mutable engine state
        self._engine_state.position = 0
        self._engine_state.side = 0
        self._engine_state.bars_held = 0
        self._engine_state.open_positions = 0

    def _apply_slippage(self, price: float, order_side: str) -> float:
        """Apply 1-tick slippage penalty in the adverse direction.

        Args:
            price: Ideal fill price.
            order_side: 'Buy' or 'Sell'.

        Returns:
            Adjusted fill price (worse for the trader).
        """
        if order_side == "Buy":
            return price + self.slippage_per_side
        return price - self.slippage_per_side

    def _gap_fill_price(
        self, bar_open: float, target_price: float, side: int, is_tp: bool
    ) -> float:
        """Determine fill price accounting for gaps.

        If the bar opens past the target, the fill occurs at the open
        (not the ideal target price) — simulating real gap behavior.

        Args:
            bar_open: Open price of the current bar.
            target_price: The TP or SL barrier price.
            side: +1 for long, -1 for short.
            is_tp: True if this is a take-profit fill.

        Returns:
            Actual fill price (may be worse or better than target).
        """
        if side == 1:  # Long position
            if is_tp:
                # TP: want price to go UP past target
                if bar_open >= target_price:
                    return bar_open  # Gap up past TP — fill at open
                return target_price
            else:
                # SL: price goes DOWN past target
                if bar_open <= target_price:
                    return bar_open  # Gap down past SL — fill at open (worse)
                return target_price
        else:  # Short position
            if is_tp:
                # TP: want price to go DOWN past target
                if bar_open <= target_price:
                    return bar_open
                return target_price
            else:
                # SL: price goes UP past target
                if bar_open >= target_price:
                    return bar_open
                return target_price

    def _close_trade(
        self,
        exit_dt: pd.Timestamp,
        exit_price: float,
        exit_reason: ExitReason,
    ) -> None:
        """Record a completed trade and transition FSM state."""
        exit_order_side = "Sell" if self._side == 1 else "Buy"
        exit_fill = self._apply_slippage(exit_price, exit_order_side)

        gross_pnl_price = self._side * (exit_fill - self._entry_fill)
        gross_pnl_dollars = gross_pnl_price * self.contract_multiplier * self._lots
        commission = 2 * self.commission_per_side * self._lots
        net_pnl = gross_pnl_dollars - commission

        record = TradeRecord(
            entry_dt=self._entry_dt,  # type: ignore[arg-type]
            exit_dt=exit_dt,
            entry_price=self._entry_price,
            exit_price=exit_price,
            entry_fill=self._entry_fill,
            exit_fill=exit_fill,
            side=self._side,
            atr_at_entry=self._atr_at_entry,
            exit_reason=exit_reason,
            duration_bars=self._bars_held,
            gross_pnl_dollars=gross_pnl_dollars,
            commission_dollars=commission,
            net_pnl_dollars=net_pnl,
            lots=self._lots,
        )
        self._trades.append(record)
        self._realized_pnl += net_pnl

        # FSM transition: apply exit-type-specific cooldown
        if exit_reason == ExitReason.SL:
            self._state = TradeState.COOLDOWN
            self._cooldown_remaining = self.sl_cooldown_bars
        elif exit_reason in (ExitReason.TP, ExitReason.TRAILING_BE):
            self._state = TradeState.COOLDOWN
            self._cooldown_remaining = self.tp_cooldown_bars
        else:
            # TIME_BARRIER and any other exits → FLAT (no cooldown)
            self._state = TradeState.FLAT

    def _on_flat(
        self,
        dt: pd.Timestamp,
        bar: pd.Series,
        signal_side: Optional[int],
        atr: float,
        lots: int = 1,
        order: Optional[Order] = None,
    ) -> None:
        """FLAT state: accept valid signals and enter a position.

        Args:
            dt: Bar timestamp.
            bar: OHLCV bar data.
            signal_side: +1 for buy, -1 for sell, None for no signal.
            atr: Current ATR value.
            lots: Number of contracts for this position.
            order: Optional Order carrying per-trade overrides.
        """
        if signal_side is None or np.isnan(atr) or atr <= 0:
            return

        # Resolve per-trade overrides (Order fields take priority over globals)
        tp_mult = self.tp_atr_mult
        sl_mult = self.sl_atr_mult
        if order is not None:
            if order.tp_atr_mult is not None:
                tp_mult = order.tp_atr_mult
            if order.sl_atr_mult is not None:
                sl_mult = order.sl_atr_mult
            self._trade_max_horizon = (
                order.max_hold_bars if order.max_hold_bars is not None
                else self.max_horizon
            )
            self._trade_trailing_atr_mult = (
                order.trailing_atr_mult if order.trailing_atr_mult is not None
                else self.trailing_atr_mult
            )
        else:
            self._trade_max_horizon = self.max_horizon
            self._trade_trailing_atr_mult = self.trailing_atr_mult

        self._state = TradeState.IN_POSITION
        self._entry_dt = dt
        self._entry_price = bar.Close
        self._atr_at_entry = atr
        self._side = signal_side
        self._lots = lots
        self._bars_held = 0
        self._trailing_activated = False

        entry_order_side = "Buy" if signal_side == 1 else "Sell"
        self._entry_fill = self._apply_slippage(self._entry_price, entry_order_side)

        if signal_side == 1:
            self._tp_price = self._entry_price + tp_mult * atr
            self._sl_price = self._entry_price - sl_mult * atr
            self._highest_high = bar.High
            self._lowest_low = bar.Low
        else:
            self._tp_price = self._entry_price - tp_mult * atr
            self._sl_price = self._entry_price + sl_mult * atr
            self._highest_high = bar.High
            self._lowest_low = bar.Low

        self._original_sl_price = self._sl_price

    def _on_in_position(self, dt: pd.Timestamp, bar_open: float,
                        bar_high: float, bar_low: float) -> None:
        """IN_POSITION state: manage an active trade (pessimistic).

        Checks:
        1. Time-barrier exit
        2. Evaluate both TP and SL — if BOTH breach, SL wins
        3. Trailing stop upgrade to breakeven
        """
        self._bars_held += 1

        # Track extremes since entry
        self._highest_high = max(self._highest_high, bar_high)
        self._lowest_low = min(self._lowest_low, bar_low)

        # 1. Time-barrier exit
        if self._bars_held > self._trade_max_horizon:
            self._close_trade(dt, bar_open, ExitReason.TIME_BARRIER)
            return

        # 2. Evaluate BOTH TP and SL — pessimistic: SL wins on same-bar
        if self._side == 1:
            tp_hit = bar_high >= self._tp_price
            sl_hit = bar_low <= self._sl_price
        else:
            tp_hit = bar_low <= self._tp_price
            sl_hit = bar_high >= self._sl_price

        if tp_hit and sl_hit:
            # Both barriers breached — assume worst case (SL)
            exit_price = self._gap_fill_price(
                bar_open, self._sl_price, self._side, is_tp=False
            )
            reason = ExitReason.TRAILING_BE if self._trailing_activated else ExitReason.SL
            self._close_trade(dt, exit_price, reason)
            return

        if sl_hit:
            exit_price = self._gap_fill_price(
                bar_open, self._sl_price, self._side, is_tp=False
            )
            reason = ExitReason.TRAILING_BE if self._trailing_activated else ExitReason.SL
            self._close_trade(dt, exit_price, reason)
            return

        if tp_hit:
            exit_price = self._gap_fill_price(
                bar_open, self._tp_price, self._side, is_tp=True
            )
            self._close_trade(dt, exit_price, ExitReason.TP)
            return

        # 3. Trailing stop upgrade: move SL after +N×ATR in favor
        #    SL target = entry ± offset×ATR (0 = breakeven, >0 = lock profit)
        if not self._trailing_activated:
            if self._side == 1:
                if self._highest_high >= (
                    self._entry_price + self._trade_trailing_atr_mult * self._atr_at_entry
                ):
                    self._sl_price = (
                        self._entry_price
                        + self.trailing_sl_atr_offset * self._atr_at_entry
                    )
                    self._trailing_activated = True
            else:
                if self._lowest_low <= (
                    self._entry_price - self._trade_trailing_atr_mult * self._atr_at_entry
                ):
                    self._sl_price = (
                        self._entry_price
                        - self.trailing_sl_atr_offset * self._atr_at_entry
                    )
                    self._trailing_activated = True

    def _on_cooldown(self) -> None:
        """COOLDOWN state: decrement counter and transition to FLAT when done.

        All signals are rejected during cooldown.
        """
        self._cooldown_remaining -= 1
        if self._cooldown_remaining <= 0:
            self._state = TradeState.FLAT

    # -------------------------------------------------------------------
    # Concurrent-mode helpers
    # -------------------------------------------------------------------

    def _open_new_position(
        self,
        dt: pd.Timestamp,
        bar: pd.Series,
        signal_side: int,
        atr: float,
        lots: int = 1,
        order: Optional[Order] = None,
    ) -> None:
        """Open a new position and add it to the open-positions list."""
        entry_price = bar.Close
        entry_order_side = "Buy" if signal_side == 1 else "Sell"
        entry_fill = self._apply_slippage(entry_price, entry_order_side)

        # Resolve per-trade overrides
        tp_mult = self.tp_atr_mult
        sl_mult = self.sl_atr_mult
        pos_max_horizon: Optional[int] = None
        pos_trailing_atr_mult: Optional[float] = None
        if order is not None:
            if order.tp_atr_mult is not None:
                tp_mult = order.tp_atr_mult
            if order.sl_atr_mult is not None:
                sl_mult = order.sl_atr_mult
            if order.max_hold_bars is not None:
                pos_max_horizon = order.max_hold_bars
            if order.trailing_atr_mult is not None:
                pos_trailing_atr_mult = order.trailing_atr_mult

        if signal_side == 1:
            tp_price = entry_price + tp_mult * atr
            sl_price = entry_price - sl_mult * atr
        else:
            tp_price = entry_price - tp_mult * atr
            sl_price = entry_price + sl_mult * atr

        pos = _OpenPosition(
            entry_dt=dt,
            entry_price=entry_price,
            entry_fill=entry_fill,
            atr_at_entry=atr,
            side=signal_side,
            tp_price=tp_price,
            sl_price=sl_price,
            original_sl_price=sl_price,
            highest_high=bar.High,
            lowest_low=bar.Low,
            lots=lots,
            pos_max_horizon=pos_max_horizon,
            pos_trailing_atr_mult=pos_trailing_atr_mult,
        )
        self._open_positions.append(pos)

    def _check_position(
        self,
        pos: _OpenPosition,
        dt: pd.Timestamp,
        bar_open: float,
        bar_high: float,
        bar_low: float,
    ) -> Optional[TradeRecord]:
        """Check an open position for exit conditions (pessimistic).

        If both TP and SL breach on the same bar, SL wins.
        Returns a TradeRecord if the position closed, else None.
        """
        pos.bars_held += 1

        pos.highest_high = max(pos.highest_high, bar_high)
        pos.lowest_low = min(pos.lowest_low, bar_low)

        exit_price: Optional[float] = None
        exit_reason: Optional[ExitReason] = None

        # 1. Time barrier (use per-position override if set)
        effective_horizon = pos.pos_max_horizon if pos.pos_max_horizon is not None else self.max_horizon
        if pos.bars_held > effective_horizon:
            exit_price = bar_open
            exit_reason = ExitReason.TIME_BARRIER

        # 2. Evaluate BOTH TP and SL — pessimistic: SL wins on conflict
        if exit_reason is None:
            if pos.side == 1:
                tp_hit = bar_high >= pos.tp_price
                sl_hit = bar_low <= pos.sl_price
            else:
                tp_hit = bar_low <= pos.tp_price
                sl_hit = bar_high >= pos.sl_price

            if tp_hit and sl_hit:
                exit_price = self._gap_fill_price(
                    bar_open, pos.sl_price, pos.side, is_tp=False
                )
                exit_reason = (
                    ExitReason.TRAILING_BE if pos.trailing_activated
                    else ExitReason.SL
                )
            elif sl_hit:
                exit_price = self._gap_fill_price(
                    bar_open, pos.sl_price, pos.side, is_tp=False
                )
                exit_reason = (
                    ExitReason.TRAILING_BE if pos.trailing_activated
                    else ExitReason.SL
                )
            elif tp_hit:
                exit_price = self._gap_fill_price(
                    bar_open, pos.tp_price, pos.side, is_tp=True
                )
                exit_reason = ExitReason.TP

        # 3. Trailing stop upgrade (use per-position override if set)
        eff_trailing = pos.pos_trailing_atr_mult if pos.pos_trailing_atr_mult is not None else self.trailing_atr_mult
        if exit_reason is None and not pos.trailing_activated:
            if pos.side == 1:
                if pos.highest_high >= (
                    pos.entry_price + eff_trailing * pos.atr_at_entry
                ):
                    pos.sl_price = (
                        pos.entry_price
                        + self.trailing_sl_atr_offset * pos.atr_at_entry
                    )
                    pos.trailing_activated = True
            else:
                if pos.lowest_low <= (
                    pos.entry_price - eff_trailing * pos.atr_at_entry
                ):
                    pos.sl_price = (
                        pos.entry_price
                        - self.trailing_sl_atr_offset * pos.atr_at_entry
                    )
                    pos.trailing_activated = True

        if exit_reason is not None and exit_price is not None:
            exit_order_side = "Sell" if pos.side == 1 else "Buy"
            exit_fill = self._apply_slippage(exit_price, exit_order_side)
            gross_pnl_price = pos.side * (exit_fill - pos.entry_fill)
            gross_pnl_dollars = gross_pnl_price * self.contract_multiplier * pos.lots
            commission = 2 * self.commission_per_side * pos.lots
            net_pnl = gross_pnl_dollars - commission

            return TradeRecord(
                entry_dt=pos.entry_dt,
                exit_dt=dt,
                entry_price=pos.entry_price,
                exit_price=exit_price,
                entry_fill=pos.entry_fill,
                exit_fill=exit_fill,
                side=pos.side,
                atr_at_entry=pos.atr_at_entry,
                exit_reason=exit_reason,
                duration_bars=pos.bars_held,
                gross_pnl_dollars=gross_pnl_dollars,
                commission_dollars=commission,
                net_pnl_dollars=net_pnl,
                lots=pos.lots,
            )

        return None

    # -------------------------------------------------------------------
    # Main run method
    # -------------------------------------------------------------------

    def run(
        self,
        signals_df: pd.DataFrame,
        ohlcv_df: pd.DataFrame,
        *,
        label: str = "",
    ) -> BacktestResult:
        """Run the backtest bar-by-bar over the OHLCV data.

        Dispatches to single-position FSM or concurrent mode based on
        ``self.allow_concurrent``.

        Args:
            signals_df: DataFrame indexed by DateTime with signal columns.
                        Must contain 'side' column (+1/-1) for signal bars
                        OR 'prob_Buy' column for probability-based filtering.
            ohlcv_df: Full OHLCV DataFrame indexed by DateTime with columns:
                      Open, High, Low, Close, Volume.
            label: Human-readable label for this run (e.g., "Historical").

        Returns:
            BacktestResult with all completed trades and aggregate metrics.
        """
        self._reset_state()

        # Compute ATR on the OHLCV data
        ohlcv = ohlcv_df.copy()
        tr = np.maximum(
            ohlcv["High"] - ohlcv["Low"],
            np.maximum(
                (ohlcv["High"] - ohlcv["Close"].shift(1)).abs(),
                (ohlcv["Low"] - ohlcv["Close"].shift(1)).abs(),
            ),
        )
        ohlcv["atr_"] = tr.rolling(self.atr_period).mean()

        # Build signal lookup — which bars have a trade signal
        #
        # Two code paths:
        #   1. Strategy-aware: delegate to execution strategy on_bar()
        #   2. Legacy: build signal_sides dict from columns (backward compat)
        #
        if self._execution_strategy is not None:
            # Strategy-aware path: build prob lookup dicts
            prob_buy_lookup: dict[pd.Timestamp, float] = {}
            prob_sell_lookup: dict[pd.Timestamp, float] = {}

            buy_col = _resolve_prob_column(signals_df, "buy")
            sell_col = _resolve_prob_column(signals_df, "sell")
            if buy_col:
                for dt_idx in signals_df.index:
                    prob_buy_lookup[pd.Timestamp(dt_idx)] = float(
                        signals_df.at[dt_idx, buy_col]
                    )
            if sell_col:
                for dt_idx in signals_df.index:
                    prob_sell_lookup[pd.Timestamp(dt_idx)] = float(
                        signals_df.at[dt_idx, sell_col]
                    )
            # Legacy column fallback: if only 'side' column, map to probs
            if "side" in signals_df.columns and not prob_buy_lookup and not prob_sell_lookup:
                for dt_idx in signals_df.index:
                    s = int(signals_df.at[dt_idx, "side"])
                    if s == 1:
                        prob_buy_lookup[pd.Timestamp(dt_idx)] = 1.0
                    elif s == -1:
                        prob_sell_lookup[pd.Timestamp(dt_idx)] = 1.0
            # Predicted column fallback
            if "Predicted" in signals_df.columns and not prob_buy_lookup:
                mask = signals_df["Predicted"] == 1
                for dt_idx in signals_df[mask].index:
                    prob_buy_lookup[pd.Timestamp(dt_idx)] = 1.0

            if self.allow_concurrent:
                self._run_concurrent_strategy(
                    ohlcv, prob_buy_lookup, prob_sell_lookup
                )
            else:
                self._run_single_strategy(
                    ohlcv, prob_buy_lookup, prob_sell_lookup
                )
        else:
            # Legacy path: build signal_sides dict
            signal_sides: dict[pd.Timestamp, int] = {}

            if "side" in signals_df.columns:
                for dt_idx in signals_df.index:
                    ts = pd.Timestamp(dt_idx)
                    signal_sides[ts] = int(signals_df.at[dt_idx, "side"])
            elif _resolve_prob_column(signals_df, "buy"):
                _buy_col = _resolve_prob_column(signals_df, "buy")
                mask = signals_df[_buy_col] >= self.prob_threshold
                for dt_idx in signals_df[mask].index:
                    signal_sides[pd.Timestamp(dt_idx)] = 1
            elif "Predicted" in signals_df.columns:
                mask = signals_df["Predicted"] == 1
                for dt_idx in signals_df[mask].index:
                    signal_sides[pd.Timestamp(dt_idx)] = 1

            if self.allow_concurrent:
                self._run_concurrent(ohlcv, signal_sides)
            else:
                self._run_single(ohlcv, signal_sides)

        return BacktestResult(
            trades=self._trades,
            equity_curve=self._equity_curve,
            label=label,
            start_dt=ohlcv.index.min() if not ohlcv.empty else None,
            end_dt=ohlcv.index.max() if not ohlcv.empty else None,
        )

    def _floating_pnl_single(self, close: float) -> float:
        """Floating PnL of the single open position."""
        if self._state != TradeState.IN_POSITION:
            return 0.0
        return self._side * (close - self._entry_fill) * self.contract_multiplier

    def _floating_pnl_concurrent(self, close: float) -> float:
        """Total floating PnL of all open concurrent positions."""
        total = 0.0
        for pos in self._open_positions:
            total += pos.side * (close - pos.entry_fill) * self.contract_multiplier
        return total

    def _run_single(
        self,
        ohlcv: pd.DataFrame,
        signal_sides: dict[pd.Timestamp, int],
    ) -> None:
        """Single-position FSM loop using itertuples for speed."""
        for row in ohlcv.itertuples():
            ts = pd.Timestamp(row.Index)
            atr = row.atr_

            if self._state == TradeState.FLAT:
                sig = signal_sides.get(ts)
                self._on_flat(ts, row, sig, atr)

            elif self._state == TradeState.IN_POSITION:
                self._on_in_position(ts, row.Open, row.High, row.Low)

            elif self._state == TradeState.COOLDOWN:
                self._on_cooldown()

            # Record equity: realized + floating
            self._equity_curve.append(
                self._realized_pnl + self._floating_pnl_single(row.Close)
            )

    def _run_concurrent(
        self,
        ohlcv: pd.DataFrame,
        signal_sides: dict[pd.Timestamp, int],
    ) -> None:
        """Concurrent multi-position loop using itertuples for speed.

        On each bar:
        1. Check all open positions for exits (TP/SL/trailing/time)
        2. If a signal is present and we haven't hit max_concurrent, open new
        3. Record equity (realized + floating)
        """
        for row in ohlcv.itertuples():
            ts = pd.Timestamp(row.Index)
            atr = row.atr_

            # 1. Check existing positions for exits
            surviving: list[_OpenPosition] = []
            for pos in self._open_positions:
                record = self._check_position(pos, ts, row.Open, row.High, row.Low)
                if record is not None:
                    self._trades.append(record)
                    self._realized_pnl += record.net_pnl_dollars
                else:
                    surviving.append(pos)
            self._open_positions = surviving

            # 2. Accept new signals if room
            sig = signal_sides.get(ts)
            if (
                sig is not None
                and not (np.isnan(atr) if isinstance(atr, float) else False)
                and atr > 0
                and len(self._open_positions) < self.max_concurrent
            ):
                self._open_new_position(ts, row, sig, atr)

            # 3. Record equity: realized + floating
            self._equity_curve.append(
                self._realized_pnl + self._floating_pnl_concurrent(row.Close)
            )

    # -------------------------------------------------------------------
    # Strategy-aware loop methods
    # -------------------------------------------------------------------

    def _update_engine_state(self) -> None:
        """Sync the mutable EngineState with FSM state (single mode)."""
        es = self._engine_state
        if self._state == TradeState.IN_POSITION:
            es.position = 1
            es.side = self._side
            es.bars_held = self._bars_held
        else:
            es.position = 0
            es.side = 0
            es.bars_held = 0
        es.open_positions = 1 if self._state == TradeState.IN_POSITION else 0

    def _run_single_strategy(
        self,
        ohlcv: pd.DataFrame,
        prob_buy_lookup: dict[pd.Timestamp, float],
        prob_sell_lookup: dict[pd.Timestamp, float],
    ) -> None:
        """Single-position FSM loop with execution strategy delegation."""
        strategy = self._execution_strategy
        assert strategy is not None

        for row in ohlcv.itertuples():
            ts = pd.Timestamp(row.Index)
            atr = row.atr_

            if self._state == TradeState.FLAT:
                # Update engine state for strategy
                self._update_engine_state()

                # Get probabilities for this bar
                pb = prob_buy_lookup.get(ts, 0.0)
                ps = prob_sell_lookup.get(ts, 0.0)

                # Ask strategy what to do
                orders = strategy.on_bar(
                    ts, row.Open, row.High, row.Low, row.Close,
                    atr, pb, ps, self._engine_state,
                )

                # Dispatch orders to existing FSM entry point
                for order in orders:
                    if order.action in ("BUY", "SELL"):
                        # Consecutive signal filter
                        if self.consecutive_signal_threshold > 0:
                            if order.action == "BUY":
                                self._consecutive_buy_count += 1
                                self._consecutive_sell_count = 0
                                if self._consecutive_buy_count < self.consecutive_signal_threshold:
                                    break  # suppress entry, wait for more signals
                                self._consecutive_buy_count = 0  # reset after firing
                            else:  # SELL
                                self._consecutive_sell_count += 1
                                self._consecutive_buy_count = 0
                                if self._consecutive_sell_count < self.consecutive_signal_threshold:
                                    break
                                self._consecutive_sell_count = 0

                        sig = order.side
                        self._on_flat(ts, row, sig, atr, lots=order.lots, order=order)
                        break  # single-position: only one entry per bar
                else:
                    # No BUY/SELL order this bar — reset counters
                    if self.consecutive_signal_threshold > 0:
                        self._consecutive_buy_count = 0
                        self._consecutive_sell_count = 0

            elif self._state == TradeState.IN_POSITION:
                self._on_in_position(ts, row.Open, row.High, row.Low)

            elif self._state == TradeState.COOLDOWN:
                self._on_cooldown()

            # Record equity: realized + floating
            self._equity_curve.append(
                self._realized_pnl + self._floating_pnl_single(row.Close)
            )

    def _run_concurrent_strategy(
        self,
        ohlcv: pd.DataFrame,
        prob_buy_lookup: dict[pd.Timestamp, float],
        prob_sell_lookup: dict[pd.Timestamp, float],
    ) -> None:
        """Concurrent multi-position loop with execution strategy delegation."""
        strategy = self._execution_strategy
        assert strategy is not None

        for row in ohlcv.itertuples():
            ts = pd.Timestamp(row.Index)
            atr = row.atr_

            # 1. Check existing positions for exits
            surviving: list[_OpenPosition] = []
            for pos in self._open_positions:
                record = self._check_position(pos, ts, row.Open, row.High, row.Low)
                if record is not None:
                    self._trades.append(record)
                    self._realized_pnl += record.net_pnl_dollars
                else:
                    surviving.append(pos)
            self._open_positions = surviving

            # 2. Update engine state for strategy
            self._engine_state.open_positions = len(self._open_positions)
            if self._open_positions:
                self._engine_state.position = 1
                self._engine_state.side = self._open_positions[0].side
            else:
                self._engine_state.position = 0
                self._engine_state.side = 0

            # 3. Ask strategy what to do
            pb = prob_buy_lookup.get(ts, 0.0)
            ps = prob_sell_lookup.get(ts, 0.0)
            orders = strategy.on_bar(
                ts, row.Open, row.High, row.Low, row.Close,
                atr, pb, ps, self._engine_state,
            )

            # 4. Dispatch orders
            dispatched = False
            for order in orders:
                if order.action in ("BUY", "SELL"):
                    # Consecutive signal filter
                    if self.consecutive_signal_threshold > 0:
                        if order.action == "BUY":
                            self._consecutive_buy_count += 1
                            self._consecutive_sell_count = 0
                            if self._consecutive_buy_count < self.consecutive_signal_threshold:
                                dispatched = True
                                break
                            self._consecutive_buy_count = 0
                        else:  # SELL
                            self._consecutive_sell_count += 1
                            self._consecutive_buy_count = 0
                            if self._consecutive_sell_count < self.consecutive_signal_threshold:
                                dispatched = True
                                break
                            self._consecutive_sell_count = 0

                    if (
                        not (np.isnan(atr) if isinstance(atr, float) else False)
                        and atr > 0
                        and len(self._open_positions) < self.max_concurrent
                    ):
                        self._open_new_position(ts, row, order.side, atr, lots=order.lots, order=order)
                        dispatched = True
            if not dispatched and self.consecutive_signal_threshold > 0:
                # No BUY/SELL order this bar — reset counters
                self._consecutive_buy_count = 0
                self._consecutive_sell_count = 0

            # 5. Record equity: realized + floating
            self._equity_curve.append(
                self._realized_pnl + self._floating_pnl_concurrent(row.Close)
            )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _safe_wr(wins: int, total: int) -> str:
    """Format win rate, returning '-' if no trades."""
    if total == 0:
        return "   -  "
    return f"{wins / total:>5.1%} "


def _safe_pf(trades: list[TradeRecord]) -> float:
    """Calculate profit factor from a list of trades."""
    gross_profit = sum(t.net_pnl_dollars for t in trades if t.net_pnl_dollars > 0)
    gross_loss = abs(sum(t.net_pnl_dollars for t in trades if t.net_pnl_dollars < 0))
    if gross_loss == 0:
        return float("inf") if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def format_report(
    result: BacktestResult,
    config: dict | None = None,
    predictions_path: str | None = None,
    data_path: str | None = None,
) -> str:
    """Format a BacktestResult as a structured console report.

    Args:
        result: BacktestResult from a backtest run.
        config: Optional strategy config dict for metadata header.
        predictions_path: Optional path shown in the header.
        data_path: Optional data path shown in the header.
    """
    w = 90
    lines: list[str] = []

    # ── Header: Model / Strategy Metadata ──
    lines.append("=" * w)
    title = f"BACKTEST REPORT: {result.label}" if result.label else "BACKTEST REPORT"
    lines.append(title.center(w))
    lines.append("=" * w)

    if config:
        nickname = config.get("nickname", "?")
        models = config.get("models", {})
        if models:
            long_id = models.get("long", {}).get("experiment_id", "N/A")
            short_id = models.get("short", {}).get("experiment_id", "N/A")
            long_thr = models.get("long", {}).get("threshold", "?")
            short_thr = models.get("short", {}).get("threshold", "?")
            lines.append(f"  Strategy:       {nickname}")
            lines.append(f"  Models:         {long_id} (Long) + {short_id} (Short)")
            lines.append(f"  Threshold:      Buy >= {long_thr}, Sell >= {short_thr}")
        else:
            lines.append(f"  Strategy:       {nickname}")
            direction = config.get("direction", "?")
            threshold = config.get("entry_threshold", "?")
            lines.append(f"  Direction:      {direction}")
            lines.append(f"  Threshold:      {threshold}")

        tp = config.get("tp_atr_mult", "?")
        sl = config.get("sl_atr_mult", "?")
        trailing = config.get("trailing_atr_mult", "?")
        lines.append(f"  TP / SL:        {tp}x / {sl}x ATR")
        trailing_note = " (disabled)" if trailing and trailing >= 50 else ""
        lines.append(f"  Trailing:       {trailing}x{trailing_note}")

    if predictions_path:
        lines.append(f"  Predictions:    {predictions_path}")
    if data_path:
        lines.append(f"  Data:           {data_path}")

    # Prediction date range (from actual trades)
    if result.trades:
        entries = [t.entry_dt for t in result.trades]
        lines.append(f"  Trade Range:    {min(entries)} -> {max(entries)}")

    lines.append("=" * w)

    if result.trade_count == 0:
        lines.append("  No trades simulated.")
        lines.append("=" * w)
        return "\n".join(lines)

    # ── Aggregate Summary ──
    lines.append("  AGGREGATE SUMMARY")
    lines.append("-" * w)
    lines.append(f"  Total Trades:     {result.trade_count}")
    lines.append(f"  Win Rate:         {result.win_rate:.1%}")
    lines.append(f"  Profit Factor:    {result.profit_factor:.2f}")
    lines.append(f"  Total Net PnL:    ${result.total_pnl:>14,.2f}")
    lines.append(f"  Max Drawdown:     ${result.max_drawdown:>14,.2f}")

    # ── Monthly Breakdown Table ──
    lines.append("")
    lines.append("=" * w)
    lines.append("  MONTHLY BREAKDOWN")
    lines.append("-" * w)
    hdr = (
        f"  {'Month':<10s} | {'Trades':>6s} | {'Buys':>5s} | {'Sells':>5s} |"
        f" {'WR%':>6s} | {'Buy WR':>6s} | {'Sell WR':>7s} |"
        f" {'Net PnL':>11s} | {'PF':>5s}"
    )
    lines.append(hdr)
    lines.append("-" * w)

    # Group trades by month
    from collections import defaultdict
    monthly: dict[str, list[TradeRecord]] = defaultdict(list)
    for t in result.trades:
        key = t.entry_dt.strftime("%Y-%m")
        monthly[key].append(t)

    current_year = None
    year_trades: list[TradeRecord] = []

    for month_key in sorted(monthly.keys()):
        year = month_key[:4]

        # Yearly subtotal separator
        if current_year is not None and year != current_year and year_trades:
            _append_subtotal(lines, f"{current_year} Total", year_trades, w, bold=True)
            year_trades = []

        current_year = year
        trades = monthly[month_key]
        year_trades.extend(trades)

        _append_month_row(lines, month_key, trades)

    # Final year subtotal
    if current_year is not None and year_trades:
        _append_subtotal(lines, f"{current_year} Total", year_trades, w, bold=True)

    # Grand total
    lines.append("=" * w)
    _append_subtotal(lines, "GRAND TOTAL", result.trades, w, bold=False)
    lines.append("=" * w)

    # ── Exit Distribution by Side ──
    lines.append("")
    lines.append("  EXIT DISTRIBUTION BY SIDE")
    lines.append("-" * w)
    lines.append(
        f"  {'Exit Reason':<16s} | {'Long':>7s} | {'Short':>7s} | {'Total':>7s}"
    )
    lines.append("-" * w)

    long_trades = [t for t in result.trades if t.side == 1]
    short_trades = [t for t in result.trades if t.side == -1]

    for reason in ["TP", "SL", "TRAILING_BE", "TIME_BARRIER"]:
        long_count = sum(
            1 for t in long_trades if t.exit_reason.value == reason
        )
        short_count = sum(
            1 for t in short_trades if t.exit_reason.value == reason
        )
        total_count = long_count + short_count
        lines.append(
            f"  {reason:<16s} | {long_count:>7d} | {short_count:>7d} | {total_count:>7d}"
        )

    lines.append("-" * w)

    # ── Notable Periods ──
    lines.append("")
    lines.append("  NOTABLE PERIODS")
    lines.append("-" * w)

    if monthly:
        # Best / worst month by PnL
        month_pnl = {
            k: sum(t.net_pnl_dollars for t in v) for k, v in monthly.items()
        }
        best_month = max(month_pnl, key=month_pnl.get)  # type: ignore[arg-type]
        worst_month = min(month_pnl, key=month_pnl.get)  # type: ignore[arg-type]
        best_wr = sum(1 for t in monthly[best_month] if t.net_pnl_dollars > 0) / len(monthly[best_month])
        worst_wr = sum(1 for t in monthly[worst_month] if t.net_pnl_dollars > 0) / max(len(monthly[worst_month]), 1)

        lines.append(
            f"  Best Month:       {best_month}  "
            f"(${month_pnl[best_month]:>10,.2f}, {best_wr:.1%} WR, "
            f"{len(monthly[best_month])} trades)"
        )
        lines.append(
            f"  Worst Month:      {worst_month}  "
            f"(${month_pnl[worst_month]:>10,.2f}, {worst_wr:.1%} WR, "
            f"{len(monthly[worst_month])} trades)"
        )

    # Win / loss streaks
    if result.trades:
        max_win_streak = 0
        max_loss_streak = 0
        cur_win = 0
        cur_loss = 0
        for t in result.trades:
            if t.net_pnl_dollars > 0:
                cur_win += 1
                cur_loss = 0
            else:
                cur_loss += 1
                cur_win = 0
            max_win_streak = max(max_win_streak, cur_win)
            max_loss_streak = max(max_loss_streak, cur_loss)

        lines.append(f"  Win Streak:       {max_win_streak} trades")
        lines.append(f"  Loss Streak:      {max_loss_streak} trades")

        best_trade = max(result.trades, key=lambda t: t.net_pnl_dollars)
        worst_trade = min(result.trades, key=lambda t: t.net_pnl_dollars)
        lines.append(
            f"  Best Trade:       ${best_trade.net_pnl_dollars:>10,.2f}  "
            f"({best_trade.entry_dt.strftime('%Y-%m-%d %H:%M')}, "
            f"{'Long' if best_trade.side == 1 else 'Short'})"
        )
        lines.append(
            f"  Worst Trade:      ${worst_trade.net_pnl_dollars:>10,.2f}  "
            f"({worst_trade.entry_dt.strftime('%Y-%m-%d %H:%M')}, "
            f"{'Long' if worst_trade.side == 1 else 'Short'})"
        )

    lines.append("=" * w)
    return "\n".join(lines)


def _append_month_row(
    lines: list[str],
    label: str,
    trades: list[TradeRecord],
) -> None:
    """Append one month row to the report lines."""
    total = len(trades)
    buys = [t for t in trades if t.side == 1]
    sells = [t for t in trades if t.side == -1]
    wins = sum(1 for t in trades if t.net_pnl_dollars > 0)
    buy_wins = sum(1 for t in buys if t.net_pnl_dollars > 0)
    sell_wins = sum(1 for t in sells if t.net_pnl_dollars > 0)
    pnl = sum(t.net_pnl_dollars for t in trades)
    pf = _safe_pf(trades)
    pf_str = f"{pf:>5.2f}" if pf < 100 else "  inf"

    lines.append(
        f"  {label:<10s} | {total:>6d} | {len(buys):>5d} | {len(sells):>5d} |"
        f" {_safe_wr(wins, total)} | {_safe_wr(buy_wins, len(buys))} |"
        f" {_safe_wr(sell_wins, len(sells))}  |"
        f" ${pnl:>10,.2f} | {pf_str}"
    )


def _append_subtotal(
    lines: list[str],
    label: str,
    trades: list[TradeRecord],
    w: int,
    bold: bool = False,
) -> None:
    """Append a subtotal row (year or grand total)."""
    if bold:
        lines.append("-" * w)
    _append_month_row(lines, label, trades)
    if bold:
        lines.append("=" * w)


def compare_runs(
    result_a: BacktestResult,
    result_b: BacktestResult,
) -> str:
    """Format a side-by-side A/B comparison of two backtest runs."""
    w = 70
    lines: list[str] = []
    lines.append("=" * w)
    lines.append("A/B COMPARISON".center(w))
    lines.append("=" * w)

    label_a = result_a.label or "Run A"
    label_b = result_b.label or "Run B"

    header = f"  {'Metric':<24s} {label_a:>18s} {label_b:>18s}"
    lines.append(header)
    lines.append("-" * w)

    rows = [
        ("Trade Count", f"{result_a.trade_count}", f"{result_b.trade_count}"),
        ("Win Rate", f"{result_a.win_rate:.1%}", f"{result_b.win_rate:.1%}"),
        (
            "Profit Factor",
            f"{result_a.profit_factor:.2f}",
            f"{result_b.profit_factor:.2f}",
        ),
        (
            "Total Net PnL",
            f"${result_a.total_pnl:,.2f}",
            f"${result_b.total_pnl:,.2f}",
        ),
        (
            "Max Drawdown",
            f"${result_a.max_drawdown:,.2f}",
            f"${result_b.max_drawdown:,.2f}",
        ),
    ]

    for label, val_a, val_b in rows:
        lines.append(f"  {label:<24s} {val_a:>18s} {val_b:>18s}")

    lines.append("-" * w)
    lines.append("  Exit Distribution:")

    dist_a = result_a.exit_distribution
    dist_b = result_b.exit_distribution
    for reason in ["TP", "SL", "TRAILING_BE", "TIME_BARRIER"]:
        da = dist_a.get(reason, {"count": 0, "pct": 0.0})
        db = dist_b.get(reason, {"count": 0, "pct": 0.0})
        val_a_str = f"{int(da['count'])} ({da['pct']:.1f}%)"
        val_b_str = f"{int(db['count'])} ({db['pct']:.1f}%)"
        lines.append(f"    {reason:<16s} {val_a_str:>18s} {val_b_str:>18s}")

    lines.append("=" * w)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Data Loading Utilities
# ---------------------------------------------------------------------------


def _resolve_prob_column(df: pd.DataFrame, keyword: str) -> str | None:
    """Find a column containing `keyword` (case-insensitive).

    Searches for columns matching common patterns like 'prob_buy',
    'prob_Buy', 'PROB_BUY', 'probability_buy', etc.

    Returns the original column name if found, else None.
    """
    keyword_lower = keyword.lower()
    for col in df.columns:
        if keyword_lower in col.lower():
            return col
    return None


def load_predictions(path: str) -> pd.DataFrame:
    """Load vault predictions CSV, returning a DataFrame with DatetimeIndex.

    Handles both new-format (prob_Buy column) and legacy-format (Predicted 0/1).
    Gracefully skips malformed lines.
    """
    df = pd.read_csv(
        path, index_col=0, parse_dates=True, on_bad_lines="warn"
    )
    return df


def load_ohlcv(path: str) -> pd.DataFrame:
    """Load OHLCV data from parquet or CSV.

    Returns a DataFrame with columns: Open, High, Low, Close, Volume
    indexed by DateTime.
    """
    if path.endswith(".parquet"):
        df = pd.read_parquet(path)
    else:
        df = pd.read_csv(path, index_col=0, parse_dates=True, sep=None, engine="python")

    # Ensure standard column names
    rename_map = {
        "open": "Open",
        "high": "High",
        "low": "Low",
        "close": "Close",
        "volume": "Volume",
    }
    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})

    # If RAW_ columns exist (from processed parquets), use those for OHLC
    if "RAW_Close" in df.columns:
        if "RAW_Open" in df.columns:
            df["Open"] = df["RAW_Open"]
        if "RAW_High" in df.columns:
            df["High"] = df["RAW_High"]
        if "RAW_Low" in df.columns:
            df["Low"] = df["RAW_Low"]
        df["Close"] = df["RAW_Close"]

    required = {"Open", "High", "Low", "Close", "Volume"}
    missing = required - set(df.columns)
    if missing:
        # Fall back to loading raw CSV directly
        from src.data_paths import get_data_path as _gdp
        raw_candidates = [
            str(_gdp("raw/cl-5m_bk.csv")),
            str(_gdp("raw/CL.csv")),
        ]
        loaded = False
        for raw_path in raw_candidates:
            if os.path.exists(raw_path):
                print(f"  Falling back to raw CSV: {raw_path}")
                df = pd.read_csv(
                    raw_path,
                    sep=";",
                    header=None,
                    names=["Date", "Time", "Open", "High", "Low", "Close", "Volume"],
                )
                df["DateTime"] = pd.to_datetime(
                    df["Date"] + " " + df["Time"], dayfirst=True
                )
                df = df.set_index("DateTime")
                df = df.drop(columns=["Date", "Time"])
                loaded = True
                break
        if not loaded:
            raise FileNotFoundError(
                f"OHLCV data missing columns {missing} and no raw CSV found."
            )

    return df


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="BacktestEngine — Config-Driven Strategy Backtester"
    )
    parser.add_argument(
        "--predictions",
        default=None,
        help="Path to predictions CSV. If omitted and --config is given, "
             "auto-resolves from config's models.*.predictions_path",
    )
    parser.add_argument(
        "--data",
        default="data/processed/CL_set_06.parquet",
        help="Path to OHLCV parquet for Run A (historical)",
    )
    parser.add_argument(
        "--live-data",
        default=None,
        help="Optional path to live session OHLCV parquet for Run B",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to a strategy JSON config (reads all parameters)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.45,
        help="Probability threshold (default: 0.45)",
    )
    parser.add_argument(
        "--tp-mult", type=float, default=2.0, help="TP ATR multiplier"
    )
    parser.add_argument(
        "--sl-mult", type=float, default=1.0, help="SL ATR multiplier"
    )
    parser.add_argument(
        "--commission-per-side", type=float, default=2.50, help="Commission per side ($)"
    )
    parser.add_argument(
        "--slippage-per-side", type=float, default=0.03, help="Slippage per side"
    )
    parser.add_argument(
        "--contract-multiplier", type=float, default=1000.0, help="CL multiplier"
    )
    parser.add_argument(
        "--report-file", default=None,
        help="Optional path to save the full report as a text file",
    )
    args = parser.parse_args()

    # Resolve paths via CL_DATA_ROOT fallback
    from src.data_paths import resolve_cli_path
    if args.predictions:
        args.predictions = resolve_cli_path(args.predictions)
    args.data = resolve_cli_path(args.data)
    if args.live_data:
        args.live_data = resolve_cli_path(args.live_data)
    if args.config:
        args.config = resolve_cli_path(args.config)

    # If --config is provided, create engine from strategy JSON
    if args.config is not None:
        with open(args.config) as f:
            strategy_cfg = json.load(f)

        bt = BacktestEngine.from_config(
            strategy_cfg,
            commission_per_side=args.commission_per_side,
            slippage_per_side=args.slippage_per_side,
            contract_multiplier=args.contract_multiplier,
        )
        concurrent_str = (
            f"concurrent={bt.max_concurrent}"
            if bt.allow_concurrent
            else "single-position"
        )
        # Display the correct threshold(s) depending on which path is used
        strat = bt._execution_strategy
        if strat is not None and hasattr(strat, "long_threshold"):
            threshold_str = (
                f"threshold(buy={strat.long_threshold}, sell={strat.short_threshold})"
            )
        elif strat is not None and hasattr(strat, "threshold"):
            threshold_str = f"threshold={strat.threshold}"
        else:
            threshold_str = f"threshold={bt.prob_threshold}"
        print(
            f"Loaded strategy config '{strategy_cfg.get('nickname', '?')}': "
            f"TP={bt.tp_atr_mult}x  SL={bt.sl_atr_mult}x  "
            f"{threshold_str}  [{concurrent_str}]"
        )
        strategy_cfg = strategy_cfg  # already loaded
    else:
        strategy_cfg = None  # no config provided
        bt = BacktestEngine(
            tp_atr_mult=args.tp_mult,
            sl_atr_mult=args.sl_mult,
            prob_threshold=args.threshold,
            commission_per_side=args.commission_per_side,
            slippage_per_side=args.slippage_per_side,
            contract_multiplier=args.contract_multiplier,
        )

    # Auto-resolve predictions from config if not explicitly provided
    if args.predictions is None and strategy_cfg is not None:
        models_cfg = strategy_cfg.get("models", {})
        long_preds_path = models_cfg.get("long", {}).get("predictions_path")
        short_preds_path = models_cfg.get("short", {}).get("predictions_path")

        if long_preds_path and short_preds_path:
            # Dual-model: auto-merge long + short predictions
            long_preds_path = resolve_cli_path(long_preds_path)
            short_preds_path = resolve_cli_path(short_preds_path)
            print(f"Auto-resolving predictions from config (dual-model):")
            print(f"  Long:  {long_preds_path}")
            print(f"  Short: {short_preds_path}")
            long_df = load_predictions(long_preds_path)
            short_df = load_predictions(short_preds_path)
            # Find probability columns (case-insensitive, strict validation)
            long_col = _resolve_prob_column(long_df, "buy")
            short_col = _resolve_prob_column(short_df, "sell")
            if long_col is None:
                raise ValueError(
                    f"Long model predictions ({long_preds_path}) have no column "
                    f"containing 'buy' (found: {list(long_df.columns)}). "
                    f"Cannot silently use another column as prob_buy."
                )
            if short_col is None:
                raise ValueError(
                    f"Short model predictions ({short_preds_path}) have no column "
                    f"containing 'sell' (found: {list(short_df.columns)}). "
                    f"Cannot silently use another column as prob_sell."
                )
            long_probs = long_df[[long_col]].rename(columns={long_col: "prob_Buy"})
            short_probs = short_df[[short_col]].rename(columns={short_col: "prob_Sell"})
            preds = long_probs.join(short_probs, how="outer").fillna(0.0)
            print(f"  Merged: {len(preds):,} rows ({preds['prob_Buy'].gt(0).sum():,} buy signals, {preds['prob_Sell'].gt(0).sum():,} sell signals)")
        elif long_preds_path:
            long_preds_path = resolve_cli_path(long_preds_path)
            print(f"Auto-resolving predictions from config: {long_preds_path}")
            preds = load_predictions(long_preds_path)
        elif short_preds_path:
            short_preds_path = resolve_cli_path(short_preds_path)
            print(f"Auto-resolving predictions from config: {short_preds_path}")
            preds = load_predictions(short_preds_path)
        else:
            print("WARNING: No predictions_path in config and no --predictions flag. Using default.")
            args.predictions = resolve_cli_path("reports/vault_predictions.csv")
            preds = load_predictions(args.predictions)
    elif args.predictions is None:
        # No config and no predictions — use legacy default
        args.predictions = resolve_cli_path("reports/vault_predictions.csv")
        print(f"Loading predictions from {args.predictions}...")
        preds = load_predictions(args.predictions)
    else:
        print(f"Loading predictions from {args.predictions}...")
        preds = load_predictions(args.predictions)

    # Run A: Historical data
    print(f"Loading historical OHLCV from {args.data}...")
    ohlcv_a = load_ohlcv(args.data)

    print("Running backtest on historical data...")
    result_a = bt.run(preds, ohlcv_a, label="Historical")
    strategy_cfg_for_report = strategy_cfg if args.config else None
    report_text = format_report(
        result_a,
        config=strategy_cfg_for_report,
        predictions_path=args.predictions or "(auto-resolved from config)",
        data_path=args.data,
    )
    print()
    print(report_text)

    if args.report_file:
        os.makedirs(os.path.dirname(args.report_file) or ".", exist_ok=True)
        with open(args.report_file, "w", encoding="utf-8") as rf:
            rf.write(report_text + "\n")
        print(f"\nReport saved to {args.report_file}")

    # Auto-save report to model registry if experiment_id is available from config
    if strategy_cfg is not None:
        models_cfg = strategy_cfg.get("models", {})
        exp_id = (
            models_cfg.get("long", {}).get("experiment_id")
            or models_cfg.get("short", {}).get("experiment_id")
        )
        if exp_id:
            registry_dir = os.path.join("models", "registry", exp_id)
            if os.path.isdir(registry_dir):
                registry_report = os.path.join(registry_dir, "backtest_report.txt")
                with open(registry_report, "w", encoding="utf-8") as rf:
                    rf.write(report_text + "\n")
                print(f"Report auto-saved to registry: {registry_report}")

    # Run B: Live session data (optional)
    if args.live_data and os.path.exists(args.live_data):
        print(f"\nLoading live session OHLCV from {args.live_data}...")
        ohlcv_b = load_ohlcv(args.live_data)

        print("Running backtest on live session data...")
        result_b = bt.run(preds, ohlcv_b, label="Live Session")
        print()
        print(format_report(result_b))

        # A/B Comparison
        print()
        print(compare_runs(result_a, result_b))
    elif args.live_data:
        print(f"\nNote: Live data file not found at {args.live_data} — skipping Run B.")


# Backward-compatible alias
CLAdvancedExecutionBacktester = BacktestEngine


if __name__ == "__main__":
    main()
