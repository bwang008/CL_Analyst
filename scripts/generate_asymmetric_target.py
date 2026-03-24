#!/usr/bin/env python3
"""Generate Asymmetric Drawdown target for directional model training.

The "Easy Money" Setup — forces the model to find trades that go your way
immediately without drawing down.

LONG Logic:
  For each bar, look ahead 288 bars (24H).
  Label = 1 IF:
    Max(High[t+1:t+288]) - Close[t] > 2.0 * ATR   (upside thrust)
    AND
    Close[t] - Min(Low[t+1:t+288]) < 0.5 * ATR     (minimal drawdown)
  Label = 0 otherwise.

SHORT Logic (inverse):
  Label = 1 IF:
    Close[t] - Min(Low[t+1:t+288]) > 2.0 * ATR     (downside thrust)
    AND
    Max(High[t+1:t+288]) - Close[t] < 0.5 * ATR    (minimal adverse move)
  Label = 0 otherwise.

Usage:
    python scripts/generate_asymmetric_target.py \
        --data data/processed/cl-5m_bk_set_11.parquet \
        --output data/processed/cl-5m_bk_set_11_asym.parquet

    # Dry-run:
    python scripts/generate_asymmetric_target.py --data ... --dry-run --rows 10000
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import numpy as np
import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


def generate_asymmetric_drawdown_target(
    df: pd.DataFrame,
    forward_horizon: int = 288,        # 24H = 288 five-min bars
    atr_period: int = 14,              # ATR lookback (in bars)
    profit_atr_mult: float = 2.0,      # Required move in favorable direction
    drawdown_atr_mult: float = 0.5,    # Max tolerable adverse excursion
) -> pd.DataFrame:
    """Compute Asymmetric Drawdown LONG and SHORT targets.

    Args:
        df: DataFrame with Open, High, Low, Close columns.
        forward_horizon: Bars to look ahead (288 = 24H).
        atr_period: Number of bars for ATR calculation.
        profit_atr_mult: Required favorable move as multiple of ATR.
        drawdown_atr_mult: Maximum adverse excursion as multiple of ATR.

    Returns:
        DataFrame with TARGET_ASYM_LONG and TARGET_ASYM_SHORT appended.
    """
    print(f"  Generating Asymmetric Drawdown target:")
    print(f"    horizon={forward_horizon} bars ({forward_horizon / 12:.0f}H)")
    print(f"    ATR period={atr_period}")
    print(f"    profit_mult={profit_atr_mult}x ATR, drawdown_mult={drawdown_atr_mult}x ATR")

    n = len(df)
    high = df["High"].values
    low = df["Low"].values
    close = df["Close"].values

    # ---- Compute trailing ATR (causal, no future leak) ----
    tr = np.maximum(
        high - low,
        np.maximum(
            np.abs(high - np.roll(close, 1)),
            np.abs(low - np.roll(close, 1))
        )
    )
    tr[0] = high[0] - low[0]  # first bar has no previous close
    atr = pd.Series(tr).rolling(window=atr_period, min_periods=1).mean().values

    # ---- Compute forward max high and forward min low ----
    # Using reversed rolling max/min for vectorized efficiency
    forward_max_high = (
        pd.Series(high[::-1])
        .rolling(window=forward_horizon, min_periods=1)
        .max()
        .values[::-1]
    )
    forward_min_low = (
        pd.Series(low[::-1])
        .rolling(window=forward_horizon, min_periods=1)
        .min()
        .values[::-1]
    )

    # Shift by 1 to exclude current bar (look at NEXT bars only)
    fwd_max_high = np.full(n, np.nan)
    fwd_min_low = np.full(n, np.nan)
    fwd_max_high[:-1] = forward_max_high[1:]
    fwd_min_low[:-1] = forward_min_low[1:]
    # Last horizon bars don't have enough future data
    fwd_max_high[-(forward_horizon):] = np.nan
    fwd_min_low[-(forward_horizon):] = np.nan

    # ---- LONG target ----
    # Upside move: max_high - close > profit_atr_mult * ATR
    # Downside risk: close - min_low < drawdown_atr_mult * ATR
    upside_move = fwd_max_high - close
    downside_risk = close - fwd_min_low

    long_labels = np.where(
        np.isnan(fwd_max_high),
        np.nan,
        np.where(
            (upside_move > profit_atr_mult * atr) &
            (downside_risk < drawdown_atr_mult * atr),
            1.0, 0.0
        )
    )

    # ---- SHORT target (inverse) ----
    # Downside move: close - min_low > profit_atr_mult * ATR
    # Upside risk: max_high - close < drawdown_atr_mult * ATR
    short_labels = np.where(
        np.isnan(fwd_min_low),
        np.nan,
        np.where(
            (downside_risk > profit_atr_mult * atr) &
            (upside_move < drawdown_atr_mult * atr),
            1.0, 0.0
        )
    )

    df["TARGET_ASYM_LONG"] = pd.array(long_labels, dtype="Float64").astype("Int64")
    df["TARGET_ASYM_SHORT"] = pd.array(short_labels, dtype="Float64").astype("Int64")

    # ---- Report distributions ----
    for col in ["TARGET_ASYM_LONG", "TARGET_ASYM_SHORT"]:
        counts = df[col].value_counts(dropna=False)
        non_nan = df[col].dropna()
        pos_rate = (non_nan == 1).sum() / len(non_nan) if len(non_nan) > 0 else 0
        print(f"\n  {col}:")
        print(f"    Distribution: {dict(counts)}")
        print(f"    Positive rate: {pos_rate:.2%}")

    return df


def main():
    parser = argparse.ArgumentParser(
        description="Generate Asymmetric Drawdown target (The 'Easy Money' Setup)."
    )
    parser.add_argument("--data", required=True, help="Input parquet path")
    parser.add_argument("--output", default=None,
                        help="Output parquet path (default: input_asym.parquet)")
    parser.add_argument("--horizon", type=int, default=288,
                        help="Forward horizon in bars (default: 288 = 24H)")
    parser.add_argument("--atr-period", type=int, default=14,
                        help="ATR lookback period in bars (default: 14)")
    parser.add_argument("--profit-mult", type=float, default=2.0,
                        help="Required favorable move as ATR multiple (default: 2.0)")
    parser.add_argument("--drawdown-mult", type=float, default=0.5,
                        help="Max adverse excursion as ATR multiple (default: 0.5)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Only process first N rows (see --rows)")
    parser.add_argument("--rows", type=int, default=10000,
                        help="Rows for dry-run (default: 10000)")
    args = parser.parse_args()

    # Derive output path
    if args.output is None:
        base, ext = os.path.splitext(args.data)
        args.output = f"{base}_asym{ext}"

    print("=" * 60)
    print("GENERATE ASYMMETRIC DRAWDOWN TARGET — 'Easy Money' Setup")
    print("=" * 60)
    print(f"  Input:          {args.data}")
    print(f"  Output:         {args.output}")
    print(f"  Params:         horizon={args.horizon}, ATR={args.atr_period}")
    print(f"                  profit_mult={args.profit_mult}x, dd_mult={args.drawdown_mult}x")
    print("=" * 60)

    t0 = time.perf_counter()
    df = pd.read_parquet(args.data)
    print(f"  Loaded {len(df):,} rows in {time.perf_counter() - t0:.1f}s")

    if args.dry_run:
        df = df.iloc[:args.rows].copy()
        print(f"  [DRY-RUN] Using first {len(df):,} rows only")

    df = generate_asymmetric_drawdown_target(
        df,
        forward_horizon=args.horizon,
        atr_period=args.atr_period,
        profit_atr_mult=args.profit_mult,
        drawdown_atr_mult=args.drawdown_mult,
    )

    # Save
    df.to_parquet(args.output)
    elapsed = time.perf_counter() - t0
    print(f"\n  Saved to: {args.output}")
    print(f"  Total time: {elapsed:.1f}s")
    print("=" * 60)


if __name__ == "__main__":
    main()
