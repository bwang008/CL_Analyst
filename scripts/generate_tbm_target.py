#!/usr/bin/env python3
"""Generate Triple Barrier Method target for Experiment 2.

Loads an existing processed parquet (e.g. set_11), computes a new TBM target
with the specified ATR barrier parameters, appends it as TARGET_TBM_LONG,
and saves as a new parquet file.

Usage:
    python scripts/generate_tbm_target.py \
        --data /path/to/cl-5m_bk_set_11.parquet \
        --output /path/to/cl-5m_bk_set_11_tbm.parquet

    # Dry-run on first N rows:
    python scripts/generate_tbm_target.py --data ... --dry-run --rows 10000
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


def generate_tbm_target(
    df: pd.DataFrame,
    tp_atr_mult: float = 1.5,
    sl_atr_mult: float = 1.0,
    max_horizon: int = 864,    # 72 hours = 864 five-min bars
    atr_period: int = 14,
) -> pd.DataFrame:
    """Compute Triple Barrier Long target and append to DataFrame.

    This is a standalone implementation (not relying on DataProcessor) so it
    can run on the VM without importing the full data_processor module.

    Args:
        df: DataFrame with High, Low, Close columns and a DatetimeIndex.
        tp_atr_mult: ATR multiplier for take-profit (upper barrier).
        sl_atr_mult: ATR multiplier for stop-loss (lower barrier).
        max_horizon: Vertical barrier in bars (72H = 864 for 5-min data).
        atr_period: Period for ATR calculation.

    Returns:
        DataFrame with TARGET_TBM_LONG column appended.
    """
    print(f"  Generating TBM target: TP={tp_atr_mult}×ATR, SL={sl_atr_mult}×ATR, "
          f"horizon={max_horizon} bars ({max_horizon / 12:.0f}H)")

    # Compute ATR if not present
    atr_col = f"ATR_{atr_period}"
    if atr_col not in df.columns:
        import pandas_ta as ta  # noqa: F401
        df[atr_col] = df.ta.atr(length=atr_period)

    close = df["Close"].values
    high_all = df["High"].values
    low_all = df["Low"].values
    atr = df[atr_col].values
    n = len(df)

    # LONG labels: TP above entry (price rises), SL below (price falls)
    long_labels = np.zeros(n, dtype=np.float64)
    for i in range(n - 1):
        if np.isnan(atr[i]) or atr[i] <= 0:
            continue
        entry = close[i]
        tp_barrier = entry + tp_atr_mult * atr[i]
        sl_barrier = entry - sl_atr_mult * atr[i]
        end_idx = min(i + max_horizon, n)
        for j in range(i + 1, end_idx):
            if high_all[j] >= tp_barrier:
                long_labels[i] = 1
                break
            if low_all[j] <= sl_barrier:
                break

    # Mark final bars as NaN (insufficient look-ahead)
    long_labels[-max_horizon:] = np.nan

    df["TARGET_TBM_LONG"] = pd.array(long_labels, dtype="Int64")

    # Report distribution
    counts = df["TARGET_TBM_LONG"].value_counts(dropna=False)
    print(f"  TARGET_TBM_LONG distribution: {dict(counts)}")

    return df


def main():
    parser = argparse.ArgumentParser(
        description="Generate Triple Barrier Method target for Exp 2."
    )
    parser.add_argument("--data", required=True, help="Input parquet path")
    parser.add_argument("--output", default=None,
                        help="Output parquet path (default: input_tbm.parquet)")
    parser.add_argument("--tp-atr", type=float, default=1.5,
                        help="Take-profit ATR multiplier (default: 1.5)")
    parser.add_argument("--sl-atr", type=float, default=1.0,
                        help="Stop-loss ATR multiplier (default: 1.0)")
    parser.add_argument("--horizon", type=int, default=864,
                        help="Vertical barrier in bars (default: 864 = 72H)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Only process first N rows (see --rows)")
    parser.add_argument("--rows", type=int, default=10000,
                        help="Rows for dry-run (default: 10000)")
    args = parser.parse_args()

    # Derive output path
    if args.output is None:
        base, ext = os.path.splitext(args.data)
        args.output = f"{base}_tbm{ext}"

    print("=" * 60)
    print("GENERATE TBM TARGET — Experiment 2")
    print("=" * 60)
    print(f"  Input:   {args.data}")
    print(f"  Output:  {args.output}")
    print(f"  Params:  TP={args.tp_atr}×ATR, SL={args.sl_atr}×ATR, "
          f"horizon={args.horizon}")
    print("=" * 60)

    t0 = time.perf_counter()
    df = pd.read_parquet(args.data)
    print(f"  Loaded {len(df):,} rows in {time.perf_counter() - t0:.1f}s")

    if args.dry_run:
        df = df.iloc[:args.rows].copy()
        print(f"  [DRY-RUN] Using first {len(df):,} rows only")

    df = generate_tbm_target(
        df,
        tp_atr_mult=args.tp_atr,
        sl_atr_mult=args.sl_atr,
        max_horizon=args.horizon,
    )

    # Save
    df.to_parquet(args.output)
    elapsed = time.perf_counter() - t0
    print(f"\n  Saved to: {args.output}")
    print(f"  Total time: {elapsed:.1f}s")
    print("=" * 60)


if __name__ == "__main__":
    main()
