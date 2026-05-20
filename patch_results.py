import json
import os
import pandas as pd
import copy
from agent.backtest_engine import BacktestEngine, load_ohlcv, load_predictions
from src.live_execution.strategies.execution_models import create_execution_strategy
from agent.batch_post_optimizer import merge_predictions
from agent.strategy_optimizer import extract_metrics

batch_dir = "reports/batch_runs/batch_20260518_2321"
with open(os.path.join(batch_dir, "batch_progress.json")) as f:
    progress = json.load(f)

manifest_path = os.path.join(batch_dir, "manifest.json")
with open(manifest_path) as f:
    manifest = json.load(f)

gcs_path = manifest.get("defaults", {}).get("gcs_data_path", "")
basename = os.path.basename(gcs_path)
candidates = [
    os.path.join("data", "processed", basename),
    os.path.join("data", "processed", basename.replace("cl-1h_bk_", "CL_")),
    os.path.join("data", "processed", basename.replace("cl-5m_bk_", "CL_")),
]
ohlcv_path = None
for c in candidates:
    if os.path.exists(c):
        ohlcv_path = c
        break

print(f"OHLCV: {ohlcv_path}")
ohlcv_df = load_ohlcv(ohlcv_path)

with open(os.path.join(batch_dir, "optimization_results.json")) as f:
    results = json.load(f)

for exp in progress.get("experiments", []):
    if exp.get("status") != "COMPLETED": continue
    label = exp["label"]
    local_dir = exp["local_dir"]
    canary_dir = os.path.join(local_dir, "registry", "canary_output")
    prefix = exp.get("gcs_prefix", "")

    for metric in ["logloss", "average_precision"]:
        opt_key = f"{label}|ensemble|{metric}"
        if opt_key not in results or results[opt_key]["status"] != "OK":
            continue
            
        long_pred = os.path.join(canary_dir, f"oos_predictions_{prefix}_long_{metric}.csv")
        if not os.path.exists(long_pred): long_pred = os.path.join(canary_dir, f"oos_predictions_long_{metric}.csv")
        short_pred = os.path.join(canary_dir, f"oos_predictions_{prefix}_short_{metric}.csv")
        if not os.path.exists(short_pred): short_pred = os.path.join(canary_dir, f"oos_predictions_short_{metric}.csv")
        
        merged_path = os.path.join(canary_dir, f"_merged_ens_{metric}.csv")
        if not os.path.exists(merged_path):
            merged_df = merge_predictions(long_pred, short_pred)
            merged_df.to_csv(merged_path)
        
        pred_df = load_predictions(merged_path)
        
        ens_config_new = os.path.join(canary_dir, f"{prefix}_{metric}.json")
        ens_config_old = os.path.join(canary_dir, f"ensemble_config_{metric}.json")
        ens_config_path = ens_config_new if os.path.exists(ens_config_new) else ens_config_old
        
        with open(ens_config_path) as f:
            base_cfg = json.load(f)
            
        opt_info = results[opt_key]["optuna_info"]
        
        cfg = copy.deepcopy(base_cfg)
        strategy = create_execution_strategy(cfg)
        if "long_params" in opt_info and "short_params" in opt_info:
            strategy.apply_trial_params(cfg, opt_info["long_params"], side="long")
            strategy.apply_trial_params(cfg, opt_info["short_params"], side="short")
        else:
            strategy.apply_trial_params(cfg, opt_info["params"])
            
        engine = BacktestEngine.from_config(cfg)
        bt_result = engine.run(pred_df, ohlcv_df)
        new_metrics = extract_metrics(bt_result)
        
        results[opt_key]["metrics"]["buy_trades"] = new_metrics["buy_trades"]
        results[opt_key]["metrics"]["sell_trades"] = new_metrics["sell_trades"]

with open(os.path.join(batch_dir, "optimization_results.json"), "w") as f:
    json.dump(results, f, indent=2)
print("Done patching.")
