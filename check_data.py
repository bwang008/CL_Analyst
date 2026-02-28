"""
Quick diagnostic script for processed CL datasets.

Inspects dimensions, all TARGET columns, tail rows, and key feature columns.
Usage:
    python check_data.py                              # defaults to CL_set_06
    python check_data.py data/processed/CL_set_05.parquet
"""

import sys
import os
import pandas as pd


def check_dataset(path: str) -> None:
    if not os.path.exists(path):
        print(f"ERROR: File not found: {path}")
        print("  Make sure the processed dataset exists. Generate it via DataProcessor.")
        sys.exit(1)

    print(f"Loading {path}...")
    df = pd.read_parquet(path)

    # 1. Dimensions
    print(f"\n{'='*60}")
    print(f"  DATASET SUMMARY")
    print(f"{'='*60}")
    print(f"  File:    {path}")
    print(f"  Rows:    {len(df):,}")
    print(f"  Columns: {len(df.columns)}")

    # 2. All TARGET columns
    target_cols = sorted([c for c in df.columns if "TARGET" in c])
    print(f"\n{'='*60}")
    print(f"  TARGET COLUMNS ({len(target_cols)} found)")
    print(f"{'='*60}")
    if not target_cols:
        print("  WARNING: No TARGET columns found!")
    else:
        for col in target_cols:
            na_count = df[col].isna().sum()
            n_unique = df[col].nunique()
            print(f"\n  {col}:")

            # Continuous targets (TARGET_RET_*) — show summary stats
            if col.startswith("TARGET_RET_") or n_unique > 20:
                valid = df[col].dropna()
                print(f"    {'type':>8s}: continuous ({n_unique:,} unique values)")
                print(f"    {'mean':>8s}: {valid.mean():+.6f}")
                print(f"    {'std':>8s}: {valid.std():.6f}")
                print(f"    {'min':>8s}: {valid.min():+.6f}")
                print(f"    {'max':>8s}: {valid.max():+.6f}")
            else:
                # Categorical targets — show value distribution
                vc = df[col].value_counts(dropna=False).sort_index()
                for val, count in vc.items():
                    pct = count / len(df) * 100
                    print(f"    {str(val):>8s}: {count:>10,}  ({pct:5.1f}%)")

            if na_count > 0:
                print(f"    {'NaN':>8s}: {na_count:>10,}  ({na_count/len(df)*100:5.1f}%)")

    # 3. Feature columns
    feature_cols = [
        c for c in df.columns
        if not c.startswith(("RAW_", "TARGET_", "META_"))
        and c not in {"Target", "DateTime"}
    ]
    print(f"\n{'='*60}")
    print(f"  FEATURE COLUMNS ({len(feature_cols)})")
    print(f"{'='*60}")
    # Show first 10 and last 5 for brevity
    preview = feature_cols[:10]
    if len(feature_cols) > 15:
        preview += ["..."]
        preview += feature_cols[-5:]
    for col in preview:
        if col == "...":
            print(f"  ...")
        else:
            print(f"  {col}")

    # 4. Key feature spot-checks
    print(f"\n{'='*60}")
    print(f"  KEY FEATURE CHECKS")
    print(f"{'='*60}")
    checks = {
        "ATR_14": "ATR (volatility proxy)",
        "STRUC_HURST_100": "Hurst exponent (mean-reversion vs trend)",
        "Volume_Log": "Log-transformed volume",
        "Time_Sin": "Cyclical time (sin)",
        "Time_Cos": "Cyclical time (cos)",
    }
    for col, desc in checks.items():
        if col in df.columns:
            mean = df[col].mean()
            std = df[col].std()
            na = df[col].isna().sum()
            print(f"  ✓ {col:20s}  mean={mean:+.4f}  std={std:.4f}  NaN={na}")
        else:
            print(f"  ✗ {col:20s}  MISSING — {desc}")

    # 5. Tail inspection
    show_cols = ["Close"] if "Close" in df.columns else []
    show_cols += target_cols[:3]  # first 3 targets
    if show_cols:
        print(f"\n{'='*60}")
        print(f"  TAIL (last 5 rows)")
        print(f"{'='*60}")
        print(df[show_cols].tail().to_string(index=True))

    # 6. NaN summary
    total_na = df.isna().sum().sum()
    print(f"\n{'='*60}")
    print(f"  Total NaN cells: {total_na:,}")
    if total_na > 0:
        na_cols = df.isna().sum()
        na_cols = na_cols[na_cols > 0].sort_values(ascending=False)
        print(f"  Columns with NaN ({len(na_cols)}):")
        for col, count in na_cols.head(10).items():
            print(f"    {col}: {count:,}")
    print(f"{'='*60}")


if __name__ == "__main__":
    default_path = "data/processed/CL_set_06.parquet"
    path = sys.argv[1] if len(sys.argv) > 1 else default_path
    check_dataset(path)