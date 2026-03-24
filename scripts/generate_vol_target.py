#!/usr/bin/env python3
"""Generate Volatility Expansion target for Experiment 3.

Loads an existing processed parquet (e.g. set_11), computes a forward-looking
True Range over the next 24H, labels bars in the top 20% of historical rolling
TR as TARGET_VOL_EXPANSION = 1, and saves as a new parquet file.

Usage:
    python scripts/generate_vol_target.py \
        --data /path/to/cl-5m_bk_set_11.parquet \
        --output /path/to/cl-5m_bk_set_11_vol.parquet

    # Dry-run on first N rows:
    python scripts/generate_vol_target.py --data ... --dry-run --rows 10000
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


def generate_vol_expansion_target(
    df: pd.DataFrame,
    forward_horizon: int = 288,        # 24 hours = 288 five-min bars
    rolling_window: int = 10_080,      # 35 days = 10,080 five-min bars
    percentile_threshold: float = 0.80,  # top 20% = 80th percentile
) -> pd.DataFrame:
    """Compute Volatility Expansion target and append to DataFrame.

    For each bar, looks at the True Range (max High - min Low) of the next
    `forward_horizon` bars. If this forward TR is above the `percentile_threshold`
    of the trailing `rolling_window` historical forward TRs, label = 1.

    Args:
        df: DataFrame with High, Low columns and a DatetimeIndex.
        forward_horizon: Bars to look ahead for TR computation (288 = 24H).
        rolling_window: Trailing window for percentile computation.
        percentile_threshold: Quantile threshold (0.80 = top 20%).

    Returns:
        DataFrame with TARGET_VOL_EXPANSION column appended.
    """
    print(f"  Generating Vol Expansion target: "
          f"horizon={forward_horizon} bars ({forward_horizon / 12:.0f}H), "
          f"rolling={rolling_window} bars ({rolling_window / 288:.0f}D), "
          f"top {(1 - percentile_threshold) * 100:.0f}%")

    n = len(df)
    high = df["High"].values
    low = df["Low"].values

    # Compute forward True Range: max(High[t+1:t+horizon]) - min(Low[t+1:t+horizon])
    # Using vectorised rolling operations on reversed series for efficiency
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
    forward_tr = np.full(n, np.nan)
    forward_tr[:-1] = forward_max_high[1:] - forward_min_low[1:]
    # Last `forward_horizon` bars don't have enough future data
    forward_tr[-(forward_horizon):] = np.nan

    # Compute rolling percentile threshold (trailing, causal)
    forward_tr_series = pd.Series(forward_tr, index=df.index)
    rolling_threshold = forward_tr_series.rolling(
        window=rolling_window, min_periods=rolling_window // 2
    ).quantile(percentile_threshold)

    # Label: 1 if forward TR exceeds rolling threshold, else 0
    labels = np.where(
        np.isnan(forward_tr) | np.isnan(rolling_threshold.values),
        np.nan,
        np.where(forward_tr > rolling_threshold.values, 1.0, 0.0)
    )

    df["TARGET_VOL_EXPANSION"] = pd.array(labels, dtype="Int64")

    # Report distribution
    counts = df["TARGET_VOL_EXPANSION"].value_counts(dropna=False)
    print(f"  TARGET_VOL_EXPANSION distribution: {dict(counts)}")
    non_nan = df["TARGET_VOL_EXPANSION"].dropna()
    if len(non_nan) > 0:
        pos_rate = (non_nan == 1).sum() / len(non_nan)
        print(f"  Positive rate: {pos_rate:.1%} (target: ~20%)")

    return df


def main():
    parser = argparse.ArgumentParser(
        description="Generate Volatility Expansion target for Exp 3."
    )
    parser.add_argument("--data", required=True, help="Input parquet path")
    parser.add_argument("--output", default=None,
                        help="Output parquet path (default: input_vol.parquet)")
    parser.add_argument("--horizon", type=int, default=288,
                        help="Forward horizon in bars (default: 288 = 24H)")
    parser.add_argument("--rolling-window", type=int, default=10080,
                        help="Rolling window in bars (default: 10080 = 35D)")
    parser.add_argument("--percentile", type=float, default=0.80,
                        help="Percentile threshold (default: 0.80 = top 20%%)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Only process first N rows (see --rows)")
    parser.add_argument("--rows", type=int, default=10000,
                        help="Rows for dry-run (default: 10000)")
    args = parser.parse_args()

    # Derive output path
    if args.output is None:
        base, ext = os.path.splitext(args.data)
        args.output = f"{base}_vol{ext}"

    print("=" * 60)
    print("GENERATE VOL EXPANSION TARGET — Experiment 3")
    print("=" * 60)
    print(f"  Input:   {args.data}")
    print(f"  Output:  {args.output}")
    print(f"  Params:  horizon={args.horizon}, "
          f"rolling={args.rolling_window}, "
          f"percentile={args.percentile}")
    print("=" * 60)

    t0 = time.perf_counter()
    df = pd.read_parquet(args.data)
    print(f"  Loaded {len(df):,} rows in {time.perf_counter() - t0:.1f}s")

    if args.dry_run:
        df = df.iloc[:args.rows].copy()
        print(f"  [DRY-RUN] Using first {len(df):,} rows only")

    df = generate_vol_expansion_target(
        df,
        forward_horizon=args.horizon,
        rolling_window=args.rolling_window,
        percentile_threshold=args.percentile,
    )

    # Save
    df.to_parquet(args.output)
    elapsed = time.perf_counter() - t0
    print(f"\n  Saved to: {args.output}")
    print(f"  Total time: {elapsed:.1f}s")
    print("=" * 60)


if __name__ == "__main__":
    main()
