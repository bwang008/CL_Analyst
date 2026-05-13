import os
import json
import sys

PROJECT_ROOT = r"C:\Users\bwang\Documents\GitHub\CL_Analyst_Development"
sys.path.insert(0, PROJECT_ROOT)

from agent.batch_post_optimizer import generate_optimized_report

batch_dir = os.path.join(PROJECT_ROOT, r"reports\batch_runs\batch_20260511_2116")
progress_path = os.path.join(batch_dir, "batch_progress.json")

with open(progress_path, "r", encoding="utf-8-sig") as f:
    progress = json.load(f)

all_results = {}

for exp in progress.get("experiments", []):
    if exp.get("status") != "COMPLETED":
        continue

    label = exp["label"]
    canary_dir = os.path.join(exp["local_dir"], "registry", "canary_output")

    for metric in ["logloss", "average_precision"]:
        # Only read the ensemble opt JSON, since that's what was run in the batch!
        opt_path = os.path.join(canary_dir, f"ensemble_config_{metric}_opt.json")
        if os.path.exists(opt_path):
            with open(opt_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            
            if "optuna_info" in cfg:
                optuna_info = cfg["optuna_info"]
                params = optuna_info.get("params", {})
                
                # --- ENSEMBLE ---
                ens_key = f"{label}|ensemble|{metric}"
                all_results[ens_key] = {
                    "status": "OK",
                    "config": cfg,
                    "metrics": optuna_info.get("combined_metrics", {}),
                    "optuna_info": {
                        "params": {k: v for k, v in params.items()},
                        "n_trials": optuna_info.get("n_trials"),
                        "wall_time_seconds": optuna_info.get("wall_time_seconds"),
                        "baseline_metrics": optuna_info.get("baseline_metrics", {})
                    }
                }

                # --- LONG ONLY ---
                long_key = f"{label}|long|{metric}"
                all_results[long_key] = {
                    "status": "OK",
                    "metrics": optuna_info.get("long_metrics", {}),
                    "optuna_info": {
                        "params": {
                            "entry_threshold": params.get("entry_threshold_long", "-"),
                            "tp_atr_mult": params.get("tp_atr_mult_long", "-"),
                            "sl_atr_mult": params.get("sl_atr_mult_long", "-"),
                            "trailing_atr_mult": params.get("trailing_atr_mult_long", "-"),
                            "cooldown_bars": params.get("cooldown_bars_long", "-"),
                            "max_hold_bars": params.get("max_hold_bars_long", "-"),
                            "consecutive_signal_threshold": params.get("consecutive_signal_threshold_long", "-")
                        },
                        "n_trials": optuna_info.get("n_trials"),
                        "wall_time_seconds": optuna_info.get("wall_time_seconds"),
                        "baseline_metrics": optuna_info.get("baseline_metrics", {}) # Not perfectly split but fine for now
                    }
                }

                # --- SHORT ONLY ---
                short_key = f"{label}|short|{metric}"
                all_results[short_key] = {
                    "status": "OK",
                    "metrics": optuna_info.get("short_metrics", {}),
                    "optuna_info": {
                        "params": {
                            "entry_threshold": params.get("entry_threshold_short", "-"),
                            "tp_atr_mult": params.get("tp_atr_mult_short", "-"),
                            "sl_atr_mult": params.get("sl_atr_mult_short", "-"),
                            "trailing_atr_mult": params.get("trailing_atr_mult_short", "-"),
                            "cooldown_bars": params.get("cooldown_bars_short", "-"),
                            "max_hold_bars": params.get("max_hold_bars_short", "-"),
                            "consecutive_signal_threshold": params.get("consecutive_signal_threshold_short", "-")
                        },
                        "n_trials": optuna_info.get("n_trials"),
                        "wall_time_seconds": optuna_info.get("wall_time_seconds"),
                        "baseline_metrics": optuna_info.get("baseline_metrics", {})
                    }
                }

# Re-generate the report
report = generate_optimized_report(batch_dir, progress, all_results, "dummy_ohlcv_path")

report_path = os.path.join(batch_dir, "batch_summary_optimized.md")
with open(report_path, "w", encoding="utf-8") as f:
    f.write(report)
print(f"Optimized report saved to {report_path}")

# Save raw results JSON
results_json_path = os.path.join(batch_dir, "optimization_results.json")
serializable = {}
for k, v in all_results.items():
    sv = {"status": v["status"]}
    if v.get("metrics"):
        sv["metrics"] = v["metrics"]
    if v.get("optuna_info"):
        sv["optuna_info"] = v["optuna_info"]
    serializable[k] = sv
with open(results_json_path, "w") as f:
    json.dump(serializable, f, indent=2, default=str)
print(f"Raw results saved to {results_json_path}")
