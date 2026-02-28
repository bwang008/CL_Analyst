"""
CLConcurrentPositionBacktester — Unlimited Concurrent Positions.

Simulates what happens when every model signal is acted on independently,
with no position limits. Each trade is individually managed with:
- TP / SL barriers (ATR-based)
- Time-barrier exit (288 bars max hold)
- Trailing stop to breakeven (+1×ATR)
- Gap-aware slippage

Key output: signal clustering report showing how many positions are open
simultaneously and whether concurrent signals correlate with probability
levels — informing whether to scale position size vs. fire multiple orders.

Does NOT mutate backtester.py or backtest_cl_advanced.py.

Usage:
    conda activate trader
    python agent/backtest_cl_concurrent.py --predictions reports/vault_predictions_exp017.csv
    python agent/backtest_cl_concurrent.py --predictions reports/vault_predictions_exp017.csv --sweep

Author: CL Analyst
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import numpy as np
import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------


class ExitReason(Enum):
    """Why a trade was closed."""

    TP = "TP"
    SL = "SL"
    TRAILING_BE = "TRAILING_BE"
    TIME_BARRIER = "TIME_BARRIER"


@dataclass
class OpenPosition:
    """A live position being tracked bar-by-bar."""

    entry_dt: pd.Timestamp
    entry_price: float
    entry_fill: float
    entry_prob: float  # Model probability at entry
    side: int
    atr: float
    tp_price: float
    sl_price: float
    original_sl: float
    lots: int = 1  # Position size (lots)
    trailing_activated: bool = False
    bars_held: int = 0
    highest_high: float = 0.0
    lowest_low: float = float("inf")


@dataclass
class TradeRecord:
    """Completed trade with probability metadata."""

    entry_dt: pd.Timestamp
    exit_dt: pd.Timestamp
    entry_price: float
    exit_price: float
    entry_fill: float
    exit_fill: float
    entry_prob: float
    side: int
    atr_at_entry: float
    exit_reason: ExitReason
    duration_bars: int
    lots: int  # Position size
    net_pnl_dollars: float
    concurrent_positions: int  # How many other positions were open at entry


@dataclass
class BacktestResult:
    """Aggregate results with clustering analysis."""

    trades: list[TradeRecord] = field(default_factory=list)
    label: str = ""
    max_concurrent: int = 0
    concurrent_histogram: dict[int, int] = field(default_factory=dict)

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
        return sum(1 for t in self.trades if t.net_pnl_dollars > 0) / len(self.trades)

    @property
    def profit_factor(self) -> float:
        gross_p = sum(t.net_pnl_dollars for t in self.trades if t.net_pnl_dollars > 0)
        gross_l = abs(sum(t.net_pnl_dollars for t in self.trades if t.net_pnl_dollars < 0))
        if gross_l == 0:
            return float("inf") if gross_p > 0 else 0.0
        return gross_p / gross_l

    @property
    def max_drawdown(self) -> float:
        if not self.trades:
            return 0.0
        cum = np.cumsum([t.net_pnl_dollars for t in self.trades])
        return float(np.min(cum - np.maximum.accumulate(cum)))

    @property
    def exit_distribution(self) -> dict[str, dict[str, float]]:
        total = len(self.trades)
        if total == 0:
            return {}
        counts: dict[str, int] = {}
        for t in self.trades:
            k = t.exit_reason.value
            counts[k] = counts.get(k, 0) + 1
        return {r: {"count": c, "pct": c / total * 100} for r, c in counts.items()}

    def prob_analysis(self) -> dict[str, dict[str, float]]:
        """Win rate breakdown by probability bucket."""
        if not self.trades:
            return {}
        buckets = [
            ("0.45-0.50", 0.45, 0.50),
            ("0.50-0.55", 0.50, 0.55),
            ("0.55-0.60", 0.55, 0.60),
            ("0.60-0.70", 0.60, 0.70),
            ("0.70-0.80", 0.70, 0.80),
            ("0.80+", 0.80, 1.01),
        ]
        result: dict[str, dict[str, float]] = {}
        for label, lo, hi in buckets:
            bucket_trades = [t for t in self.trades if lo <= t.entry_prob < hi]
            if bucket_trades:
                wins = sum(1 for t in bucket_trades if t.net_pnl_dollars > 0)
                pnl = sum(t.net_pnl_dollars for t in bucket_trades)
                result[label] = {
                    "trades": len(bucket_trades),
                    "win_rate": wins / len(bucket_trades) * 100,
                    "total_pnl": pnl,
                    "avg_pnl": pnl / len(bucket_trades),
                }
        return result

    def concurrency_analysis(self) -> dict[str, dict[str, float]]:
        """Win rate breakdown by number of concurrent positions at entry."""
        if not self.trades:
            return {}
        groups: dict[int, list[TradeRecord]] = {}
        for t in self.trades:
            groups.setdefault(t.concurrent_positions, []).append(t)
        result: dict[str, dict[str, float]] = {}
        for n in sorted(groups):
            trades = groups[n]
            wins = sum(1 for t in trades if t.net_pnl_dollars > 0)
            pnl = sum(t.net_pnl_dollars for t in trades)
            label = f"{n} open" if n <= 10 else "10+"
            result[label] = {
                "trades": len(trades),
                "win_rate": wins / len(trades) * 100,
                "total_pnl": pnl,
                "avg_prob": np.mean([t.entry_prob for t in trades]),
            }
        return result


# ---------------------------------------------------------------------------
# Backtester
# ---------------------------------------------------------------------------


# Default probability → lot-size tiers
_DEFAULT_SIZING_TIERS: list[tuple[float, int]] = [
    # (min_probability, lots)
    (0.80, 3),
    (0.70, 2),
    (0.60, 2),
    (0.50, 1),
]


class CLConcurrentPositionBacktester:
    """Bar-by-bar backtester allowing unlimited simultaneous positions.

    Every signal that exceeds the probability threshold opens a new
    independent position. Each position is individually managed with
    TP, SL, trailing stop, and time-barrier exits.

    When position_sizing=True, lot count is scaled by probability:
        0.80+  → 3 lots
        0.70+  → 2 lots
        0.60+  → 2 lots
        0.50+  → 1 lot

    Args:
        tp_atr_mult: ATR multiplier for take-profit.
        sl_atr_mult: ATR multiplier for stop-loss.
        max_horizon: Max bars per position (time barrier).
        trailing_atr_mult: ATR move to trigger breakeven trailing.
        atr_period: ATR calculation period.
        commission_per_side: Commission per side in dollars.
        slippage_per_side: Slippage per side in price units.
        contract_multiplier: CL = 1000.
        prob_threshold: Min probability to open a position.
        position_sizing: If True, scale lots by probability tier.
        sizing_tiers: Custom (min_prob, lots) tiers, highest-first.
    """

    def __init__(
        self,
        *,
        tp_atr_mult: float = 7.0,
        sl_atr_mult: float = 1.0,
        max_horizon: int = 288,
        trailing_atr_mult: float = 1.0,
        atr_period: int = 14,
        commission_per_side: float = 2.50,
        slippage_per_side: float = 0.03,
        contract_multiplier: float = 1000.0,
        prob_threshold: float = 0.45,
        position_sizing: bool = False,
        sizing_tiers: list[tuple[float, int]] | None = None,
    ) -> None:
        self.tp_atr_mult = tp_atr_mult
        self.sl_atr_mult = sl_atr_mult
        self.max_horizon = max_horizon
        self.trailing_atr_mult = trailing_atr_mult
        self.atr_period = atr_period
        self.commission_per_side = commission_per_side
        self.slippage_per_side = slippage_per_side
        self.contract_multiplier = contract_multiplier
        self.prob_threshold = prob_threshold
        self.position_sizing = position_sizing
        self.sizing_tiers = sizing_tiers or _DEFAULT_SIZING_TIERS

    def _prob_to_lots(self, prob: float) -> int:
        """Map probability to lot count using sizing tiers."""
        if not self.position_sizing:
            return 1
        for min_prob, lots in self.sizing_tiers:
            if prob >= min_prob:
                return lots
        return 1

    def _slippage(self, price: float, side: str) -> float:
        if side == "Buy":
            return price + self.slippage_per_side
        return price - self.slippage_per_side

    def _gap_fill(self, bar_open: float, target: float, side: int, is_tp: bool) -> float:
        if side == 1:
            if is_tp:
                return bar_open if bar_open >= target else target
            else:
                return bar_open if bar_open <= target else target
        else:
            if is_tp:
                return bar_open if bar_open <= target else target
            else:
                return bar_open if bar_open >= target else target

    def run(
        self,
        signals_df: pd.DataFrame,
        ohlcv_df: pd.DataFrame,
        *,
        label: str = "",
        signal_side: str = "auto",
        signal_col: Optional[str] = None,
    ) -> BacktestResult:
        """Run the concurrent-positions backtest.

        Args:
            signals_df: DataFrame with 'prob_Buy' or 'Predicted' column.
            ohlcv_df: Full OHLCV DataFrame.
            label: Run label for display.

        Returns:
            BacktestResult with all trades and clustering analysis.
        """
        # Compute ATR
        ohlcv = ohlcv_df.copy()
        tr = np.maximum(
            ohlcv["High"] - ohlcv["Low"],
            np.maximum(
                (ohlcv["High"] - ohlcv["Close"].shift(1)).abs(),
                (ohlcv["Low"] - ohlcv["Close"].shift(1)).abs(),
            ),
        )
        ohlcv["_atr"] = tr.rolling(self.atr_period).mean()

        # Build signal lookup: timestamp → (probability, side)
        signal_data: dict[pd.Timestamp, tuple[float, int]] = {}
        side_map = {"long": 1, "short": -1, "auto": 0}
        if signal_side not in side_map:
            raise ValueError(f"signal_side must be one of {list(side_map)}")

        def _infer_side_from_col(col_name: str) -> Optional[int]:
            if col_name == "prob_Buy":
                return 1
            if col_name == "prob_Sell":
                return -1
            return None

        if signal_col:
            if signal_col not in signals_df.columns:
                raise ValueError(f"Signal column '{signal_col}' not found in predictions.")
            inferred = _infer_side_from_col(signal_col)
            if signal_side == "auto":
                if inferred is None:
                    raise ValueError(
                        "signal_side=auto requires prob_Buy/prob_Sell or explicit --signal-side."
                    )
                chosen_side = inferred
            else:
                chosen_side = side_map[signal_side]
            mask = signals_df[signal_col] >= self.prob_threshold
            for dt_idx in signals_df[mask].index:
                signal_data[pd.Timestamp(dt_idx)] = (
                    float(signals_df.at[dt_idx, signal_col]),
                    chosen_side,
                )
        elif "prob_Buy" in signals_df.columns or "prob_Sell" in signals_df.columns:
            has_buy = "prob_Buy" in signals_df.columns
            has_sell = "prob_Sell" in signals_df.columns
            buy_mask = signals_df["prob_Buy"] >= self.prob_threshold if has_buy else None
            sell_mask = signals_df["prob_Sell"] >= self.prob_threshold if has_sell else None

            if signal_side == "long":
                if not has_buy:
                    raise ValueError("prob_Buy column required for signal_side=long.")
                for dt_idx in signals_df[buy_mask].index:
                    signal_data[pd.Timestamp(dt_idx)] = (
                        float(signals_df.at[dt_idx, "prob_Buy"]),
                        1,
                    )
            elif signal_side == "short":
                if not has_sell:
                    raise ValueError("prob_Sell column required for signal_side=short.")
                for dt_idx in signals_df[sell_mask].index:
                    signal_data[pd.Timestamp(dt_idx)] = (
                        float(signals_df.at[dt_idx, "prob_Sell"]),
                        -1,
                    )
            else:
                # auto: if both present, choose higher prob on conflicts
                if has_buy and has_sell:
                    idx = signals_df.index[buy_mask | sell_mask]
                    for dt_idx in idx:
                        buy_ok = bool(buy_mask.at[dt_idx])
                        sell_ok = bool(sell_mask.at[dt_idx])
                        if buy_ok and sell_ok:
                            buy_prob = float(signals_df.at[dt_idx, "prob_Buy"])
                            sell_prob = float(signals_df.at[dt_idx, "prob_Sell"])
                            if buy_prob >= sell_prob:
                                signal_data[pd.Timestamp(dt_idx)] = (buy_prob, 1)
                            else:
                                signal_data[pd.Timestamp(dt_idx)] = (sell_prob, -1)
                        elif buy_ok:
                            signal_data[pd.Timestamp(dt_idx)] = (
                                float(signals_df.at[dt_idx, "prob_Buy"]),
                                1,
                            )
                        elif sell_ok:
                            signal_data[pd.Timestamp(dt_idx)] = (
                                float(signals_df.at[dt_idx, "prob_Sell"]),
                                -1,
                            )
                elif has_buy:
                    for dt_idx in signals_df[buy_mask].index:
                        signal_data[pd.Timestamp(dt_idx)] = (
                            float(signals_df.at[dt_idx, "prob_Buy"]),
                            1,
                        )
                elif has_sell:
                    for dt_idx in signals_df[sell_mask].index:
                        signal_data[pd.Timestamp(dt_idx)] = (
                            float(signals_df.at[dt_idx, "prob_Sell"]),
                            -1,
                        )
        elif "Predicted" in signals_df.columns:
            preds = signals_df["Predicted"]
            if preds.dtype == object:
                pred_lower = preds.astype(str).str.lower()
                if signal_side in ("auto", "long"):
                    buy_mask = pred_lower == "buy"
                    for dt_idx in signals_df[buy_mask].index:
                        signal_data[pd.Timestamp(dt_idx)] = (0.50, 1)
                if signal_side in ("auto", "short"):
                    sell_mask = pred_lower == "sell"
                    for dt_idx in signals_df[sell_mask].index:
                        signal_data[pd.Timestamp(dt_idx)] = (0.50, -1)
            else:
                if signal_side in ("auto", "long"):
                    buy_mask = preds == 1
                    for dt_idx in signals_df[buy_mask].index:
                        signal_data[pd.Timestamp(dt_idx)] = (0.50, 1)
                if signal_side in ("auto", "short"):
                    sell_mask = preds == -1
                    for dt_idx in signals_df[sell_mask].index:
                        signal_data[pd.Timestamp(dt_idx)] = (0.50, -1)

        # Bar-by-bar simulation
        open_positions: list[OpenPosition] = []
        completed: list[TradeRecord] = []
        max_concurrent = 0
        concurrent_hist: dict[int, int] = {}

        for dt, bar in ohlcv.iterrows():
            ts = pd.Timestamp(dt)
            atr = bar["_atr"]
            bar_open = bar["Open"]
            bar_high = bar["High"]
            bar_low = bar["Low"]
            bar_close = bar["Close"]

            # --- Manage existing positions ---
            still_open: list[OpenPosition] = []
            for pos in open_positions:
                pos.bars_held += 1
                pos.highest_high = max(pos.highest_high, bar_high)
                pos.lowest_low = min(pos.lowest_low, bar_low)

                closed = False
                exit_price = 0.0
                exit_reason = ExitReason.TP

                # 1. Time barrier
                if pos.bars_held > self.max_horizon:
                    exit_price = bar_open
                    exit_reason = ExitReason.TIME_BARRIER
                    closed = True

                # 2. TP
                if not closed:
                    if pos.side == 1 and bar_high >= pos.tp_price:
                        exit_price = self._gap_fill(bar_open, pos.tp_price, 1, True)
                        exit_reason = ExitReason.TP
                        closed = True
                    elif pos.side == -1 and bar_low <= pos.tp_price:
                        exit_price = self._gap_fill(bar_open, pos.tp_price, -1, True)
                        exit_reason = ExitReason.TP
                        closed = True

                # 3. SL (may have been trailed to breakeven)
                if not closed:
                    if pos.side == 1 and bar_low <= pos.sl_price:
                        exit_price = self._gap_fill(bar_open, pos.sl_price, 1, False)
                        exit_reason = (
                            ExitReason.TRAILING_BE if pos.trailing_activated
                            else ExitReason.SL
                        )
                        closed = True
                    elif pos.side == -1 and bar_high >= pos.sl_price:
                        exit_price = self._gap_fill(bar_open, pos.sl_price, -1, False)
                        exit_reason = (
                            ExitReason.TRAILING_BE if pos.trailing_activated
                            else ExitReason.SL
                        )
                        closed = True

                # 4. Trailing stop upgrade
                if not closed and not pos.trailing_activated:
                    if pos.side == 1:
                        if pos.highest_high >= (
                            pos.entry_price + self.trailing_atr_mult * pos.atr
                        ):
                            pos.sl_price = pos.entry_price
                            pos.trailing_activated = True
                    else:
                        if pos.lowest_low <= (
                            pos.entry_price - self.trailing_atr_mult * pos.atr
                        ):
                            pos.sl_price = pos.entry_price
                            pos.trailing_activated = True

                if closed:
                    exit_side = "Sell" if pos.side == 1 else "Buy"
                    exit_fill = self._slippage(exit_price, exit_side)
                    pnl_price = pos.side * (exit_fill - pos.entry_fill)
                    # Scale P&L and commission by lot count
                    net_pnl = (
                        pnl_price * self.contract_multiplier * pos.lots
                        - 2 * self.commission_per_side * pos.lots
                    )

                    completed.append(TradeRecord(
                        entry_dt=pos.entry_dt,
                        exit_dt=ts,
                        entry_price=pos.entry_price,
                        exit_price=exit_price,
                        entry_fill=pos.entry_fill,
                        exit_fill=exit_fill,
                        entry_prob=pos.entry_prob,
                        side=pos.side,
                        atr_at_entry=pos.atr,
                        exit_reason=exit_reason,
                        duration_bars=pos.bars_held,
                        lots=pos.lots,
                        net_pnl_dollars=net_pnl,
                        concurrent_positions=0,  # Filled below
                    ))
                else:
                    still_open.append(pos)

            open_positions = still_open

            # --- Open new position on signal ---
            if ts in signal_data and not np.isnan(atr) and atr > 0:
                prob, side = signal_data[ts]
                lots = self._prob_to_lots(prob)
                if side == 1:
                    entry_fill = self._slippage(bar_close, "Buy")
                    tp = bar_close + self.tp_atr_mult * atr
                    sl = bar_close - self.sl_atr_mult * atr
                else:
                    entry_fill = self._slippage(bar_close, "Sell")
                    tp = bar_close - self.tp_atr_mult * atr
                    sl = bar_close + self.sl_atr_mult * atr

                new_pos = OpenPosition(
                    entry_dt=ts,
                    entry_price=bar_close,
                    entry_fill=entry_fill,
                    entry_prob=prob,
                    side=side,
                    atr=atr,
                    tp_price=tp,
                    sl_price=sl,
                    original_sl=sl,
                    lots=lots,
                    highest_high=bar_high,
                    lowest_low=bar_low,
                )
                open_positions.append(new_pos)

            # Track concurrency
            n_open = len(open_positions)
            if n_open > max_concurrent:
                max_concurrent = n_open
            concurrent_hist[n_open] = concurrent_hist.get(n_open, 0) + 1

        # Set concurrent_positions on each trade record (at entry time)
        # We need a second pass — count how many positions were open at each entry
        # Build a simple timeline
        entry_times = {}
        for i, t in enumerate(completed):
            entry_times.setdefault(t.entry_dt, []).append(i)

        # For each trade, count overlapping positions
        for i, trade in enumerate(completed):
            overlap = sum(
                1 for other in completed
                if other.entry_dt <= trade.entry_dt < other.exit_dt
                and other is not trade
            )
            trade.concurrent_positions = overlap

        # Sort by entry time
        completed.sort(key=lambda t: t.entry_dt)

        return BacktestResult(
            trades=completed,
            label=label,
            max_concurrent=max_concurrent,
            concurrent_histogram=concurrent_hist,
        )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def format_report(r: BacktestResult) -> str:
    """Full report with probability and concurrency analysis."""
    w = 70
    lines: list[str] = []
    lines.append("=" * w)
    title = f"CONCURRENT BACKTEST: {r.label}" if r.label else "CONCURRENT BACKTEST"
    lines.append(title.center(w))
    lines.append("=" * w)

    if r.trade_count == 0:
        lines.append("  No trades simulated.")
        lines.append("=" * w)
        return "\n".join(lines)

    total_lots = sum(t.lots for t in r.trades)
    lines.append(f"  Total Trades:       {r.trade_count:,}")
    lines.append(f"  Total Lots:         {total_lots:,}")
    lines.append(f"  Win Rate:           {r.win_rate:.1%}")
    lines.append(f"  Profit Factor:      {r.profit_factor:.2f}")
    lines.append(f"  Total Net PnL:      ${r.total_pnl:>14,.2f}")
    lines.append(f"  Max Drawdown:       ${r.max_drawdown:>14,.2f}")
    lines.append(f"  Max Concurrent:     {r.max_concurrent}")
    lines.append("-" * w)

    # Exit distribution
    lines.append("  Exit Distribution:")
    dist = r.exit_distribution
    for reason in ["TP", "SL", "TRAILING_BE", "TIME_BARRIER"]:
        d = dist.get(reason, {"count": 0, "pct": 0.0})
        lines.append(f"    {reason:<16s} {int(d['count']):>6,}  ({d['pct']:.1f}%)")

    lines.append("-" * w)

    # Probability analysis
    lines.append("  Win Rate by Probability Bucket:")
    lines.append(f"    {'Bucket':<12s} {'Trades':>8s} {'Win Rate':>10s} {'Avg PnL':>12s} {'Total PnL':>14s}")
    prob = r.prob_analysis()
    for bucket, data in prob.items():
        lines.append(
            f"    {bucket:<12s} {int(data['trades']):>8,} "
            f"{data['win_rate']:>9.1f}% "
            f"${data['avg_pnl']:>11,.2f} "
            f"${data['total_pnl']:>13,.2f}"
        )

    lines.append("-" * w)

    # Concurrency analysis
    lines.append("  Win Rate by Concurrent Open Positions:")
    lines.append(f"    {'Open Pos':>10s} {'Trades':>8s} {'Win Rate':>10s} {'Avg Prob':>10s} {'Total PnL':>14s}")
    conc = r.concurrency_analysis()
    for level, data in conc.items():
        lines.append(
            f"    {level:>10s} {int(data['trades']):>8,} "
            f"{data['win_rate']:>9.1f}% "
            f"{data['avg_prob']:>9.4f} "
            f"${data['total_pnl']:>13,.2f}"
        )

    lines.append("=" * w)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Data Loading (reuse from advanced backtester)
# ---------------------------------------------------------------------------


def load_predictions(path: str) -> pd.DataFrame:
    """Load predictions CSV."""
    return pd.read_csv(path, index_col=0, parse_dates=True, on_bad_lines="warn")


def load_ohlcv(path: str) -> pd.DataFrame:
    """Load OHLCV data from parquet or CSV with raw CSV fallback."""
    if path.endswith(".parquet"):
        df = pd.read_parquet(path)
    else:
        df = pd.read_csv(path, index_col=0, parse_dates=True, sep=None, engine="python")

    rename_map = {"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"}
    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})

    if "RAW_Close" in df.columns:
        for col in ["Open", "High", "Low"]:
            raw_col = f"RAW_{col}"
            if raw_col in df.columns:
                df[col] = df[raw_col]
        df["Close"] = df["RAW_Close"]

    required = {"Open", "High", "Low", "Close", "Volume"}
    missing = required - set(df.columns)
    if missing:
        raw_candidates = [
            os.path.join(PROJECT_ROOT, "data", "raw", "cl-5m_bk.csv"),
            os.path.join(PROJECT_ROOT, "data", "raw", "CL.csv"),
        ]
        for raw_path in raw_candidates:
            if os.path.exists(raw_path):
                print(f"  Falling back to raw CSV: {raw_path}")
                df = pd.read_csv(
                    raw_path, sep=";", header=None,
                    names=["Date", "Time", "Open", "High", "Low", "Close", "Volume"],
                )
                df["DateTime"] = pd.to_datetime(df["Date"] + " " + df["Time"], dayfirst=True)
                df = df.set_index("DateTime").drop(columns=["Date", "Time"])
                break
        else:
            raise FileNotFoundError(f"OHLCV missing {missing}, no raw CSV found.")

    return df


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_float_list(raw: str, *, name: str) -> list[float]:
    if raw is None:
        return []
    values = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            values.append(float(chunk))
        except ValueError as exc:
            raise ValueError(f"Invalid {name} value: '{chunk}'") from exc
    return values


def _pnl_to_drawdown_ratio(total_pnl: float, max_drawdown: float) -> float:
    if max_drawdown == 0:
        return float("inf") if total_pnl > 0 else 0.0
    return total_pnl / abs(max_drawdown)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="CL Concurrent Positions Backtester"
    )
    parser.add_argument(
        "--predictions",
        default=os.getenv("CL_PREDICTIONS_PATH", "reports/vault_predictions_exp017.csv"),
    )
    parser.add_argument(
        "--data",
        default=os.getenv("CL_OHLCV_PATH", "data/processed/CL_set_06.parquet"),
    )
    parser.add_argument("--threshold", type=float, default=0.50)
    parser.add_argument("--tp-mult", type=float, default=7.0)
    parser.add_argument("--sl-mult", type=float, default=1.0)
    parser.add_argument("--commission-per-side", type=float, default=2.50)
    parser.add_argument("--slippage-per-side", type=float, default=0.03)
    parser.add_argument("--contract-multiplier", type=float, default=1000.0)
    parser.add_argument(
        "--signal-side",
        choices=["long", "short", "auto"],
        default="auto",
        help="Signal side selection for prob columns.",
    )
    parser.add_argument(
        "--signal-col",
        default=None,
        help="Optional explicit signal column (e.g., prob_Sell).",
    )
    parser.add_argument(
        "--position-sizing", action="store_true",
        help="Scale lot size by probability (0.50->1, 0.60->2, 0.70->2, 0.80->3)",
    )
    parser.add_argument(
        "--sweep", action="store_true",
        help="Run threshold sweep from 0.45 to 0.80",
    )
    parser.add_argument(
        "--grid", action="store_true",
        help="Run grid sweep over threshold x TP x SL",
    )
    parser.add_argument(
        "--grid-thresholds",
        default="0.45,0.50,0.55,0.60,0.65,0.70,0.75,0.80",
        help="Comma-separated thresholds for grid mode.",
    )
    parser.add_argument(
        "--grid-tp-mults",
        default="5,7,9",
        help="Comma-separated TP ATR multipliers for grid mode.",
    )
    parser.add_argument(
        "--grid-sl-mults",
        default="0.75,1.0,1.25",
        help="Comma-separated SL ATR multipliers for grid mode.",
    )
    parser.add_argument(
        "--grid-output",
        default=os.path.join("reports", "concurrent_grid_results.csv"),
        help="CSV output path for grid results.",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=10,
        help="Top K configurations to print in grid mode.",
    )
    args = parser.parse_args()

    preds = load_predictions(args.predictions)
    ohlcv = load_ohlcv(args.data)

    if args.grid:
        thresholds = _parse_float_list(args.grid_thresholds, name="grid-thresholds")
        tp_mults = _parse_float_list(args.grid_tp_mults, name="grid-tp-mults")
        sl_mults = _parse_float_list(args.grid_sl_mults, name="grid-sl-mults")

        w = 120
        print("=" * w)
        print("GRID SWEEP - CONCURRENT POSITIONS".center(w))
        print("=" * w)
        print(
            f"  {'Thresh':>6s} {'TP':>5s} {'SL':>5s} "
            f"{'Trades':>7s} {'Win%':>6s} {'PF':>7s} "
            f"{'Net PnL':>14s} {'MaxDD':>14s} {'PnL/DD':>8s} {'MaxConc':>8s}"
        )
        print("-" * w)

        rows: list[dict] = []
        for thresh in thresholds:
            for tp_mult in tp_mults:
                for sl_mult in sl_mults:
                    bt = CLConcurrentPositionBacktester(
                        tp_atr_mult=tp_mult,
                        sl_atr_mult=sl_mult,
                        prob_threshold=thresh,
                        commission_per_side=args.commission_per_side,
                        slippage_per_side=args.slippage_per_side,
                        contract_multiplier=args.contract_multiplier,
                        position_sizing=args.position_sizing,
                    )
                    result = bt.run(
                        preds,
                        ohlcv,
                        label=f"t={thresh:.2f}|tp={tp_mult:.2f}|sl={sl_mult:.2f}",
                        signal_side=args.signal_side,
                        signal_col=args.signal_col,
                    )
                    ratio = _pnl_to_drawdown_ratio(result.total_pnl, result.max_drawdown)
                    rows.append(
                        {
                            "threshold": round(float(thresh), 4),
                            "tp_mult": round(float(tp_mult), 4),
                            "sl_mult": round(float(sl_mult), 4),
                            "trade_count": result.trade_count,
                            "win_rate": result.win_rate,
                            "profit_factor": result.profit_factor,
                            "total_pnl": result.total_pnl,
                            "max_drawdown": result.max_drawdown,
                            "pnl_to_drawdown_ratio": ratio,
                            "max_concurrent": result.max_concurrent,
                        }
                    )

        df = pd.DataFrame(rows)
        df = df.sort_values(by="pnl_to_drawdown_ratio", ascending=False)
        os.makedirs(os.path.dirname(args.grid_output) or ".", exist_ok=True)
        df.to_csv(args.grid_output, index=False)

        top_k = min(args.top_k, len(df))
        for _, row in df.head(top_k).iterrows():
            print(
                f"  {row['threshold']:>6.2f} {row['tp_mult']:>5.2f} {row['sl_mult']:>5.2f} "
                f"{int(row['trade_count']):>7,} {row['win_rate']:>5.1%} "
                f"{row['profit_factor']:>7.2f} ${row['total_pnl']:>13,.2f} "
                f"${row['max_drawdown']:>13,.2f} {row['pnl_to_drawdown_ratio']:>8.2f} "
                f"{int(row['max_concurrent']):>8}"
            )
        print("=" * w)
        print(f"Saved grid results to {args.grid_output}")
    elif args.sweep:
        # Threshold sweep
        thresholds = [0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80]
        w = 100
        print("=" * w)
        print("THRESHOLD SWEEP - CONCURRENT POSITIONS".center(w))
        print("=" * w)
        print(
            f"  {'Thresh':>6s} {'Trades':>7s} {'Win%':>6s} "
            f"{'PF':>7s} {'Net PnL':>14s} {'MaxDD':>14s} "
            f"{'MaxConc':>8s} {'TP%':>6s} {'SL%':>6s} {'Trail%':>7s} {'Time%':>6s}"
        )
        print("-" * w)

        for thresh in thresholds:
            bt = CLConcurrentPositionBacktester(
                tp_atr_mult=args.tp_mult,
                sl_atr_mult=args.sl_mult,
                prob_threshold=thresh,
                commission_per_side=args.commission_per_side,
                slippage_per_side=args.slippage_per_side,
                contract_multiplier=args.contract_multiplier,
                position_sizing=args.position_sizing,
            )
            result = bt.run(
                preds,
                ohlcv,
                label=f"t={thresh:.2f}",
                signal_side=args.signal_side,
                signal_col=args.signal_col,
            )
            dist = result.exit_distribution
            tp_pct = dist.get("TP", {}).get("pct", 0.0)
            sl_pct = dist.get("SL", {}).get("pct", 0.0)
            tr_pct = dist.get("TRAILING_BE", {}).get("pct", 0.0)
            tb_pct = dist.get("TIME_BARRIER", {}).get("pct", 0.0)

            print(
                f"  {thresh:>6.2f} {result.trade_count:>7,} {result.win_rate:>5.1%} "
                f"{result.profit_factor:>7.2f} ${result.total_pnl:>13,.2f} "
                f"${result.max_drawdown:>13,.2f} "
                f"{result.max_concurrent:>8} "
                f"{tp_pct:>5.1f}% {sl_pct:>5.1f}% {tr_pct:>6.1f}% {tb_pct:>5.1f}%"
            )

        print("=" * w)
    else:
        # Single run with full report
        print(f"Loading predictions: {args.predictions}")
        print(f"Threshold: {args.threshold}")
        print(f"Loading OHLCV: {args.data}")

        bt = CLConcurrentPositionBacktester(
            tp_atr_mult=args.tp_mult,
            sl_atr_mult=args.sl_mult,
            prob_threshold=args.threshold,
            commission_per_side=args.commission_per_side,
            slippage_per_side=args.slippage_per_side,
            contract_multiplier=args.contract_multiplier,
            position_sizing=args.position_sizing,
        )

        print("Running backtest...")
        result = bt.run(
            preds,
            ohlcv,
            label=f"Concurrent (t={args.threshold})",
            signal_side=args.signal_side,
            signal_col=args.signal_col,
        )
        print()
        print(format_report(result))


if __name__ == "__main__":
    main()
