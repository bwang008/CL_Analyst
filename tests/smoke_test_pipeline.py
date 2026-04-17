import os
import sys
import time
import json
import sqlite3
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime, timezone

# Add project root to sys.path
_project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_project_root))

from dotenv import load_dotenv
load_dotenv(_project_root / ".env")

from src.data_paths import get_data_path, get_reports_root
from src.live_execution.live_trader import build_live_features
from src.live_execution.strategies.configurable_strategy import ConfigurableStrategy

def log_report(msg: str):
    print(msg)
    reports_dir = get_reports_root()
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / "HEALTH_REPORT.txt"
    with open(report_path, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now(timezone.utc).isoformat()}] {msg}\n")

def stage_1_database_integrity(strategy_config_path: Path) -> bool:
    print("--- Stage 1: Database & Logging Integrity ---")
    try:
        with open(strategy_config_path, "r") as f:
            config = json.load(f)
            
        client_id = config.get("live_config", {}).get("client_id", 10)
        db_path = get_data_path(f"live_telemetry_cid{client_id}.db")
        
        # Check repo-local vs shared root if not found
        if not db_path.exists():
            repo_local = _project_root / "data" / f"live_telemetry_cid{client_id}.db"
            if repo_local.exists():
                db_path = repo_local
                
        if not db_path.exists():
            print(f"FAIL: Telemetry DB not found at {db_path}")
            return False
        
        conn = sqlite3.connect(db_path)
        # Fetch last 50 entries
        query = "SELECT timestamp, features_json FROM shadow_log ORDER BY timestamp DESC LIMIT 50"
        df = pd.read_sql(query, conn)
        conn.close()
        
        if len(df) == 0:
            print("FAIL: No entries found in shadow_log.")
            return False
            
        latest_ts = df['timestamp'].iloc[0]
        print(f"Latest shadow_log timestamp: {latest_ts}")
        
        current_time = datetime.now(timezone.utc)
        # Parse timestamp and ensure it is timezone-aware
        last_time = pd.to_datetime(latest_ts)
        if last_time.tzinfo is None:
            last_time = last_time.tz_localize('UTC')
        else:
            last_time = last_time.tz_convert('UTC')
            
        diff_hours = (current_time - last_time).total_seconds() / 3600.0
        
        if diff_hours > 1.0:
            print(f"FAIL: Freshness Assertion Failed! Latest shadow_log entry is {diff_hours:.2f} hours old. Must be < 1.0 hour.")
            return False
        
        # Verify variance > 0 for key features
        parsed_features = []
        for feat_str in df['features_json']:
            if feat_str:
                parsed_features.append(json.loads(feat_str))
                
        if not parsed_features:
            print("FAIL: No features_json data found.")
            return False
            
        feat_df = pd.DataFrame(parsed_features)
        
        target_features = ['MACRO_VIX', 'MACRO_DXY', 'VOL_PARK_864', 'log_ret']
        features_found = False
        for tf in target_features:
            if tf in feat_df.columns:
                features_found = True
                var = feat_df[tf].astype(float).var()
                if pd.isna(var) or var == 0.0:
                    print(f"FAIL: Feature {tf} has 0.0 variance. Flatlining detected.")
                    return False
            else:
                print(f"WARN: Feature {tf} not present in logs, skipping variance check.")
                
        if not features_found:
            print("FAIL: None of the target features found in logs.")
            return False
                
        print("PASS: Database Integrity and Feature Variance verified.")
        return True
    except Exception as e:
        print(f"FAIL: Exception in Stage 1: {e}")
        return False

def stage_2_cache_validation(strategy_config_path: Path) -> bool:
    print("--- Stage 2: Cache & Artifact Validation ---")
    try:
        # Check Parquet datasets
        cache_1h = get_data_path("processed/cl-1h_bk_HourSet_03.parquet")
        if not cache_1h.exists():
            # Try plain warm_start_cache
            cache_1h = get_data_path("processed/warm_start_cache_1h.parquet")
            if not cache_1h.exists():
                print(f"FAIL: 1h Parquet cache not found.")
                return False
                
        # Check Strategy artifacts
        with open(strategy_config_path, "r") as f:
            config = json.load(f)
            
        models_root = _project_root.joinpath(config["models"]["long"]["model_path"]).parent
        if not models_root.exists():
            print(f"FAIL: Models registry path {models_root} does not exist.")
            return False
            
        long_model = _project_root / config["models"]["long"]["model_path"]
        short_model = _project_root / config["models"]["short"]["model_path"]
        
        if not long_model.exists():
            print(f"FAIL: Long model artifact not found at {long_model}")
            return False
        if not short_model.exists():
            print(f"FAIL: Short model artifact not found at {short_model}")
            return False
            
        print("PASS: Cache Data and Model Artifacts verified.")
        return True
    except Exception as e:
        print(f"FAIL: Exception in Stage 2: {e}")
        return False

def stage_3_train_serve_parity(strategy_config_path: Path) -> bool:
    print("--- Stage 3: Train-Serve Parity (Playback) ---")
    try:
        strategy = ConfigurableStrategy(config_path=str(strategy_config_path))
        feature_names = strategy.feature_names
        learner_buy = strategy._long_learner
        
        # Determine paths
        parquet_path = get_data_path("processed/cl-1h_bk_HourSet_03.parquet")
        if not parquet_path.exists():
            parquet_path = get_data_path("processed/warm_start_cache_1h.parquet")
            
        df = pd.read_parquet(parquet_path)
        if "DateTime" in df.columns:
            df.set_index("DateTime", inplace=True)
        df.index = pd.to_datetime(df.index, utc=True)
        df = df[~df.index.duplicated(keep='last')].sort_index()
        
        with open(strategy_config_path, "r") as f:
            config = json.load(f)
            
        oos_path = _project_root / config["models"]["long"]["predictions_path"]
        if not oos_path.exists():
            print(f"FAIL: OOS predictions CSV not found at {oos_path}")
            return False
            
        oos_df = pd.read_csv(oos_path, index_col=0, parse_dates=True)
        oos_df.index = pd.to_datetime(oos_df.index, utc=True)
        
        target_dates = oos_df[(oos_df.index >= "2026-03-01") & (oos_df.index < "2026-04-01")].index
        if len(target_dates) == 0:
            print("FAIL: No OOS dates found in March 2026 for parity check.")
            return False
            
        sampled_dates = target_dates[:5]
        print(f"Running sequential inference on {len(sampled_dates)} continuous sampled bars...")
        
        for bar_time in sampled_dates:
            rolling_df = df[df.index <= bar_time].tail(5000)
            
            t0 = time.perf_counter()
            live_features = build_live_features(rolling_df, feature_names, bar_size="1h")
            latency = time.perf_counter() - t0
            
            if latency > 1.0:
                print(f"WARN: Feature compilation latency exceeded 1.0s: {latency:.3f}s")
                
            if live_features is None:
                print(f"FAIL: Failed to generate features for {bar_time}")
                return False
                
            buy_prob_live = strategy._run_inference(learner_buy, live_features)
            buy_prob_oos = oos_df.loc[bar_time, "prob_Buy"]
            
            diff = abs(buy_prob_live - buy_prob_oos)
            if diff > 1.5e-3:  # Bounding allowed drift from truncated EMA warmup
                print(f"FAIL: Train-Serve Skew detected at {bar_time} exceeds 0.0015 tolerance. Live: {buy_prob_live:.6f}, OOS: {buy_prob_oos:.6f}, Diff: {diff:.6f}")
                return False
                
        print("PASS: Train-Serve Parity verified.")
        return True
    except Exception as e:
        print(f"FAIL: Exception in Stage 3: {e}")
        return False

def main():
    print("="*50)
    print("AUTOMATED SMOKE TEST PIPELINE")
    print("="*50)
    
    strategy_config = _project_root / "configs" / "strategies" / "hourly_ensemble_004.json"
    
    stage_1 = stage_1_database_integrity(strategy_config)
    stage_2 = stage_2_cache_validation(strategy_config)
    stage_3 = stage_3_train_serve_parity(strategy_config)
    
    print("\n" + "="*50)
    print("SMOKE TEST MATRIX SUMMARY")
    print("="*50)
    print(f"[Stage 1] Database Integrity: {'[PASS]' if stage_1 else '[FAIL]'}")
    print(f"[Stage 2] Cache & Artifacts:  {'[PASS]' if stage_2 else '[FAIL]'}")
    print(f"[Stage 3] Train-Serve Parity: {'[PASS]' if stage_3 else '[FAIL]'}")
    
    all_passed = stage_1 and stage_2 and stage_3
    if all_passed:
        final_status = "SUCCESS: All Pipeline Assertions Passed."
        exit_code = 0
    else:
        final_status = "FAILED: One or more Pipeline Assertions Failed. Immediate action required."
        exit_code = 1
        
    print(f"\nFINAL VERDICT: {final_status}")
    log_report(f"SMOKE TEST RUN - {final_status}")
    sys.exit(exit_code)

if __name__ == "__main__":
    main()
