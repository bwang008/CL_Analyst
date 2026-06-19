"""Prediction Parity Comparison — CSV predictions vs live model.predict().

Compares the pre-computed prediction CSVs (used by the BacktestEngine)
against model.predict() run on locally-computed features to quantify
the prediction-level divergence that drives the 37.7% PnL gap.

Steps:
  0. Data provenance check (row counts, date ranges, OHLCV alignment)
  1. Load prediction CSVs from strategy config
  2. Compute features vectorized on the local parquet + model.predict()
  3. Bar-by-bar probability comparison
  4. Find first signal flip (where binary signals disagree)
  5. Report top feature diffs at the flip timestamp

Usage:
    python scripts/prediction_parity_compare.py \\
        --config configs/strategies/HS11_Prod_Ensemble_E01_06162026.json \\
        --data C:\\CL_Analyst_Data\\data\\processed\\CL_HourSet_11.parquet

Author: CL Analyst
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Ensure project root is on sys.path
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(_PROJECT_ROOT / ".env")

from src import util
from src.features.alpha_factory import AlphaFactory


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sigmoid(x):
    """Apply sigmoid to convert logit to probability."""
    return 1.0 / (1.0 + np.exp(-x))


def load_model(model_path: str):
    """Load a LightGBM Booster from pickle or text file."""
    path = Path(model_path)
    if not path.is_absolute():
        path = _PROJECT_ROOT / path

    pure_path = str(path).replace(".pkl", "_pure.txt")
    if os.path.exists(pure_path):
        import lightgbm as lgb
        print(f"  Loading sanitized model: {pure_path}")
        return lgb.Booster(model_file=pure_path)

    print(f"  Loading model: {path}")
    with open(path, "rb") as f:
        return pickle.load(f)


def compute_vectorized_features(
    df: pd.DataFrame,
    model_feature_names: list[str],
) -> pd.DataFrame:
    """Compute features on the full DataFrame, replicating build_live_features logic.

    Uses the same AlphaFactory path as both data_processor (process_hourset_09)
    and feature_pipeline (build_live_features for 1h), ensuring identical output.

    Returns a DataFrame with columns = model_feature_names, dtype float32.
    """
    work = df.copy()

    # 1. Cyclical time features
    minutes = work.index.hour * 60 + work.index.minute
    work["Time_Sin"] = np.sin(2 * np.pi * minutes / 1440)
    work["Time_Cos"] = np.cos(2 * np.pi * minutes / 1440)
    day_of_week = work.index.dayofweek
    work["Time_DayOfWeek_Sin"] = np.sin(2 * np.pi * day_of_week / 5)
    work["Time_DayOfWeek_Cos"] = np.cos(2 * np.pi * day_of_week / 5)

    # 2. AlphaFactory (1h bar windows, matching feature_pipeline.py for bar_size="1h")
    windows = [24, 72, 168, 336, 840]
    _has_ichimoku = any(f.startswith("ICHIMOKU_") for f in model_feature_names)
    _has_dma = any(f.startswith("TREND_DMA_") for f in model_feature_names)
    _has_exh_div = any(f.startswith("EXHDIV_") for f in model_feature_names)
    _has_term_structure = any(f.startswith("TS_") for f in model_feature_names)

    factory = AlphaFactory(work, bars_per_hour=1)
    work = factory.add_all_features(
        windows=windows,
        include_momentum=True,
        include_macro=True,
        include_extended=True,
        macro_windows={"1W": 168, "2W": 336, "1M": 840, "3M": 2160, "6M": 4320},
        include_ichimoku=_has_ichimoku,
        include_dma=_has_dma,
        include_exhaustion_divergence=_has_exh_div,
        include_term_structure=_has_term_structure,
    )

    # 3. ATR_14
    if "ATR_14" not in work.columns:
        import pandas_ta as ta
        atr_series = work.ta.atr(length=14)
        if atr_series is not None:
            work["ATR_14"] = atr_series

    # 4. Volume_Log
    work["Volume_Log"] = np.log1p(work["Volume"])

    # 5. NaN handling (matching feature_pipeline: replace inf -> NaN, ffill)
    work.replace([np.inf, -np.inf], np.nan, inplace=True)
    work.ffill(inplace=True)

    # 6. Select model features + cast to float32
    missing_cols = set(model_feature_names) - set(work.columns)
    if missing_cols:
        print(f"  WARNING: {len(missing_cols)} model features missing from computed features:")
        print(f"    {sorted(missing_cols)[:10]}...")
        # Fill missing with NaN (will show up as prediction differences)
        for col in missing_cols:
            work[col] = np.nan

    result = work[model_feature_names].astype(np.float32)
    return result


# ---------------------------------------------------------------------------
# Step 0: Data Provenance Check
# ---------------------------------------------------------------------------

def step0_provenance_check(
    local_df: pd.DataFrame,
    long_pred_df: pd.DataFrame,
    short_pred_df: pd.DataFrame,
) -> bool:
    """Compare local parquet against prediction CSVs for date/row alignment."""
    print("\n" + "=" * 70)
    print("  STEP 0: DATA PROVENANCE CHECK")
    print("=" * 70)

    # Row counts
    print(f"\n  Local parquet:          {len(local_df):>8,} rows")
    print(f"  Long prediction CSV:   {len(long_pred_df):>8,} rows")
    print(f"  Short prediction CSV:  {len(short_pred_df):>8,} rows")

    # Date ranges
    print(f"\n  Local parquet range:    {local_df.index.min()} -> {local_df.index.max()}")
    print(f"  Long CSV range:        {long_pred_df.index.min()} -> {long_pred_df.index.max()}")
    print(f"  Short CSV range:       {short_pred_df.index.min()} -> {short_pred_df.index.max()}")

    # Overlap analysis
    local_idx = local_df.index
    long_idx = long_pred_df.index
    short_idx = short_pred_df.index

    overlap_long = local_idx.intersection(long_idx)
    overlap_short = local_idx.intersection(short_idx)
    print(f"\n  Local ^ Long CSV:      {len(overlap_long):>8,} bars overlap")
    print(f"  Local ^ Short CSV:     {len(overlap_short):>8,} bars overlap")

    # Bars in CSV but not in local
    csv_only_long = long_idx.difference(local_idx)
    csv_only_short = short_idx.difference(local_idx)
    if len(csv_only_long) > 0:
        print(f"\n  WARNING: {len(csv_only_long)} timestamps in LONG CSV not in local parquet")
        print(f"    First 5: {csv_only_long[:5].tolist()}")
    if len(csv_only_short) > 0:
        print(f"\n  WARNING: {len(csv_only_short)} timestamps in SHORT CSV not in local parquet")
        print(f"    First 5: {csv_only_short[:5].tolist()}")

    # Bars in local but not in CSV
    local_only_long = local_idx.difference(long_idx)
    local_only_short = local_idx.difference(short_idx)
    if len(local_only_long) > 0:
        print(f"\n  INFO: {len(local_only_long)} timestamps in local parquet not in LONG CSV")
    if len(local_only_short) > 0:
        print(f"\n  INFO: {len(local_only_short)} timestamps in local parquet not in SHORT CSV")

    # OHLCV spot-check: compare first 5 and last 5 overlap bars
    if "Open" in long_pred_df.columns and len(overlap_long) > 0:
        print(f"\n  OHLCV comparison (Long CSV has OHLCV columns):")
        check_indices = list(overlap_long[:5]) + list(overlap_long[-5:])
        for col in ["Open", "High", "Low", "Close"]:
            if col in long_pred_df.columns:
                diffs = []
                for ts in check_indices:
                    local_val = local_df.loc[ts, col]
                    csv_val = long_pred_df.loc[ts, col]
                    diffs.append(abs(local_val - csv_val))
                print(f"    {col}: max_diff={max(diffs):.6f}, mean_diff={np.mean(diffs):.6f}")
    else:
        print(f"\n  INFO: Prediction CSVs have no OHLCV columns (stripped format)")

    ok = len(overlap_long) > 0 and len(overlap_short) > 0
    if ok:
        print(f"\n  PASSED: Provenance check OK -- sufficient overlap for comparison")
    else:
        print(f"\n  FAILED: Provenance check -- no overlap between local and CSV data")
    return ok


# ---------------------------------------------------------------------------
# Steps 1-5: Prediction Comparison
# ---------------------------------------------------------------------------

def run_comparison(
    config_path: str,
    data_path: str,
    start_date: str | None = None,
    end_date: str | None = None,
):
    """Main comparison logic."""
    t0 = time.perf_counter()

    # -- Load strategy config --
    with open(config_path) as f:
        config = json.load(f)
    nickname = config.get("nickname", "unknown")
    models_cfg = config.get("models", {})
    print(f"\nStrategy: {nickname}")
    print(f"Config:   {config_path}")

    # -- Determine model paths and prediction CSV paths --
    long_cfg = models_cfg.get("long", {})
    short_cfg = models_cfg.get("short", {})

    long_model_path = long_cfg.get("model_path")
    short_model_path = short_cfg.get("model_path")
    long_pred_path = long_cfg.get("predictions_path")
    short_pred_path = short_cfg.get("predictions_path")
    long_threshold = float(long_cfg.get("threshold", 0.5))
    short_threshold = float(short_cfg.get("threshold", 0.5))

    # Check for tiered thresholds
    long_tiers = config.get("long", {}).get("tiers", [])
    short_tiers = config.get("short", {}).get("tiers", [])
    if long_tiers:
        long_threshold = min(t["min_prob"] for t in long_tiers)
    if short_tiers:
        short_threshold = min(t["min_prob"] for t in short_tiers)

    print(f"\nLong model:      {long_model_path}")
    print(f"Long CSV:        {long_pred_path}")
    print(f"Long threshold:  {long_threshold}")
    print(f"\nShort model:     {short_model_path}")
    print(f"Short CSV:       {short_pred_path}")
    print(f"Short threshold: {short_threshold}")

    # -- Step 1: Load prediction CSVs --
    print("\n" + "=" * 70)
    print("  STEP 1: LOADING PREDICTION CSVs")
    print("=" * 70)

    long_pred_df = pd.read_csv(
        _PROJECT_ROOT / long_pred_path, index_col=0, parse_dates=True
    )
    short_pred_df = pd.read_csv(
        _PROJECT_ROOT / short_pred_path, index_col=0, parse_dates=True
    )

    # Resolve probability columns
    long_prob_col = [c for c in long_pred_df.columns if "buy" in c.lower()][0]
    short_prob_col = [c for c in short_pred_df.columns if "sell" in c.lower()][0]
    print(f"  Long CSV:  {len(long_pred_df)} rows, prob column='{long_prob_col}'")
    print(f"  Short CSV: {len(short_pred_df)} rows, prob column='{short_prob_col}'")

    # -- Load local parquet --
    print(f"\n  Loading local parquet: {data_path}")
    local_df = pd.read_parquet(data_path)
    if "DateTime" in local_df.columns and not isinstance(local_df.index, pd.DatetimeIndex):
        local_df = local_df.set_index("DateTime")
    local_df.index = pd.to_datetime(local_df.index)
    print(f"  Local parquet: {len(local_df)} rows, {len(local_df.columns)} columns")

    # -- Step 0: Provenance check --
    if not step0_provenance_check(local_df, long_pred_df, short_pred_df):
        print("\n  ABORTING -- provenance check failed.")
        return

    # -- Load models --
    print("\n" + "=" * 70)
    print("  STEP 2: LOADING MODELS AND COMPUTING FEATURES")
    print("=" * 70)

    long_model = load_model(long_model_path)
    long_model_feats = long_model.feature_name()
    print(f"  Long model: {len(long_model_feats)} features")

    short_model = load_model(short_model_path)
    short_model_feats = short_model.feature_name()
    print(f"  Short model: {len(short_model_feats)} features")

    # Check if both models use the same features
    if set(long_model_feats) == set(short_model_feats):
        print(f"  Both models share the same {len(long_model_feats)} features")
        shared_feats = long_model_feats
    else:
        diff = set(long_model_feats) ^ set(short_model_feats)
        print(f"  WARNING: Models have {len(diff)} different features")
        shared_feats = None  # compute separately

    # Determine comparison window
    csv_start = max(long_pred_df.index.min(), short_pred_df.index.min())
    csv_end = min(long_pred_df.index.max(), short_pred_df.index.max())
    if start_date:
        csv_start = max(csv_start, pd.Timestamp(start_date))
    if end_date:
        csv_end = min(csv_end, pd.Timestamp(end_date))

    print(f"\n  Comparison window: {csv_start} -> {csv_end}")

    # -- Compute vectorized features --
    print(f"\n  Computing features on local parquet ({len(local_df)} bars)...")
    t_feat = time.perf_counter()

    # Need OHLCV columns for feature computation
    ohlcv_cols = ["Open", "High", "Low", "Close", "Volume"]
    if all(c in local_df.columns for c in ohlcv_cols):
        ohlcv_df = local_df[ohlcv_cols].copy()
    else:
        print("  ERROR: Local parquet missing OHLCV columns!")
        return

    # Use the union of model features to compute once
    if shared_feats:
        all_model_feats = shared_feats
    else:
        all_model_feats = list(set(long_model_feats) | set(short_model_feats))

    features_df = compute_vectorized_features(ohlcv_df, all_model_feats)
    print(f"  Features computed in {time.perf_counter() - t_feat:.1f}s")
    print(f"  Feature shape: {features_df.shape}")

    # -- Generate predictions --
    print(f"\n  Running model.predict() on local features...")

    # Restrict to comparison window
    mask = (features_df.index >= csv_start) & (features_df.index <= csv_end)
    feat_window = features_df.loc[mask]
    print(f"  Prediction window: {len(feat_window)} bars")

    # Check for NaN before prediction
    nan_rows = feat_window.isna().any(axis=1).sum()
    if nan_rows > 0:
        print(f"  WARNING: {nan_rows} bars have NaN features -- filling with ffill for prediction")
        feat_window = feat_window.ffill().bfill()

    # Long predictions
    t_pred = time.perf_counter()
    long_feat = feat_window[long_model_feats]
    long_raw = long_model.predict(long_feat)
    long_local_probs = _sigmoid(long_raw)
    print(f"  Long predictions: {len(long_local_probs)} bars")

    # Short predictions
    short_feat = feat_window[short_model_feats]
    short_raw = short_model.predict(short_feat)
    short_local_probs = _sigmoid(short_raw)
    print(f"  Short predictions: {len(short_local_probs)} bars")
    print(f"  Predictions computed in {time.perf_counter() - t_pred:.1f}s")

    # -- Step 3: Bar-by-bar comparison --
    print("\n" + "=" * 70)
    print("  STEP 3: PREDICTION COMPARISON")
    print("=" * 70)

    # Align timestamps
    compare_idx = feat_window.index.intersection(long_pred_df.index)
    compare_idx = compare_idx.intersection(short_pred_df.index)
    print(f"\n  Aligned bars for comparison: {len(compare_idx)}")

    if len(compare_idx) == 0:
        print("  FAILED: No overlapping timestamps -- cannot compare!")
        return

    # Build comparison DataFrame
    compare = pd.DataFrame(index=compare_idx)

    # Map local predictions to the aligned index
    local_long_series = pd.Series(long_local_probs, index=feat_window.index)
    local_short_series = pd.Series(short_local_probs, index=feat_window.index)

    compare["csv_long_prob"] = long_pred_df.loc[compare_idx, long_prob_col]
    compare["local_long_prob"] = local_long_series.loc[compare_idx]
    compare["long_diff"] = (compare["csv_long_prob"] - compare["local_long_prob"]).abs()

    compare["csv_short_prob"] = short_pred_df.loc[compare_idx, short_prob_col]
    compare["local_short_prob"] = local_short_series.loc[compare_idx]
    compare["short_diff"] = (compare["csv_short_prob"] - compare["local_short_prob"]).abs()

    # Statistics
    for side, diff_col, threshold in [
        ("LONG", "long_diff", long_threshold),
        ("SHORT", "short_diff", short_threshold),
    ]:
        diffs = compare[diff_col]
        print(f"\n  {side} prediction divergence:")
        print(f"    Mean:    {diffs.mean():.8f}")
        print(f"    Std:     {diffs.std():.8f}")
        print(f"    Max:     {diffs.max():.8f}")
        print(f"    P50:     {diffs.quantile(0.50):.8f}")
        print(f"    P90:     {diffs.quantile(0.90):.8f}")
        print(f"    P99:     {diffs.quantile(0.99):.8f}")
        print(f"    P99.9:   {diffs.quantile(0.999):.8f}")

        # How many exceed 1% / 5% / 10%
        pct_01 = (diffs > 0.01).sum()
        pct_05 = (diffs > 0.05).sum()
        pct_10 = (diffs > 0.10).sum()
        print(f"    > 1%:    {pct_01} bars ({100 * pct_01 / len(diffs):.2f}%)")
        print(f"    > 5%:    {pct_05} bars ({100 * pct_05 / len(diffs):.2f}%)")
        print(f"    > 10%:   {pct_10} bars ({100 * pct_10 / len(diffs):.2f}%)")

    # -- Step 4: Find first signal flip --
    print("\n" + "=" * 70)
    print("  STEP 4: SIGNAL FLIP ANALYSIS")
    print("=" * 70)

    for side, csv_col, local_col, threshold, model_feats in [
        ("LONG", "csv_long_prob", "local_long_prob", long_threshold, long_model_feats),
        ("SHORT", "csv_short_prob", "local_short_prob", short_threshold, short_model_feats),
    ]:
        csv_signal = compare[csv_col] >= threshold
        local_signal = compare[local_col] >= threshold
        flips = csv_signal != local_signal
        n_flips = flips.sum()
        print(f"\n  {side} (threshold={threshold}):")
        print(f"    CSV signals >= threshold:   {csv_signal.sum()}")
        print(f"    Local signals >= threshold: {local_signal.sum()}")
        print(f"    Signal disagreements:       {n_flips} ({100 * n_flips / len(compare):.2f}%)")

        if n_flips > 0:
            first_flip_ts = compare.index[flips][0]
            csv_p = compare.loc[first_flip_ts, csv_col]
            local_p = compare.loc[first_flip_ts, local_col]
            print(f"\n    FIRST {side} SIGNAL FLIP at: {first_flip_ts}")
            print(f"       CSV prob:   {csv_p:.6f} {'-> SIGNAL' if csv_p >= threshold else '-> no signal'}")
            print(f"       Local prob: {local_p:.6f} {'-> SIGNAL' if local_p >= threshold else '-> no signal'}")
            print(f"       Diff:       {abs(csv_p - local_p):.6f}")

            # -- Step 5: Feature diffs at flip point --
            if first_flip_ts in features_df.index:
                # Get local features at this timestamp
                local_feats_at_ts = features_df.loc[first_flip_ts, model_feats]

                # Try to get the training parquet features at this timestamp
                # (these are what the CSV prediction was computed from)
                parquet_feats = local_df.loc[first_flip_ts] if first_flip_ts in local_df.index else None

                if parquet_feats is not None:
                    # Compare features that exist in both
                    common_feats = [f for f in model_feats if f in local_df.columns]
                    if common_feats:
                        parquet_vals = local_df.loc[first_flip_ts, common_feats].astype(np.float32)
                        local_vals = local_feats_at_ts[common_feats]
                        feat_diffs = (parquet_vals - local_vals).abs()
                        # Normalize by magnitude for relative comparison
                        feat_magnitudes = parquet_vals.abs().clip(lower=1e-8)
                        feat_rel_diffs = feat_diffs / feat_magnitudes

                        top_abs = feat_diffs.nlargest(5)
                        top_rel = feat_rel_diffs.nlargest(5)

                        print(f"\n    Top 5 absolute feature diffs (parquet vs locally computed):")
                        for feat, diff_val in top_abs.items():
                            pq_val = parquet_vals[feat]
                            lo_val = local_vals[feat]
                            print(f"      {feat:50s}  parquet={pq_val:12.6f}  local={lo_val:12.6f}  diff={diff_val:.6f}")

                        print(f"\n    Top 5 relative feature diffs:")
                        for feat, diff_val in top_rel.items():
                            pq_val = parquet_vals[feat]
                            lo_val = local_vals[feat]
                            print(f"      {feat:50s}  parquet={pq_val:12.6f}  local={lo_val:12.6f}  rel_diff={diff_val:.6f}")
                    else:
                        print(f"\n    WARNING: No model features found in parquet columns for comparison")
                else:
                    print(f"\n    WARNING: Timestamp {first_flip_ts} not found in local parquet")

            # Show a few more flips for context
            flip_indices = compare.index[flips]
            n_show = min(10, len(flip_indices))
            print(f"\n    First {n_show} signal flips:")
            for ts in flip_indices[:n_show]:
                csv_p = compare.loc[ts, csv_col]
                local_p = compare.loc[ts, local_col]
                marker = "CSV=YES LOCAL=NO" if csv_p >= threshold else "CSV=NO LOCAL=YES"
                print(f"      {ts}  csv={csv_p:.6f}  local={local_p:.6f}  [{marker}]")

    # -- Summary --
    elapsed = time.perf_counter() - t0
    print("\n" + "=" * 70)
    print("  SUMMARY")
    print("=" * 70)
    print(f"  Total bars compared:     {len(compare)}")
    print(f"  Long mean diff:          {compare['long_diff'].mean():.8f}")
    print(f"  Short mean diff:         {compare['short_diff'].mean():.8f}")
    long_flips = (compare['csv_long_prob'] >= long_threshold).ne(compare['local_long_prob'] >= long_threshold).sum()
    short_flips = (compare['csv_short_prob'] >= short_threshold).ne(compare['local_short_prob'] >= short_threshold).sum()
    print(f"  Long signal flips:       {long_flips}")
    print(f"  Short signal flips:      {short_flips}")
    print(f"  Wall time:               {elapsed:.1f}s")
    print("=" * 70)

    # Save comparison CSV
    out_path = _PROJECT_ROOT / "reports" / "prediction_parity_comparison.csv"
    compare.to_csv(out_path)
    print(f"\n  Comparison saved to: {out_path}")

    return compare


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Compare prediction CSV vs live model.predict()"
    )
    parser.add_argument(
        "--config", required=True,
        help="Strategy config JSON (e.g. configs/strategies/HS11_Prod_Ensemble_E01_06162026.json)"
    )
    parser.add_argument(
        "--data", required=True,
        help="Local OHLCV parquet (e.g. C:\\CL_Analyst_Data\\data\\processed\\CL_HourSet_11.parquet)"
    )
    parser.add_argument(
        "--start-date", default=None,
        help="Start date for comparison window (YYYY-MM-DD). Default: CSV start."
    )
    parser.add_argument(
        "--end-date", default=None,
        help="End date for comparison window (YYYY-MM-DD). Default: CSV end."
    )
    args = parser.parse_args()

    run_comparison(
        config_path=args.config,
        data_path=args.data,
        start_date=args.start_date,
        end_date=args.end_date,
    )


if __name__ == "__main__":
    main()
