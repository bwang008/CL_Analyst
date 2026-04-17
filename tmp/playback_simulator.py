import os
import sys
from pathlib import Path

# Add project root to sys.path
_project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_project_root))

from dotenv import load_dotenv
load_dotenv(_project_root / ".env")

import pandas as pd
import numpy as np
import logging

from src.live_execution.live_trader import build_live_features
from src.live_execution.strategies.configurable_strategy import ConfigurableStrategy

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

def main():
    print("Loading strategy and models...")
    # Load strategy configuration
    config_path = str(_project_root / "configs" / "strategies" / "hourly_ensemble_004.json")
    strategy = ConfigurableStrategy(config_path=config_path)
    feature_names = strategy.feature_names
    learner_buy = strategy._long_learner
    
    print("Loading 1h parquet dataset...")
    parquet_path = r"C:\CL_Analyst_Data\data\processed\cl-1h_bk_HourSet_03.parquet"
    df = pd.read_parquet(parquet_path)
    
    # Ensure DateTime index
    if "DateTime" in df.columns:
        df.set_index("DateTime", inplace=True)
    df.index = pd.to_datetime(df.index, utc=True)
    # Deduplicate in case of duplicate boundaries
    df = df[~df.index.duplicated(keep='last')].sort_index()
    
    print("Loading OOS prediction CSV...")
    oos_path = _project_root / "models" / "registry" / "E2E_HourSet_03_long_average_precision" / "oos_predictions.csv"
    oos_df = pd.read_csv(oos_path, index_col=0, parse_dates=True)
    oos_df.index = pd.to_datetime(oos_df.index, utc=True)
    
    # Get sequential bars for March 2026
    target_dates = oos_df[(oos_df.index >= "2026-03-01") & (oos_df.index < "2026-04-01")].index
    if len(target_dates) == 0:
        print("No OOS dates found in March 2026")
        return
        
    print(f"Total bars in March 2026: {len(target_dates)}")
    
    # 30 sequential bars
    sample_size = min(30, len(target_dates))
    sampled_dates = target_dates[:sample_size]
    
    print(f"Running sequential inference on {len(sampled_dates)} continuous sampled bars...")
    
    results = []
    skewed_features_summary = {}
    
    for i, bar_time in enumerate(sampled_dates):
        # We need a rolling window. For 1h, strategy expects "1h" stream.
        # Longest window is MACRO_3M (2160 hours / 90 days), let's use 5000 just to be safe
        rolling_df = df[df.index <= bar_time].tail(5000)
        
        # build features exactly like the live trader
        live_features = build_live_features(
            rolling_df, 
            feature_names, 
            bar_size="1h"
        )
        
        if live_features is None:
            print(f"Failed to generate features for {bar_time}")
            continue
            
        buy_prob_live = strategy._run_inference(learner_buy, live_features)
        
        # Ground truth
        buy_prob_oos = oos_df.loc[bar_time, "prob_Buy"]
        
        diff = abs(buy_prob_live - buy_prob_oos)
        results.append({
            "bar_time": bar_time,
            "live_prob": buy_prob_live,
            "oos_prob": buy_prob_oos,
            "diff": diff
        })
        
        # --- THE PARITY DUMP ---
        offline_row = df.loc[bar_time]
        divergent_features = {}
        for col in feature_names:
            live_val = live_features[col].iloc[0]
            if col in offline_row:
                offline_val = offline_row[col]
            else:
                offline_val = np.nan
                
            if pd.isna(live_val) and pd.isna(offline_val):
                continue
            if pd.isna(live_val) or pd.isna(offline_val) or abs(live_val - offline_val) > 1e-4:
                divergent_features[col] = (live_val, offline_val)
                skewed_features_summary[col] = skewed_features_summary.get(col, 0) + 1

        if divergent_features:
            print(f"\nBar {bar_time}: Found {len(divergent_features)} divergent features:")
            for col, (lv, ov) in divergent_features.items():
                print(f"    {col}: Live={lv:.6f}, Offline={ov:.6f}")
        # -----------------------
        
        if (i+1) % 5 == 0:
            avg_diff = np.mean([r['diff'] for r in results])
            print(f"Processed {i+1}/{len(sampled_dates)} bars. Avg prob diff: {avg_diff:.6f}")

    if not results:
        print("No valid results")
        return

    print("\n" + "="*50)
    print("SKEWED FEATURES SUMMARY (over 30 continuous bars)")
    print("="*50)
    if not skewed_features_summary:
        print("No skewed features found!")
    else:
        for col, count in sorted(skewed_features_summary.items(), key=lambda x: -x[1]):
            print(f"{col}: Diverged in {count} / {len(sampled_dates)} bars")


if __name__ == "__main__":
    main()
