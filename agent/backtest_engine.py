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


# ---------------------------------------------------------------------------
# Enums & Data Structures
# ---------------------------------------------------------------------------


class TradeState(Enum):
    """Finite State Machine states for trade management.

    Transitions:
        FLAT ──[buy signal ≥ threshold]──→ IN_POSITION
        IN_POSITION ──[TP hit]──→ FLAT
        IN_POSITION ──[SL hit (no trailing)]──→ COOLDOWN
        IN_POSITION ──[trailing stop hit at breakeven]──→ FLAT
        IN_POSITION ──[288 bars elapsed]──→ FLAT
        COOLDOWN ──[cooldown_bars elapsed]──→ FLAT
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


@dataclass
class BacktestResult:
    """Aggregate results from a backtest run."""

    trades: list[TradeRecord] = field(default_factory=list)
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
        cooldown_bars: Bars to wait after a stop-loss exit (single mode).
        trailing_atr_mult: ATR move in favor to trigger trailing stop
                           to breakeven ($entry price).
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
        cooldown_bars: int = 10,
        trailing_atr_mult: float = 1.0,
        atr_period: int = 14,
        commission_per_side: float = 2.50,
        slippage_per_side: float = 0.03,
        contract_multiplier: float = 1000.0,
        prob_threshold: float = 0.45,
        allow_concurrent: bool = False,
        max_concurrent: int = 1,
    ) -> None:
        self.tp_atr_mult = tp_atr_mult
        self.sl_atr_mult = sl_atr_mult
        self.max_horizon = max_horizon
        self.cooldown_bars = cooldown_bars
        self.trailing_atr_mult = trailing_atr_mult
        self.atr_period = atr_period
        self.commission_per_side = commission_per_side
        self.slippage_per_side = slippage_per_side
        self.contract_multiplier = contract_multiplier
        self.prob_threshold = prob_threshold
        self.allow_concurrent = allow_concurrent
        self.max_concurrent = max(1, max_concurrent)

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
        self._trades: list[TradeRecord] = []

        # Concurrent mode state
        self._open_positions: list[_OpenPosition] = []

    @classmethod
    def from_config(cls, cfg: dict, **overrides) -> "BacktestEngine":
        """Create a BacktestEngine from a strategy config dict.

        Reads all supported fields from the JSON config, with CLI overrides
        taking precedence.
        """
        kwargs = {
            "tp_atr_mult": cfg.get("tp_atr_mult", 2.0),
            "sl_atr_mult": cfg.get("sl_atr_mult", 1.0),
            "prob_threshold": cfg.get("entry_threshold", 0.45),
            "allow_concurrent": cfg.get("allow_concurrent", False),
            "max_concurrent": cfg.get("max_concurrent", 1),
            "cooldown_bars": cfg.get("cooldown_bars", 10),
            "trailing_atr_mult": cfg.get("trailing_atr_mult", 1.0),
            "max_horizon": cfg.get("max_hold_bars", 288),
        }
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
        self._open_positions = []

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
        gross_pnl_dollars = gross_pnl_price * self.contract_multiplier
        commission = 2 * self.commission_per_side
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
        )
        self._trades.append(record)

        # FSM transition: SL → COOLDOWN, everything else → FLAT
        if exit_reason == ExitReason.SL:
            self._state = TradeState.COOLDOWN
            self._cooldown_remaining = self.cooldown_bars
        else:
            self._state = TradeState.FLAT

    def _on_flat(
        self,
        dt: pd.Timestamp,
        bar: pd.Series,
        signal_side: Optional[int],
        atr: float,
    ) -> None:
        """FLAT state: accept valid signals and enter a position.

        Args:
            dt: Bar timestamp.
            bar: OHLCV bar data.
            signal_side: +1 for buy, -1 for sell, None for no signal.
            atr: Current ATR value.
        """
        if signal_side is None or np.isnan(atr) or atr <= 0:
            return

        self._state = TradeState.IN_POSITION
        self._entry_dt = dt
        self._entry_price = bar["Close"]
        self._atr_at_entry = atr
        self._side = signal_side
        self._bars_held = 0
        self._trailing_activated = False

        entry_order_side = "Buy" if signal_side == 1 else "Sell"
        self._entry_fill = self._apply_slippage(self._entry_price, entry_order_side)

        if signal_side == 1:
            self._tp_price = self._entry_price + self.tp_atr_mult * atr
            self._sl_price = self._entry_price - self.sl_atr_mult * atr
            self._highest_high = bar["High"]
            self._lowest_low = bar["Low"]
        else:
            self._tp_price = self._entry_price - self.tp_atr_mult * atr
            self._sl_price = self._entry_price + self.sl_atr_mult * atr
            self._highest_high = bar["High"]
            self._lowest_low = bar["Low"]

        self._original_sl_price = self._sl_price

    def _on_in_position(self, dt: pd.Timestamp, bar: pd.Series) -> None:
        """IN_POSITION state: manage an active trade.

        Checks (in order):
        1. Time-barrier exit (288 bars)
        2. TP hit (with gap awareness)
        3. SL hit (with gap awareness)
        4. Trailing stop upgrade to breakeven

        Args:
            dt: Bar timestamp.
            bar: OHLCV bar data.
        """
        self._bars_held += 1
        bar_open = bar["Open"]
        bar_high = bar["High"]
        bar_low = bar["Low"]

        # Track extremes since entry
        self._highest_high = max(self._highest_high, bar_high)
        self._lowest_low = min(self._lowest_low, bar_low)

        # 1. Time-barrier exit — force close after max_horizon bars
        if self._bars_held > self.max_horizon:
            exit_price = bar_open  # Exit at open of the 289th bar
            self._close_trade(dt, exit_price, ExitReason.TIME_BARRIER)
            return

        # 2. Check TP hit
        tp_hit = False
        if self._side == 1:
            tp_hit = bar_high >= self._tp_price
        else:
            tp_hit = bar_low <= self._tp_price

        if tp_hit:
            exit_price = self._gap_fill_price(
                bar_open, self._tp_price, self._side, is_tp=True
            )
            self._close_trade(dt, exit_price, ExitReason.TP)
            return

        # 3. Check SL hit (current SL level — may have been trailed)
        sl_hit = False
        if self._side == 1:
            sl_hit = bar_low <= self._sl_price
        else:
            sl_hit = bar_high >= self._sl_price

        if sl_hit:
            exit_price = self._gap_fill_price(
                bar_open, self._sl_price, self._side, is_tp=False
            )
            # Determine exit reason based on whether trailing was activated
            if self._trailing_activated:
                self._close_trade(dt, exit_price, ExitReason.TRAILING_BE)
            else:
                self._close_trade(dt, exit_price, ExitReason.SL)
            return

        # 4. Trailing stop upgrade: move SL to breakeven after +1×ATR
        if not self._trailing_activated:
            if self._side == 1:
                # Long: if highest high reached entry + trailing_atr_mult * ATR
                if self._highest_high >= (
                    self._entry_price + self.trailing_atr_mult * self._atr_at_entry
                ):
                    self._sl_price = self._entry_price  # Move to breakeven
                    self._trailing_activated = True
            else:
                # Short: if lowest low reached entry - trailing_atr_mult * ATR
                if self._lowest_low <= (
                    self._entry_price - self.trailing_atr_mult * self._atr_at_entry
                ):
                    self._sl_price = self._entry_price  # Move to breakeven
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
    ) -> None:
        """Open a new position and add it to the open-positions list."""
        entry_price = bar["Close"]
        entry_order_side = "Buy" if signal_side == 1 else "Sell"
        entry_fill = self._apply_slippage(entry_price, entry_order_side)

        if signal_side == 1:
            tp_price = entry_price + self.tp_atr_mult * atr
            sl_price = entry_price - self.sl_atr_mult * atr
        else:
            tp_price = entry_price - self.tp_atr_mult * atr
            sl_price = entry_price + self.sl_atr_mult * atr

        pos = _OpenPosition(
            entry_dt=dt,
            entry_price=entry_price,
            entry_fill=entry_fill,
            atr_at_entry=atr,
            side=signal_side,
            tp_price=tp_price,
            sl_price=sl_price,
            original_sl_price=sl_price,
            highest_high=bar["High"],
            lowest_low=bar["Low"],
        )
        self._open_positions.append(pos)

    def _check_position(
        self,
        pos: _OpenPosition,
        dt: pd.Timestamp,
        bar: pd.Series,
    ) -> Optional[TradeRecord]:
        """Check an open position for exit conditions.

        Returns a TradeRecord if the position closed, else None.
        """
        pos.bars_held += 1
        bar_open = bar["Open"]
        bar_high = bar["High"]
        bar_low = bar["Low"]

        pos.highest_high = max(pos.highest_high, bar_high)
        pos.lowest_low = min(pos.lowest_low, bar_low)

        exit_price: Optional[float] = None
        exit_reason: Optional[ExitReason] = None

        # 1. Time barrier
        if pos.bars_held > self.max_horizon:
            exit_price = bar_open
            exit_reason = ExitReason.TIME_BARRIER

        # 2. TP
        if exit_reason is None:
            if pos.side == 1 and bar_high >= pos.tp_price:
                exit_price = self._gap_fill_price(
                    bar_open, pos.tp_price, pos.side, is_tp=True
                )
                exit_reason = ExitReason.TP
            elif pos.side == -1 and bar_low <= pos.tp_price:
                exit_price = self._gap_fill_price(
                    bar_open, pos.tp_price, pos.side, is_tp=True
                )
                exit_reason = ExitReason.TP

        # 3. SL
        if exit_reason is None:
            if pos.side == 1 and bar_low <= pos.sl_price:
                exit_price = self._gap_fill_price(
                    bar_open, pos.sl_price, pos.side, is_tp=False
                )
                exit_reason = (
                    ExitReason.TRAILING_BE if pos.trailing_activated
                    else ExitReason.SL
                )
            elif pos.side == -1 and bar_high >= pos.sl_price:
                exit_price = self._gap_fill_price(
                    bar_open, pos.sl_price, pos.side, is_tp=False
                )
                exit_reason = (
                    ExitReason.TRAILING_BE if pos.trailing_activated
                    else ExitReason.SL
                )

        # 4. Trailing stop upgrade
        if exit_reason is None and not pos.trailing_activated:
            if pos.side == 1:
                if pos.highest_high >= (
                    pos.entry_price + self.trailing_atr_mult * pos.atr_at_entry
                ):
                    pos.sl_price = pos.entry_price
                    pos.trailing_activated = True
            else:
                if pos.lowest_low <= (
                    pos.entry_price - self.trailing_atr_mult * pos.atr_at_entry
                ):
                    pos.sl_price = pos.entry_price
                    pos.trailing_activated = True

        if exit_reason is not None and exit_price is not None:
            exit_order_side = "Sell" if pos.side == 1 else "Buy"
            exit_fill = self._apply_slippage(exit_price, exit_order_side)
            gross_pnl_price = pos.side * (exit_fill - pos.entry_fill)
            gross_pnl_dollars = gross_pnl_price * self.contract_multiplier
            commission = 2 * self.commission_per_side
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
        ohlcv["_atr"] = tr.rolling(self.atr_period).mean()

        # Build signal lookup — which bars have a trade signal
        signal_sides: dict[pd.Timestamp, int] = {}

        if "side" in signals_df.columns:
            for dt_idx in signals_df.index:
                ts = pd.Timestamp(dt_idx)
                signal_sides[ts] = int(signals_df.at[dt_idx, "side"])
        elif "prob_Buy" in signals_df.columns:
            mask = signals_df["prob_Buy"] >= self.prob_threshold
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
            label=label,
            start_dt=ohlcv.index.min() if not ohlcv.empty else None,
            end_dt=ohlcv.index.max() if not ohlcv.empty else None,
        )

    def _run_single(
        self,
        ohlcv: pd.DataFrame,
        signal_sides: dict[pd.Timestamp, int],
    ) -> None:
        """Single-position FSM loop (original behaviour)."""
        for dt, bar in ohlcv.iterrows():
            ts = pd.Timestamp(dt)
            atr = bar["_atr"]

            if self._state == TradeState.FLAT:
                sig = signal_sides.get(ts)
                self._on_flat(ts, bar, sig, atr)

            elif self._state == TradeState.IN_POSITION:
                self._on_in_position(ts, bar)

            elif self._state == TradeState.COOLDOWN:
                self._on_cooldown()

    def _run_concurrent(
        self,
        ohlcv: pd.DataFrame,
        signal_sides: dict[pd.Timestamp, int],
    ) -> None:
        """Concurrent multi-position loop.

        On each bar:
        1. Check all open positions for exits (TP/SL/trailing/time)
        2. If a signal is present and we haven't hit max_concurrent, open new
        """
        for dt, bar in ohlcv.iterrows():
            ts = pd.Timestamp(dt)
            atr = bar["_atr"]

            # 1. Check existing positions for exits
            surviving: list[_OpenPosition] = []
            for pos in self._open_positions:
                record = self._check_position(pos, ts, bar)
                if record is not None:
                    self._trades.append(record)
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
                self._open_new_position(ts, bar, sig, atr)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def format_report(result: BacktestResult) -> str:
    """Format a BacktestResult as a structured console report."""
    w = 60
    lines: list[str] = []
    lines.append("=" * w)
    title = f"BACKTEST RESULTS: {result.label}" if result.label else "BACKTEST RESULTS"
    lines.append(title.center(w))
    lines.append("=" * w)

    if result.trade_count == 0:
        lines.append("  No trades simulated.")
        lines.append("=" * w)
        return "\n".join(lines)

    lines.append(f"  Total Trades:     {result.trade_count}")
    if result.start_dt is not None and result.end_dt is not None:
        lines.append(f"  Date Range:       {result.start_dt} → {result.end_dt}")
    lines.append(f"  Win Rate:         {result.win_rate:.1%}")
    lines.append(f"  Profit Factor:    {result.profit_factor:.2f}")
    lines.append(f"  Total Net PnL:    ${result.total_pnl:>14,.2f}")
    lines.append(f"  Max Drawdown:     ${result.max_drawdown:>14,.2f}")
    lines.append("-" * w)
    lines.append("  Exit Distribution:")

    dist = result.exit_distribution
    for reason in ["TP", "SL", "TRAILING_BE", "TIME_BARRIER"]:
        if reason in dist:
            d = dist[reason]
            lines.append(
                f"    {reason:<16s} {int(d['count']):>5d}  ({d['pct']:.1f}%)"
            )
        else:
            lines.append(f"    {reason:<16s}     0  (0.0%)")

    lines.append("=" * w)
    return "\n".join(lines)


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
        raw_candidates = [
            os.path.join(PROJECT_ROOT, "data", "raw", "cl-5m_bk.csv"),
            os.path.join(PROJECT_ROOT, "data", "raw", "CL.csv"),
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
        default="reports/vault_predictions.csv",
        help="Path to vault predictions CSV",
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
    args = parser.parse_args()

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
        print(
            f"Loaded strategy config '{strategy_cfg.get('nickname', '?')}': "
            f"TP={bt.tp_atr_mult}x  SL={bt.sl_atr_mult}x  "
            f"threshold={bt.prob_threshold}  [{concurrent_str}]"
        )
    else:
        bt = BacktestEngine(
            tp_atr_mult=args.tp_mult,
            sl_atr_mult=args.sl_mult,
            prob_threshold=args.threshold,
            commission_per_side=args.commission_per_side,
            slippage_per_side=args.slippage_per_side,
            contract_multiplier=args.contract_multiplier,
        )

    # Run A: Historical data
    print(f"Loading predictions from {args.predictions}...")
    preds = load_predictions(args.predictions)

    print(f"Loading historical OHLCV from {args.data}...")
    ohlcv_a = load_ohlcv(args.data)

    print("Running backtest on historical data...")
    result_a = bt.run(preds, ohlcv_a, label="Historical")
    print()
    print(format_report(result_a))

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
