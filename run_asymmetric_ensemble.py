import os
import json
import pandas as pd
from agent.backtest_engine import BacktestEngine, load_ohlcv, format_report

# ... skipping down to the report generation ...
batch_dir = "reports/batch_runs/batch_20260511_2116"
results_path = os.path.join(batch_dir, "optimization_results.json")
with open(results_path, encoding="utf-8-sig") as f:
    results = json.load(f)

long_opt = results["HS08 3x1 6H|long|logloss"]
short_opt = results["HS08 3x1 6H|short|logloss"]

def get_params(opt_dict):
    if "optuna_info" in opt_dict:
        return opt_dict["optuna_info"]["params"]
    return opt_dict.get("config", {}).get("optuna_info", {}).get("params", {})

long_params = get_params(long_opt)
short_params = get_params(short_opt)

# 2. Base ensemble config
base_config_path = r"reports\scout_hs08_3x1_6h_20260511_2116\registry\canary_output\ensemble_config_logloss.json"
with open(base_config_path) as f:
    ens_config = json.load(f)

# 3. Inject the optimized, direction-specific parameters into the ensemble config
ens_config["nickname"] = "HourSet_08_Ensemble_01"
ens_config["description"] = "Merged optimized LONG and SHORT parameters into a single ensemble."
ens_config["allow_concurrent"] = True # Allowing concurrent so both long and short can hold positions

# Update Long models dict
ens_config["models"]["long"]["threshold"] = long_params["entry_threshold"]

# Update Long execution logic
ens_config["long"]["tp_atr_mult"] = long_params["tp_atr_mult"]
ens_config["long"]["sl_atr_mult"] = long_params["sl_atr_mult"]
ens_config["long"]["trailing_atr_mult"] = long_params["trailing_atr_mult"]
ens_config["long"]["cooldown_bars"] = long_params["cooldown_bars"]
ens_config["long"]["max_hold_bars"] = long_params["max_hold_bars"]
ens_config["long"]["consecutive_signal_threshold"] = long_params["consecutive_signal_threshold"]
ens_config["long"]["tiers"] = [{"min_prob": long_params["entry_threshold"], "lots": 1}]
ens_config["long"]["tiered_exits"] = [{"qty_pct": 1.0, "tp_atr_mult": long_params["tp_atr_mult"]}]

# Update Short models dict
ens_config["models"]["short"]["threshold"] = short_params["entry_threshold"]

# Update Short execution logic
ens_config["short"]["tp_atr_mult"] = short_params["tp_atr_mult"]
ens_config["short"]["sl_atr_mult"] = short_params["sl_atr_mult"]
ens_config["short"]["trailing_atr_mult"] = short_params["trailing_atr_mult"]
ens_config["short"]["cooldown_bars"] = short_params["cooldown_bars"]
ens_config["short"]["max_hold_bars"] = short_params["max_hold_bars"]
ens_config["short"]["consecutive_signal_threshold"] = short_params["consecutive_signal_threshold"]
ens_config["short"]["tiers"] = [{"min_prob": short_params["entry_threshold"], "lots": 1}]
ens_config["short"]["tiered_exits"] = [{"qty_pct": 1.0, "tp_atr_mult": short_params["tp_atr_mult"]}]

# The global parameters will be used as fallbacks if needed, so we set them to neutral/broad values
ens_config["tp_atr_mult"] = "Tiered"
ens_config["sl_atr_mult"] = "Tiered"
ens_config["trailing_atr_mult"] = 100.0

# Save this custom config
custom_config_path = "reports/HourSet_08_Ensemble_01.json"
with open(custom_config_path, "w", encoding="utf-8") as f:
    json.dump(ens_config, f, indent=2)

# 4. Run the backtest
ohlcv_df = load_ohlcv("data/processed/CL_HourSet_08.parquet")
merged_preds_path = r"reports\scout_hs08_3x1_6h_20260511_2116\registry\canary_output\_merged_ens_logloss.csv"
# We need to recreate the merged predictions since the optimizer deleted the temporary file
long_preds_path = r"reports\scout_hs08_3x1_6h_20260511_2116\registry\canary_output\oos_predictions_long_logloss.csv"
short_preds_path = r"reports\scout_hs08_3x1_6h_20260511_2116\registry\canary_output\oos_predictions_short_logloss.csv"

long_df = pd.read_csv(long_preds_path, index_col=0, parse_dates=True)
short_df = pd.read_csv(short_preds_path, index_col=0, parse_dates=True)

long_col = [c for c in long_df.columns if "buy" in c.lower()][0]
short_col = [c for c in short_df.columns if "sell" in c.lower()][0]

long_probs = long_df[[long_col]].rename(columns={long_col: "prob_Buy"})
short_probs = short_df[[short_col]].rename(columns={short_col: "prob_Sell"})
merged_preds = long_probs.join(short_probs, how="outer").fillna(0.0)

engine = BacktestEngine.from_config(ens_config)
result = engine.run(merged_preds, ohlcv_df)

report_text = format_report(result, config=ens_config)

output_report_path = "reports/HourSet_08_Optimized_100_Trials.md"
with open(output_report_path, "w", encoding="utf-8") as f:
    f.write(f"# Optimized Asymmetric Ensemble - HS08 3x1 6H Logloss\n\n")
    f.write(f"This backtest explicitly stitches the individually optimized LONG and SHORT configurations into a single strategy, rather than running a symmetric search over the ensemble as a whole.\n\n")
    f.write(f"## Long Side Parameters:\n")
    f.write(f"- Threshold: {long_params['entry_threshold']}\n")
    f.write(f"- TP: {long_params['tp_atr_mult']}x\n")
    f.write(f"- SL: {long_params['sl_atr_mult']}x\n")
    f.write(f"- Trailing: {long_params['trailing_atr_mult']}x\n")
    f.write(f"- Cooldown: {long_params['cooldown_bars']}\n")
    f.write(f"- Max Hold: {long_params['max_hold_bars']}\n")
    f.write(f"- Consec Sigs: {long_params['consecutive_signal_threshold']}\n\n")
    f.write(f"## Short Side Parameters:\n")
    f.write(f"- Threshold: {short_params['entry_threshold']}\n")
    f.write(f"- TP: {short_params['tp_atr_mult']}x\n")
    f.write(f"- SL: {short_params['sl_atr_mult']}x\n")
    f.write(f"- Trailing: {short_params['trailing_atr_mult']}x\n")
    f.write(f"- Cooldown: {short_params['cooldown_bars']}\n")
    f.write(f"- Max Hold: {short_params['max_hold_bars']}\n")
    f.write(f"- Consec Sigs: {short_params['consecutive_signal_threshold']}\n\n")
    f.write(f"## Backtest Engine Report:\n```text\n")
    f.write(report_text)
    f.write("\n```\n")

print(f"Merged config saved to: {custom_config_path}")
print(f"Report saved to: {output_report_path}")
print(report_text)
