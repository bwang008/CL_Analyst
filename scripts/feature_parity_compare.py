"""
Feature Parity Investigation: Backtester vs Livetest Feature Comparison.

This script extracts the feature vector from both pipelines at the same
timestamp and compares them to identify which features diverge and by how much.

Pipeline A (Backtester): Full OHLCV history -> AlphaFactory -> features
Pipeline B (Livetest):   2,200-bar cache    -> AlphaFactory -> features (via build_live_features)

Usage:
    python scripts/feature_parity_compare.py
"""

import os
import sys
import json
import numpy as np
import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.features.alpha_factory import AlphaFactory
from src.live_execution.feature_pipeline import build_live_features


# ── Configuration ──────────────────────────────────────────────────────
PARQUET_PATH = r"C:\CL_Analyst_Data\data\processed\CL_HourSet_11.parquet"
CONFIG_PATH = os.path.join(PROJECT_ROOT, "configs", "strategies",
                           "HS11_Prod_Ensemble_E01_06162026.json")
TARGET_TIMESTAMP = "2025-06-15 10:00:00"
LIVETEST_CACHE_SIZE = 2200  # bars


def load_feature_names_from_model(config_path: str) -> list[str]:
    """Extract the exact 183 feature names from the trained model."""
    with open(config_path) as f:
        cfg = json.load(f)

    model_path = os.path.join(PROJECT_ROOT, cfg["models"]["long"]["model_path"])
    if not os.path.exists(model_path):
        print(f"WARNING: Model file not found at {model_path}")
        print("Falling back to features from config globs...")
        return None

    import pickle
    with open(model_path, "rb") as f:
        model = pickle.load(f)

    # LightGBM Booster: feature_name() is a method
    if hasattr(model, "feature_name") and callable(model.feature_name):
        return list(model.feature_name())
    elif hasattr(model, "feature_name_"):
        return list(model.feature_name_)
    elif hasattr(model, "feature_names_in_"):
        return list(model.feature_names_in_)
    else:
        print("WARNING: Cannot extract feature names from model")
        return None


def load_ohlcv(path: str) -> pd.DataFrame:
    """Load the hourly parquet data."""
    df = pd.read_parquet(path)
    # Ensure DatetimeIndex
    if not isinstance(df.index, pd.DatetimeIndex):
        if "DateTime" in df.columns:
            df = df.set_index("DateTime")
        elif "datetime" in df.columns:
            df = df.set_index("datetime")
    print(f"Loaded {len(df)} hourly bars: {df.index[0]} -> {df.index[-1]}")
    return df


def compute_backtest_features(ohlcv: pd.DataFrame,
                               feature_names: list[str]) -> pd.Series:
    """
    Pipeline A: Backtester path.
    
    Runs AlphaFactory on the FULL dataset (same as process_hourset_09)
    and extracts the feature row at TARGET_TIMESTAMP.
    """
    print("\n" + "=" * 60)
    print("PIPELINE A: Backtester (full history)")
    print("=" * 60)

    # The backtester uses the pre-computed parquet which already has features.
    # But to get the OHLCV-derived features, we need to recompute from raw OHLCV.
    # The parquet already has features embedded, so let's check if we can use those.

    # First, check if the parquet already has all features
    available = set(ohlcv.columns) & set(feature_names)
    if len(available) == len(feature_names):
        print(f"  All {len(feature_names)} features found pre-computed in parquet")
        ts = pd.Timestamp(TARGET_TIMESTAMP)
        if ts not in ohlcv.index:
            # Find closest
            idx = ohlcv.index.get_indexer([ts], method="nearest")[0]
            ts = ohlcv.index[idx]
            print(f"  Closest timestamp: {ts}")
        row = ohlcv.loc[ts, feature_names]
        print(f"  Extracted feature row at {ts}")
        return row

    # Otherwise, recompute features from OHLCV columns
    print(f"  Only {len(available)}/{len(feature_names)} features pre-computed.")
    print("  Recomputing features from OHLCV...")

    # Get raw OHLCV columns
    ohlcv_cols = ["Open", "High", "Low", "Close", "Volume"]
    work = ohlcv[ohlcv_cols].copy()

    # Add time features
    minutes = work.index.hour * 60 + work.index.minute
    work["Time_Sin"] = np.sin(2 * np.pi * minutes / 1440)
    work["Time_Cos"] = np.cos(2 * np.pi * minutes / 1440)
    day_of_week = work.index.dayofweek
    work["Time_DayOfWeek_Sin"] = np.sin(2 * np.pi * day_of_week / 5)
    work["Time_DayOfWeek_Cos"] = np.cos(2 * np.pi * day_of_week / 5)

    # Replicate process_hourset_09 AlphaFactory call
    windows = [24, 72, 168, 336, 840]
    macro_windows = {"1W": 168, "2W": 336, "1M": 840, "3M": 2160, "6M": 4320}

    _has_ichimoku = any(f.startswith("ICHIMOKU_") for f in feature_names)
    _has_dma = any(f.startswith("TREND_DMA_") for f in feature_names)
    _has_term_structure = any(f.startswith("TS_") for f in feature_names)

    factory = AlphaFactory(work, bars_per_hour=1)
    work = factory.add_all_features(
        windows=windows,
        include_momentum=True,
        include_macro=True,
        include_extended=True,
        include_dma=_has_dma,
        include_ichimoku=_has_ichimoku,
        include_term_structure=_has_term_structure,
        macro_windows=macro_windows,
        log_progress=True,
    )

    # ATR_14 (added by data_processor during target generation)
    if "ATR_14" not in work.columns:
        import pandas_ta as ta
        atr_s = work.ta.atr(length=14)
        if atr_s is not None:
            work["ATR_14"] = atr_s

    # Volume_Log
    work["Volume_Log"] = np.log1p(work["Volume"])

    # Replace inf
    work.replace([np.inf, -np.inf], np.nan, inplace=True)
    work.ffill(inplace=True)

    ts = pd.Timestamp(TARGET_TIMESTAMP)
    if ts not in work.index:
        idx = work.index.get_indexer([ts], method="nearest")[0]
        ts = work.index[idx]
        print(f"  Closest timestamp: {ts}")

    missing = set(feature_names) - set(work.columns)
    if missing:
        print(f"  WARNING: {len(missing)} features still missing: {sorted(missing)[:10]}...")

    available_feats = [f for f in feature_names if f in work.columns]
    row = work.loc[ts, available_feats]
    print(f"  Extracted {len(row)}/{len(feature_names)} features at {ts}")
    return row


def compute_livetest_features(ohlcv: pd.DataFrame,
                               feature_names: list[str]) -> pd.Series:
    """
    Pipeline B: Livetest path.
    
    Takes only a 2,200-bar slice ending at TARGET_TIMESTAMP and runs
    build_live_features() — same as the live trader does.
    """
    print("\n" + "=" * 60)
    print("PIPELINE B: Livetest (2,200-bar cache)")
    print("=" * 60)

    ts = pd.Timestamp(TARGET_TIMESTAMP)

    # Get raw OHLCV columns only
    ohlcv_cols = ["Open", "High", "Low", "Close", "Volume"]
    full_ohlcv = ohlcv[ohlcv_cols].copy()

    if ts not in full_ohlcv.index:
        idx = full_ohlcv.index.get_indexer([ts], method="nearest")[0]
        ts = full_ohlcv.index[idx]
        print(f"  Closest timestamp: {ts}")

    # Find position of target timestamp
    ts_pos = full_ohlcv.index.get_loc(ts)
    start_pos = max(0, ts_pos - LIVETEST_CACHE_SIZE + 1)
    cache_slice = full_ohlcv.iloc[start_pos:ts_pos + 1].copy()
    print(f"  Cache slice: {len(cache_slice)} bars, "
          f"{cache_slice.index[0]} -> {cache_slice.index[-1]}")

    # Run build_live_features (same as livetest_engine.py does)
    result = build_live_features(
        cache_slice,
        feature_names,
        lean=False,
        bar_size="1h",
    )

    if result is None:
        print("  ERROR: build_live_features returned None!")
        return pd.Series(dtype=float)

    row = result.iloc[0]
    print(f"  Extracted {len(row)} features at {result.index[0]}")
    return row


def compare_features(bt_row: pd.Series, lt_row: pd.Series,
                     feature_names: list[str]) -> pd.DataFrame:
    """Compare feature vectors and produce divergence table."""
    print("\n" + "=" * 60)
    print("FEATURE COMPARISON")
    print("=" * 60)

    results = []
    for feat in feature_names:
        bt_val = bt_row.get(feat, np.nan)
        lt_val = lt_row.get(feat, np.nan)

        if pd.isna(bt_val) and pd.isna(lt_val):
            abs_diff = 0.0
            pct_diff = 0.0
        elif pd.isna(bt_val) or pd.isna(lt_val):
            abs_diff = float("inf")
            pct_diff = float("inf")
        else:
            abs_diff = abs(float(bt_val) - float(lt_val))
            if abs(float(bt_val)) > 1e-10:
                pct_diff = abs_diff / abs(float(bt_val)) * 100
            else:
                pct_diff = 0.0 if abs_diff < 1e-10 else float("inf")

        results.append({
            "feature": feat,
            "backtest_value": float(bt_val) if not pd.isna(bt_val) else np.nan,
            "livetest_value": float(lt_val) if not pd.isna(lt_val) else np.nan,
            "abs_diff": abs_diff,
            "pct_diff": pct_diff,
        })

    df = pd.DataFrame(results)
    df = df.sort_values("abs_diff", ascending=False).reset_index(drop=True)
    return df


def classify_feature(feat_name: str) -> str:
    """Classify a feature by its computation type."""
    if "ZSCORE" in feat_name:
        return "ZSCORE (rolling z-score)"
    if "CMF" in feat_name:
        return "CMF (Chaikin Money Flow)"
    if "OBV" in feat_name:
        return "OBV (On-Balance Volume — cumulative)"
    if "VWAP" in feat_name:
        return "VWAP (Volume-weighted avg price)"
    if "PPO" in feat_name or "MACD" in feat_name:
        return "EMA-based (infinite memory)"
    if "RSI" in feat_name:
        return "RSI (EMA-based)"
    if "BB" in feat_name:
        return "Bollinger Bands"
    if "MACRO" in feat_name:
        return "MACRO (long-window Donchian)"
    if "VOL_PARK" in feat_name or "VOL_YZ" in feat_name or "VOL_RS" in feat_name:
        return "Volatility (rolling window)"
    if "VOL_ROC" in feat_name:
        return "Vol Rate of Change"
    if "VOL_VOLVOL" in feat_name:
        return "Vol of Vol"
    if "DONCHIAN" in feat_name:
        return "Donchian position"
    if "LR_SLOPE" in feat_name or "LR_R2" in feat_name:
        return "Linear regression"
    if "HURST" in feat_name or "ENTROPY" in feat_name:
        return "Physics (Hurst/Entropy)"
    if "AMIHUD" in feat_name:
        return "Liquidity (Amihud)"
    if "CORWIN" in feat_name:
        return "Liquidity (Corwin-Schultz)"
    if "EFFICIENCY" in feat_name:
        return "Structure (efficiency ratio)"
    if "STOCH" in feat_name:
        return "Stochastic oscillator"
    if "ICHIMOKU" in feat_name:
        return "Ichimoku Cloud"
    if "DMA" in feat_name:
        return "Displaced Moving Average"
    if "DIFF" in feat_name:
        return "Term Structure (Diff)"
    if "RATIO" in feat_name:
        return "Term Structure (Ratio)"
    if "INVERT" in feat_name:
        return "Term Structure (Invert)"
    if "SIGN_AGREE" in feat_name:
        return "Term Structure (Sign Agreement)"
    if "REGIME_CROSS" in feat_name:
        return "Term Structure (Regime Cross)"
    if "DIST_SKEW" in feat_name or "DIST_KURT" in feat_name:
        return "Return distribution"
    if "log_ret" in feat_name:
        return "Log return"
    if "ATR" in feat_name:
        return "ATR"
    return "Other"


def diagnose_divergence(feat_name: str, bt_val: float, lt_val: float) -> str:
    """Provide a mathematical diagnosis for the divergence."""
    category = classify_feature(feat_name)

    if "ZSCORE" in feat_name:
        return (
            "Z-Score uses rolling(anchor_w, min_periods=1).mean() and .std() "
            "on the ANCHOR series. With full history, the rolling stats use "
            "up to anchor_w=840 prior values. With 2200-bar cache, fewer "
            "historical values are available for the first ~840 bars of the "
            "cache, but at the TARGET position (bar ~2200), both should have "
            "~840 prior values. HOWEVER: the anchor series ITSELF differs "
            "because features feeding into it (e.g., VOL_PARK_840, CMF_840) "
            "depend on long history. The Z-Score amplifies upstream divergence "
            "by dividing by a potentially different std."
        )

    if "CMF" in feat_name:
        return (
            "CMF = sum(CLV*Volume, window) / sum(Volume, window). "
            "This is a bounded rolling window (NOT cumulative like ADL). "
            "Both pipelines should produce identical CMF for window ≤ cache. "
            "Any divergence comes from upstream indicator divergence or "
            "different data availability at the window edges."
        )

    if "OBV" in feat_name:
        return (
            "OBV is a CUMULATIVE SUM: OBV[t] = OBV[t-1] ± Volume[t]. "
            "Starting from different points gives completely different baselines. "
            "OBV_SLOPE uses linreg on OBV, so slope should be similar but "
            "DIVERGENCE (normalized OBV_slope - price_slope) may differ."
        )

    if "PPO" in feat_name or "EMA" in feat_name:
        return (
            "PPO uses EMA(12) and EMA(26) which have INFINITE MEMORY. "
            "ewm(span=N, adjust=False) means: "
            "EMA[0] = close[0], EMA[t] = α*close[t] + (1-α)*EMA[t-1]. "
            "Starting from different close[0] values means different seeds. "
            "After 2200 bars, α^2200 ≈ 0, so the seed effect is negligible "
            "for span=12/26. Divergence should be tiny."
        )

    if "MACRO" in feat_name:
        return (
            "MACRO features use rolling(window, min_periods=1) on raw OHLCV. "
            "With min_periods=1, both pipelines should converge once they have "
            "enough bars. But MACRO_6M uses window=4320 hours, which exceeds "
            "the 2200-bar cache — live will use a shorter effective window."
        )

    return f"Category: {category}. See detailed analysis."


def main():
    print("Feature Parity Investigation")
    print("Comparing BacktestEngine vs LiveTrader feature pipelines")
    print(f"Target timestamp: {TARGET_TIMESTAMP}")
    print()

    # Load feature names from model
    feature_names = load_feature_names_from_model(CONFIG_PATH)
    if feature_names is None:
        print("ERROR: Cannot determine feature names. Aborting.")
        return

    print(f"\nModel expects {len(feature_names)} features")
    print(f"  TS_* features:      {sum(1 for f in feature_names if f.startswith('TS_'))}")
    print(f"  ICHIMOKU_* features:{sum(1 for f in feature_names if f.startswith('ICHIMOKU_'))}")
    print(f"  TREND_DMA_*:        {sum(1 for f in feature_names if f.startswith('TREND_DMA_'))}")
    print(f"  Other:              {sum(1 for f in feature_names if not f.startswith(('TS_', 'ICHIMOKU_', 'TREND_DMA_')))}")

    # Load OHLCV data
    ohlcv = load_ohlcv(PARQUET_PATH)

    # Pipeline A: Backtester (full history)
    bt_row = compute_backtest_features(ohlcv, feature_names)

    # Pipeline B: Livetest (2200-bar cache)
    lt_row = compute_livetest_features(ohlcv, feature_names)

    # Compare
    comparison = compare_features(bt_row, lt_row, feature_names)

    # Save full comparison
    output_path = os.path.join(PROJECT_ROOT, "reports", "feature_parity_comparison.csv")
    comparison.to_csv(output_path, index=False)
    print(f"\nFull comparison saved to: {output_path}")

    # ── Print Top 20 Divergent Features ───────────────────────────────
    print("\n" + "=" * 80)
    print("TOP 20 DIVERGENT FEATURES (sorted by absolute difference)")
    print("=" * 80)
    top20 = comparison.head(20)
    for i, row in top20.iterrows():
        feat = row["feature"]
        cat = classify_feature(feat)
        print(f"\n  {i+1:2d}. {feat}")
        print(f"      Backtest:  {row['backtest_value']:>14.6f}")
        print(f"      Livetest:  {row['livetest_value']:>14.6f}")
        print(f"      Abs Diff:  {row['abs_diff']:>14.6f}")
        print(f"      Pct Diff:  {row['pct_diff']:>14.2f}%")
        print(f"      Category:  {cat}")

    # ── Summary Statistics ────────────────────────────────────────────
    print("\n" + "=" * 80)
    print("SUMMARY STATISTICS")
    print("=" * 80)

    total = len(comparison)
    exact = len(comparison[comparison["abs_diff"] == 0])
    tiny = len(comparison[(comparison["abs_diff"] > 0) & (comparison["pct_diff"] < 0.1)])
    small = len(comparison[(comparison["pct_diff"] >= 0.1) & (comparison["pct_diff"] < 1.0)])
    medium = len(comparison[(comparison["pct_diff"] >= 1.0) & (comparison["pct_diff"] < 10.0)])
    large = len(comparison[comparison["pct_diff"] >= 10.0])

    print(f"  Total features:      {total}")
    print(f"  Exact match (0.0):   {exact}")
    print(f"  Tiny   (<0.1%):      {tiny}")
    print(f"  Small  (0.1-1.0%):   {small}")
    print(f"  Medium (1.0-10.0%):  {medium}")
    print(f"  Large  (>10.0%):     {large}")

    # ── Category breakdown for divergent features ─────────────────────
    print("\n" + "=" * 80)
    print("DIVERGENCE BY FEATURE CATEGORY")
    print("=" * 80)

    divergent = comparison[comparison["pct_diff"] > 0.1].copy()
    if len(divergent) > 0:
        divergent["category"] = divergent["feature"].apply(classify_feature)
        cat_summary = divergent.groupby("category").agg(
            count=("feature", "count"),
            mean_pct_diff=("pct_diff", "mean"),
            max_pct_diff=("pct_diff", "max"),
        ).sort_values("mean_pct_diff", ascending=False)
        print(cat_summary.to_string())

    # ── Deep-dive: ZSCORE and CMF features ────────────────────────────
    print("\n" + "=" * 80)
    print("DEEP DIVE: TS_CMF_* and *_ZSCORE_* FEATURES")
    print("=" * 80)

    zscore_feats = comparison[comparison["feature"].str.contains("ZSCORE")]
    cmf_feats = comparison[comparison["feature"].str.contains("CMF")]

    print(f"\n  ZSCORE features: {len(zscore_feats)} total")
    if len(zscore_feats) > 0:
        print(f"    Mean abs diff:  {zscore_feats['abs_diff'].mean():.6f}")
        print(f"    Max abs diff:   {zscore_feats['abs_diff'].max():.6f}")
        print(f"    Mean pct diff:  {zscore_feats['pct_diff'].mean():.2f}%")
        print(f"    Max pct diff:   {zscore_feats['pct_diff'].max():.2f}%")
        worst = zscore_feats.nlargest(5, "abs_diff")
        for _, r in worst.iterrows():
            print(f"    {r['feature']:45s}  BT={r['backtest_value']:12.6f}  "
                  f"LT={r['livetest_value']:12.6f}  "
                  f"Diff={r['abs_diff']:12.6f}  ({r['pct_diff']:.2f}%)")

    print(f"\n  CMF features: {len(cmf_feats)} total")
    if len(cmf_feats) > 0:
        print(f"    Mean abs diff:  {cmf_feats['abs_diff'].mean():.6f}")
        print(f"    Max abs diff:   {cmf_feats['abs_diff'].max():.6f}")
        print(f"    Mean pct diff:  {cmf_feats['pct_diff'].mean():.2f}%")
        print(f"    Max pct diff:   {cmf_feats['pct_diff'].max():.2f}%")
        worst = cmf_feats.nlargest(5, "abs_diff")
        for _, r in worst.iterrows():
            print(f"    {r['feature']:45s}  BT={r['backtest_value']:12.6f}  "
                  f"LT={r['livetest_value']:12.6f}  "
                  f"Diff={r['abs_diff']:12.6f}  ({r['pct_diff']:.2f}%)")

    # ── Deep-dive: OBV DIVERGENCE features ────────────────────────────
    print("\n" + "=" * 80)
    print("DEEP DIVE: OBV SLOPE/DIVERGENCE FEATURES")
    print("=" * 80)

    obv_feats = comparison[comparison["feature"].str.contains("OBV|DIVERGENCE")]
    if len(obv_feats) > 0:
        for _, r in obv_feats.iterrows():
            diagnosis = diagnose_divergence(r["feature"], r["backtest_value"], r["livetest_value"])
            print(f"\n  {r['feature']}")
            print(f"    BT={r['backtest_value']:12.6f}  LT={r['livetest_value']:12.6f}  "
                  f"Diff={r['abs_diff']:12.6f}  ({r['pct_diff']:.2f}%)")
            print(f"    Diagnosis: {diagnosis}")

    # ── Deep-dive: EMA-based features ─────────────────────────────────
    print("\n" + "=" * 80)
    print("DEEP DIVE: EMA-BASED FEATURES (PPO, RSI)")
    print("=" * 80)

    ema_feats = comparison[comparison["feature"].str.contains("PPO|RSI")]
    if len(ema_feats) > 0:
        for _, r in ema_feats.iterrows():
            print(f"  {r['feature']:45s}  BT={r['backtest_value']:12.6f}  "
                  f"LT={r['livetest_value']:12.6f}  "
                  f"Diff={r['abs_diff']:12.6f}  ({r['pct_diff']:.2f}%)")

    # ── Mathematical Root Cause Diagnosis ─────────────────────────────
    print("\n" + "=" * 80)
    print("MATHEMATICAL ROOT CAUSE DIAGNOSIS")
    print("=" * 80)

    for i, row in top20.iterrows():
        feat = row["feature"]
        if row["pct_diff"] < 0.1:
            continue
        diagnosis = diagnose_divergence(feat, row["backtest_value"], row["livetest_value"])
        print(f"\n  {feat}")
        print(f"    {diagnosis}")


if __name__ == "__main__":
    main()
