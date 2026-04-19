import pandas as pd
import json
import os
import sys

# Ensure project root is on path
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

from gcp.vm_e2e_pipeline import train_final_model, generate_oos_predictions, run_backtest, create_registry_bundle
import src.util as util

DATA_PATH = r"C:\CL_Analyst_Data\data\processed\cl-4h_bk_set_01.parquet"
OUTPUT_DIR = "reports/production_4h_v2/canary_output"
os.makedirs(OUTPUT_DIR, exist_ok=True)

df = pd.read_parquet(DATA_PATH)
feature_cols = util.get_feature_columns(df)
cutoff = pd.Timestamp("2022-01-01")
df_train = df[df.index < cutoff].copy()
df_vault = df[df.index >= cutoff].copy()

targets = [
    ("TARGET_TRIPLE_2x1_30B_LONG", "long", "average_precision", "optuna_best_params_long_average_precision.json"),
    ("TARGET_TRIPLE_2x1_30B_SHORT", "short", "logloss", "optuna_best_params_short_logloss.json"),
    ("TARGET_TRIPLE_2x1_30B_SHORT", "short", "average_precision", "optuna_best_params_short_average_precision.json"),
    ("TARGET_TRIPLE_2x1_30B_LONG", "long", "logloss", "optuna_best_params_long_logloss.json")
]

with open("configs/strategies/ensemble4.json") as f:
    base_strategy_cfg = json.load(f)

reports = {}
for target_name, direction, metric, json_name in targets:
    print(f"\nProcessing {direction} {metric}...")
    target_col = util.get_target_column(df, target_name)
    df_train_sub = df_train.dropna(subset=[target_col])
    df_vault_sub = df_vault.dropna(subset=[target_col])
    
    with open(f"reports/production_4h_v2/reports/{json_name}") as f:
        data = json.load(f)
        params = data.get("model_params_for_experiment_runner", data["best_hyperparameters"])
        
    model_path = os.path.join(OUTPUT_DIR, f"final_{direction}_{metric}.pkl")
    model = train_final_model(df_train_sub, feature_cols, target_col, params, "downsample", model_path)
    
    preds_path = os.path.join(OUTPUT_DIR, f"oos_{direction}_{metric}.csv")
    preds_df = generate_oos_predictions(model, df_vault_sub, feature_cols, target_col, direction, preds_path)
    
    # Force Short model threshold to 0.55
    import copy
    strategy_cfg = copy.deepcopy(base_strategy_cfg)
    if direction == "short":
        strategy_cfg["entry_threshold"] = 0.55
        if "models" not in strategy_cfg: strategy_cfg["models"] = {}
        if "short" not in strategy_cfg["models"]: strategy_cfg["models"]["short"] = {}
        strategy_cfg["models"]["short"]["threshold"] = 0.55
    
    bt_path = os.path.join(OUTPUT_DIR, f"bt_{direction}_{metric}.txt")
    report = run_backtest(preds_df, strategy_cfg, direction, bt_path)
    # inject the best score for formatting
    report["best_score"] = data.get("best_score", 0.0)
    reports[f"{direction}_{metric}"] = report
    
    bundle_name = f"E2E_4h_set_01_{direction}_{metric}"
    bundle_dir = os.path.join("models", "registry", bundle_name)
    os.makedirs(bundle_dir, exist_ok=True)
    
    create_registry_bundle(
        bundle_dir, model, model_path, preds_path, bt_path,
        params, metric, target_name, direction, DATA_PATH, "2022-01-01", "local_scout_import"
    )

print("\nRESULTS_START")
for name, r in reports.items():
    print(f"Model: {name} | Score: {r.get('best_score')} | Trades: {r['trade_count']} | WR: {r['win_rate']} | PF: {r['profit_factor']} | PnL: {r['total_pnl']} | MaxDD: {r['max_drawdown']}")
print("RESULTS_END")
