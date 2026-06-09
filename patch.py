import os
import re

file_path = "agent/batch_post_optimizer.py"

with open(file_path, "r", encoding="utf-8") as f:
    content = f.read()

# 1. Update argparse in main
argparse_old = """def main():
    parser = argparse.ArgumentParser(description="Batch Post-Optimizer")
    parser.add_argument("--batch-dir", required=True, help="Path to batch directory")
    parser.add_argument("--n-trials", type=int, default=500, help="Optuna trials per optimization")"""

argparse_new = """def main():
    parser = argparse.ArgumentParser(description="Batch Post-Optimizer")
    parser.add_argument("--batch-dir", required=True, help="Path to batch directory")
    parser.add_argument("--target-pairs-json", type=str, default=None, help="JSON file with top N pairs to optimize")
    parser.add_argument("--n-trials", type=int, default=500, help="Optuna trials per optimization")"""

content = content.replace(argparse_old, argparse_new)

# 2. Update opt_tasks logic in main
tasks_old = """    # Build list of PER-SIDE optimization tasks
    # Each task: (task_key, config_path, merged_path, label, metric, side)
    opt_tasks = []
    for exp in progress.get("experiments", []):
        if exp.get("status") != "COMPLETED":
            continue

        label = exp["label"]
        local_dir = exp.get("local_dir", os.path.join(batch_dir, exp.get("gcs_prefix", "")))
        canary_dir = os.path.join(local_dir, "registry", "canary_output")

        prefix = exp.get("gcs_prefix", "")
        for metric in ["logloss", "average_precision"]:
            # Try new naming convention (with gcs_prefix), fall back to legacy
            long_pred_new = os.path.join(canary_dir, f"oos_predictions_{prefix}_long_{metric}.csv")
            long_pred_old = os.path.join(canary_dir, f"oos_predictions_long_{metric}.csv")
            long_pred = long_pred_new if os.path.exists(long_pred_new) else long_pred_old

            short_pred_new = os.path.join(canary_dir, f"oos_predictions_{prefix}_short_{metric}.csv")
            short_pred_old = os.path.join(canary_dir, f"oos_predictions_short_{metric}.csv")
            short_pred = short_pred_new if os.path.exists(short_pred_new) else short_pred_old

            ens_config_new = os.path.join(canary_dir, f"{prefix}_{metric}.json")
            ens_config_old = os.path.join(canary_dir, f"ensemble_config_{metric}.json")
            ens_config = ens_config_new if os.path.exists(ens_config_new) else ens_config_old

            if not os.path.exists(ens_config):
                print(f"  Skipping {label}/{metric}: no ensemble config")
                continue

            if not os.path.exists(long_pred) or not os.path.exists(short_pred):
                print(f"  Skipping {label}/{metric}: missing prediction files")
                continue

            # Create merged predictions (both sides need columns present for backtest engine)
            merged_path = os.path.join(canary_dir, f"_merged_ens_{metric}.csv")
            if not os.path.exists(merged_path):
                merged_df = merge_predictions(long_pred, short_pred)
                merged_df.to_csv(merged_path)

            # Per-side tasks — long and short independently
            for side in ["long", "short"]:
                task_key = f"{label}|{side}|{metric}"
                opt_tasks.append((task_key, ens_config, merged_path, label, metric, side))"""

tasks_new = """    # Build list of PER-SIDE optimization tasks
    # Each task: (task_key, config_path, merged_path, label, metric, side)
    opt_tasks = []
    
    if args.target_pairs_json and os.path.exists(args.target_pairs_json):
        print(f"Loading top target pairs from {args.target_pairs_json}")
        with open(args.target_pairs_json, "r") as f:
            top_pairs = json.load(f)
            
        base_config_name = progress.get("defaults", {}).get("strategy_config", "hourly_ensemble_010.json")
        base_config_path = os.path.join(PROJECT_ROOT, "configs", "strategies", base_config_name)
        if not os.path.exists(base_config_path):
            print(f"ERROR: base config not found: {base_config_path}")
            sys.exit(1)
            
        # Map filenames to their full paths in the batch directory
        pred_map = {}
        for root, dirs, files in os.walk(batch_dir):
            for file in files:
                if file.endswith(".csv") and "oos_predictions_" in file:
                    pred_map[file.replace(".csv", "")] = os.path.join(root, file)
                    
        for i, pair in enumerate(top_pairs):
            long_prefix = pair["target_long"]
            short_prefix = pair["target_short"]
            
            long_pred_path = pred_map.get(long_prefix)
            short_pred_path = pred_map.get(short_prefix)
            
            if not long_pred_path or not short_pred_path:
                print(f"Skipping pair: missing pred path for {long_prefix} or {short_prefix}")
                continue
                
            merged_filename = f"_merged_ens_{long_prefix}_vs_{short_prefix}.csv"
            canary_dir = os.path.dirname(long_pred_path)
            merged_path = os.path.join(canary_dir, merged_filename)
            
            if not os.path.exists(merged_path):
                merged_df = merge_predictions(long_pred_path, short_pred_path)
                merged_df.to_csv(merged_path)
                
            label = f"Ensemble_{i+1}"
            task_key = f"{long_prefix}|{short_prefix}"
            
            # Side is None for full ensemble optimization
            opt_tasks.append((task_key, base_config_path, merged_path, label, "ensemble", None))
    else:
        for exp in progress.get("experiments", []):
            if exp.get("status") != "COMPLETED":
                continue

            label = exp["label"]
            local_dir = exp.get("local_dir", os.path.join(batch_dir, exp.get("gcs_prefix", "")))
            canary_dir = os.path.join(local_dir, "registry", "canary_output")

            prefix = exp.get("gcs_prefix", "")
            for metric in ["logloss", "average_precision"]:
                # Try new naming convention (with gcs_prefix), fall back to legacy
                long_pred_new = os.path.join(canary_dir, f"oos_predictions_{prefix}_long_{metric}.csv")
                long_pred_old = os.path.join(canary_dir, f"oos_predictions_long_{metric}.csv")
                long_pred = long_pred_new if os.path.exists(long_pred_new) else long_pred_old

                short_pred_new = os.path.join(canary_dir, f"oos_predictions_{prefix}_short_{metric}.csv")
                short_pred_old = os.path.join(canary_dir, f"oos_predictions_short_{metric}.csv")
                short_pred = short_pred_new if os.path.exists(short_pred_new) else short_pred_old

                ens_config_new = os.path.join(canary_dir, f"{prefix}_{metric}.json")
                ens_config_old = os.path.join(canary_dir, f"ensemble_config_{metric}.json")
                ens_config = ens_config_new if os.path.exists(ens_config_new) else ens_config_old

                if not os.path.exists(ens_config):
                    print(f"  Skipping {label}/{metric}: no ensemble config")
                    continue

                if not os.path.exists(long_pred) or not os.path.exists(short_pred):
                    print(f"  Skipping {label}/{metric}: missing prediction files")
                    continue

                # Create merged predictions (both sides need columns present for backtest engine)
                merged_path = os.path.join(canary_dir, f"_merged_ens_{metric}.csv")
                if not os.path.exists(merged_path):
                    merged_df = merge_predictions(long_pred, short_pred)
                    merged_df.to_csv(merged_path)

                # Per-side tasks — long and short independently
                for side in ["long", "short"]:
                    task_key = f"{label}|{side}|{metric}"
                    opt_tasks.append((task_key, ens_config, merged_path, label, metric, side))"""

content = content.replace(tasks_old, tasks_new)

# 3. Update generate_optimized_report to support Ensembles (side=None)
report_old = """    # Build comparison tables — Long and Short only (no Ensemble)
    for section_name, direction_key in [
        ("Long Model", "long"),
        ("Short Model", "short"),
    ]:
        for metric in ["logloss", "average_precision"]:
            lines.append(f"### {section_name} ({metric.replace('_', ' ').title()})")
            lines.append("")
            lines.append("| Experiment | Trades (pre) | Trades (opt) | PF (pre) | PF (opt) | PnL (pre) | PnL (opt) | PnL (holdout) | Opt Thr | Opt TP | Opt SL | Opt Trail | Opt Cool | Opt Hold | Opt Consec | Best Trial |")
            lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")

            for exp in progress.get("experiments", []):
                if exp.get("status") != "COMPLETED":
                    continue
                label = exp["label"]
                local_dir = exp.get("local_dir", os.path.join(batch_dir, exp.get("gcs_prefix", "")))

                # Load baseline from pipeline_summary.json
                summary_path = os.path.join(local_dir, "pipeline_summary.json")
                if not os.path.exists(summary_path):
                    continue
                with open(summary_path, encoding="utf-8-sig") as f:
                    summary = json.load(f)
                bt = summary.get("backtest_results", {})

                base_key = f"{direction_key}_{metric}"
                base = bt.get(base_key, {})
                base_trades = base.get("trade_count", 0)
                base_pf = base.get("profit_factor", 0.0)
                base_pnl = base.get("total_pnl", 0.0)

                # Look up optimized result
                opt_key = f"{label}|{direction_key}|{metric}"
                opt = all_results.get(opt_key, {})

                if opt.get("status") == "OK":
                    om = opt.get("metrics", {})
                    if "optuna_info" in opt:
                        opt_info = opt["optuna_info"]
                    else:
                        opt_info = opt.get("config", {}).get("optuna_info", {})

                    params = opt_info.get("params", {})
                    opt_trades = om.get("trade_count", 0)
                    opt_pf = om.get("profit_factor", 0.0)
                    opt_pnl = om.get("total_pnl", 0.0)
                    opt_thr = params.get("entry_threshold", "-")
                    opt_tp = params.get("tp_atr_mult", "-")
                    opt_sl = params.get("sl_atr_mult", "-")
                    opt_trail = params.get("trailing_atr_mult", "-")
                    opt_cool = params.get("cooldown_bars", "-")
                    opt_hold = params.get("max_hold_bars", "-")
                    opt_consec = params.get("consecutive_signal_threshold", "-")
                    ho_metrics = opt_info.get("holdout_metrics", {})
                    ho_pnl = f"${ho_metrics['total_pnl']:,.0f}" if ho_metrics else "-"
                    trial_num = opt_info.get('trial_number', '-')
                    n_t = opt_info.get('n_trials', '?')
                    best_trial_str = f"#{trial_num}/{n_t}" if trial_num != '-' else "-"
                    lines.append(
                        f"| {label} | {base_trades} | {opt_trades} | "
                        f"{base_pf:.2f} | {opt_pf:.2f} | "
                        f"${base_pnl:,.0f} | ${opt_pnl:,.0f} | "
                        f"{ho_pnl} | "
                        f"{opt_thr} | {opt_tp} | {opt_sl} | {opt_trail} | {opt_cool} | {opt_hold} | {opt_consec} | {best_trial_str} |"
                    )
                else:
                    reason = opt.get("error", "not run") if opt else "not run"
                    lines.append(
                        f"| {label} | {base_trades} | - | "
                        f"{base_pf:.2f} | - | "
                        f"${base_pnl:,.0f} | - | - | - | - | - | - | - | - | - | - |"
                    )
            lines.append("")"""

report_new = """    has_ensembles = any(k.count('|') == 1 for k in all_results.keys())

    if has_ensembles:
        lines.append("### Ensembles (Top 8)")
        lines.append("")
        lines.append("| Long/Short Pair | Trades (opt) | PF (opt) | PnL (opt) | PnL (holdout) | Best Trial |")
        lines.append("|---|---|---|---|---|---|")
        for key, opt in all_results.items():
            if opt.get("status") == "OK":
                om = opt.get("metrics", {})
                opt_info = opt.get("optuna_info", opt.get("config", {}).get("optuna_info", {}))
                ho_metrics = opt_info.get("holdout_metrics", {})
                ho_pnl = f"${ho_metrics['total_pnl']:,.0f}" if ho_metrics else "-"
                trial_num = opt_info.get('trial_number', '-')
                n_t = opt_info.get('n_trials', '?')
                
                parts = key.split('|')
                pair_label = f"{parts[0][:20]}... | {parts[1][:20]}..." if len(parts) == 2 else key
                
                lines.append(
                    f"| {pair_label} | {om.get('trade_count', 0)} | "
                    f"{om.get('profit_factor', 0.0):.2f} | "
                    f"${om.get('total_pnl', 0.0):,.0f} | "
                    f"{ho_pnl} | "
                    f"#{trial_num}/{n_t} |"
                )
        lines.append("")
    else:
        # Build comparison tables — Long and Short only (no Ensemble)
        for section_name, direction_key in [
            ("Long Model", "long"),
            ("Short Model", "short"),
        ]:
            for metric in ["logloss", "average_precision"]:
                lines.append(f"### {section_name} ({metric.replace('_', ' ').title()})")
                lines.append("")
                lines.append("| Experiment | Trades (pre) | Trades (opt) | PF (pre) | PF (opt) | PnL (pre) | PnL (opt) | PnL (holdout) | Opt Thr | Opt TP | Opt SL | Opt Trail | Opt Cool | Opt Hold | Opt Consec | Best Trial |")
                lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")

                for exp in progress.get("experiments", []):
                    if exp.get("status") != "COMPLETED":
                        continue
                    label = exp["label"]
                    local_dir = exp.get("local_dir", os.path.join(batch_dir, exp.get("gcs_prefix", "")))

                    # Load baseline from pipeline_summary.json
                    summary_path = os.path.join(local_dir, "pipeline_summary.json")
                    if not os.path.exists(summary_path):
                        continue
                    with open(summary_path, encoding="utf-8-sig") as f:
                        summary = json.load(f)
                    bt = summary.get("backtest_results", {})

                    base_key = f"{direction_key}_{metric}"
                    base = bt.get(base_key, {})
                    base_trades = base.get("trade_count", 0)
                    base_pf = base.get("profit_factor", 0.0)
                    base_pnl = base.get("total_pnl", 0.0)

                    # Look up optimized result
                    opt_key = f"{label}|{direction_key}|{metric}"
                    opt = all_results.get(opt_key, {})

                    if opt.get("status") == "OK":
                        om = opt.get("metrics", {})
                        if "optuna_info" in opt:
                            opt_info = opt["optuna_info"]
                        else:
                            opt_info = opt.get("config", {}).get("optuna_info", {})

                        params = opt_info.get("params", {})
                        opt_trades = om.get("trade_count", 0)
                        opt_pf = om.get("profit_factor", 0.0)
                        opt_pnl = om.get("total_pnl", 0.0)
                        opt_thr = params.get("entry_threshold", "-")
                        opt_tp = params.get("tp_atr_mult", "-")
                        opt_sl = params.get("sl_atr_mult", "-")
                        opt_trail = params.get("trailing_atr_mult", "-")
                        opt_cool = params.get("cooldown_bars", "-")
                        opt_hold = params.get("max_hold_bars", "-")
                        opt_consec = params.get("consecutive_signal_threshold", "-")
                        ho_metrics = opt_info.get("holdout_metrics", {})
                        ho_pnl = f"${ho_metrics['total_pnl']:,.0f}" if ho_metrics else "-"
                        trial_num = opt_info.get('trial_number', '-')
                        n_t = opt_info.get('n_trials', '?')
                        best_trial_str = f"#{trial_num}/{n_t}" if trial_num != '-' else "-"
                        lines.append(
                            f"| {label} | {base_trades} | {opt_trades} | "
                            f"{base_pf:.2f} | {opt_pf:.2f} | "
                            f"${base_pnl:,.0f} | ${opt_pnl:,.0f} | "
                            f"{ho_pnl} | "
                            f"{opt_thr} | {opt_tp} | {opt_sl} | {opt_trail} | {opt_cool} | {opt_hold} | {opt_consec} | {best_trial_str} |"
                        )
                    else:
                        reason = opt.get("error", "not run") if opt else "not run"
                        lines.append(
                            f"| {label} | {base_trades} | - | "
                            f"{base_pf:.2f} | - | "
                            f"${base_pnl:,.0f} | - | - | - | - | - | - | - | - | - | - |"
                        )
                lines.append("")"""
content = content.replace(report_old, report_new)

with open(file_path, "w", encoding="utf-8") as f:
    f.write(content)
print("Patched agent/batch_post_optimizer.py")
