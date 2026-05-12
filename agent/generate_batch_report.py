import os
import json
import pandas as pd
import numpy as np
import subprocess
import joblib
from datetime import datetime
from pathlib import Path
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from agent.backtest_engine import BacktestEngine, load_ohlcv, load_predictions

BATCH_ID = "batch_20260510_1518"
BATCH_DIR = f"reports/batch_runs/{BATCH_ID}"
MANIFEST = "configs/canary_batch_hourset07_scout.json"
DATASET = "data/processed/CL_HourSet_07.parquet"
TRAIN_CUTOFF = "2023-01-01"

def extract_early_stopping(pkl_path):
    try:
        model = joblib.load(pkl_path)
        if hasattr(model, 'best_iteration'):
            return model.best_iteration
        elif hasattr(model, 'best_iteration_'):
            return model.best_iteration_
        return "Unknown"
    except Exception as e:
        return f"Error: {e}"

def extract_prob_stats(csv_path, col_name):
    try:
        df = pd.read_csv(csv_path)
        if col_name in df.columns:
            probs = df[col_name].dropna()
            if len(probs) == 0:
                return "N/A", "N/A", "N/A", "N/A"
            return round(probs.min(), 4), round(probs.max(), 4), round(probs.mean(), 4), round(probs.median(), 4)
        return "N/A", "N/A", "N/A", "N/A"
    except Exception:
        return "N/A", "N/A", "N/A", "N/A"

def extract_features(csv_path):
    try:
        df = pd.read_csv(csv_path)
        df = df.sort_values('importance', ascending=False).reset_index(drop=True)
        top10 = df.head(10).to_dict('records')
        bot10 = df.tail(10).to_dict('records')
        return len(df), top10, bot10
    except Exception:
        return 0, [], []

def main():
    progress_file = os.path.join(BATCH_DIR, "batch_progress.json")
    if not os.path.exists(progress_file):
        print(f"Error: {progress_file} not found.")
        return

    with open(progress_file, "r", encoding="utf-8-sig") as f:
        progress = json.load(f)

    started_at = progress.get("started_at", "")
    completed_at = progress.get("completed_at", "")
    
    print("Loading OHLCV data...")
    ohlcv_data = load_ohlcv(DATASET)
    print("OHLCV data loaded.")

    try:
        fmt = "%Y-%m-%d %H:%M:%S"
        total_time = datetime.strptime(completed_at, fmt) - datetime.strptime(started_at, fmt)
        total_time_str = f"{int(total_time.total_seconds() // 3600)}h {int(total_time.total_seconds() % 3600 // 60)}m"
    except:
        total_time_str = "Unknown"

    experiments = progress.get("experiments", [])
    
    model_results = []
    opt_results = {}
    detailed_data = {}
    
    # 1. Gather all baseline data
    for exp in experiments:
        if exp["status"] != "COMPLETED":
            continue
            
        local_dir = exp["local_dir"]
        label = exp["label"]
        wall_time_min = exp.get("wall_time_min", 0)
        wall_time_str = f"{wall_time_min:.1f}m"
        
        summary_file = os.path.join(local_dir, "pipeline_summary.json")
        if not os.path.exists(summary_file):
            summary_file = os.path.join(local_dir, "registry", "canary_output", "pipeline_summary.json")
            if not os.path.exists(summary_file):
                continue
                
        with open(summary_file, "r", encoding="utf-8-sig") as f:
            summary = json.load(f)
            
        bt = summary.get("backtest_results", {})
        detailed_data[label] = {"wall_time": wall_time_str, "models": {}}
        
        for metric in ["logloss", "average_precision"]:
            ens_key = f"ensemble_{metric}"
            if ens_key in bt:
                ens_res = bt[ens_key]
                model_name = f"{label.replace(' ', '_')}_{metric}"
                
                # Retrieve ensemble config to use for both ensemble and isolated recalculations
                threshold = "Unknown"
                canary_dir = os.path.join(local_dir, "registry", "canary_output")
                config_path = os.path.join(canary_dir, f"ensemble_config_{metric}.json")
                ens_cfg = None
                if os.path.exists(config_path):
                    with open(config_path, "r", encoding="utf-8-sig") as fc:
                        ens_cfg = json.load(fc)
                        threshold = ens_cfg.get("entry_threshold", "Unknown")

                # --- RECALCULATE LONG / SHORT / ENSEMBLE LOCALLY ---
                for dir_name in ["long", "short"]:
                    pred_file = os.path.join(canary_dir, f"oos_predictions_{dir_name}_{metric}.csv")
                    if os.path.exists(pred_file) and ens_cfg is not None:
                        try:
                            preds = load_predictions(pred_file)
                            # Create engine using the ensemble config (so TP/SL/threshold are correct)
                            be = BacktestEngine.from_config(ens_cfg)
                            res = be.run(preds, ohlcv_data)
                            bt[f"{dir_name}_{metric}"] = {
                                "trade_count": res.trade_count,
                                "win_rate": res.win_rate * 100,
                                "profit_factor": res.profit_factor if res.profit_factor != float("inf") else 999.0,
                                "total_pnl": res.total_pnl,
                                "max_drawdown": res.max_drawdown,
                                "sharpe_ratio": "N/A"
                            }
                        except Exception as e:
                            pass
                merged_pred_path = os.path.join(canary_dir, f"oos_predictions_ensemble_{metric}.csv")
                
                if os.path.exists(config_path):
                    with open(config_path, "r", encoding="utf-8-sig") as fc:
                        cfg = json.load(fc)
                        threshold = cfg.get("entry_threshold", "Unknown")
                    
                    # Merge predictions if not exists (we do it here just in case)
                    pred_long_path = os.path.join(canary_dir, f"oos_predictions_long_{metric}.csv")
                    pred_short_path = os.path.join(canary_dir, f"oos_predictions_short_{metric}.csv")
                    if not os.path.exists(merged_pred_path) and os.path.exists(pred_long_path) and os.path.exists(pred_short_path):
                        df_l = pd.read_csv(pred_long_path)
                        df_s = pd.read_csv(pred_short_path)
                        df_merged = pd.merge(df_l, df_s, on="DateTime", how="outer", suffixes=("_long", "_short")).fillna(0.0)
                        if "prob_Buy_long" in df_merged.columns:
                            df_merged["prob_Buy"] = df_merged["prob_Buy_long"] + df_merged.get("prob_Buy_short", 0.0)
                        if "prob_Sell_short" in df_merged.columns:
                            df_merged["prob_Sell"] = df_merged["prob_Sell_short"] + df_merged.get("prob_Sell_long", 0.0)
                        df_merged.to_csv(merged_pred_path, index=False)

                    if os.path.exists(merged_pred_path):
                        try:
                            preds = load_predictions(merged_pred_path)
                            be = BacktestEngine.from_config(cfg)
                            res = be.run(preds, ohlcv_data)
                            ens_res = {
                                "trade_count": res.trade_count,
                                "win_rate": res.win_rate * 100,
                                "profit_factor": res.profit_factor if res.profit_factor != float("inf") else 999.0,
                                "total_pnl": res.total_pnl,
                                "max_drawdown": res.max_drawdown,
                                "sharpe_ratio": "N/A"
                            }
                            bt[ens_key] = ens_res
                        except Exception as e:
                            pass

                pf = ens_res.get("profit_factor", 0.0)
                if isinstance(pf, str) and pf == "Infinity":
                    pf = 999.0

                model_results.append({
                    "model_name": model_name,
                    "target_pair": label,
                    "metric": metric,
                    "trades": ens_res.get("trade_count", 0),
                    "win_rate": ens_res.get("win_rate", 0.0),
                    "profit_factor": pf,
                    "pnl": ens_res.get("total_pnl", 0.0),
                    "max_dd": ens_res.get("max_drawdown", 0.0),
                    "wall_time": wall_time_str,
                    "local_dir": local_dir,
                    "threshold": threshold,
                    "baseline_res": ens_res
                })
                
                detailed_data[label]["models"][metric] = {
                    "ensemble": ens_res,
                    "long": bt.get(f"long_{metric}", {}),
                    "short": bt.get(f"short_{metric}", {})
                }
                
                # Fetch detailed metrics
                for dir_name in ["long", "short"]:
                    # config
                    cfg_file = os.path.join(local_dir, "registry", f"E2E_HourSet_07_{dir_name}_{metric}", "experiment_config.json")
                    if not os.path.exists(cfg_file):
                        cfg_file = os.path.join(local_dir, "reports", f"optuna_best_params_{dir_name}_{metric}.json")
                    if os.path.exists(cfg_file):
                        with open(cfg_file, "r", encoding="utf-8-sig") as fc:
                            detailed_data[label]["models"][metric][f"{dir_name}_cfg"] = json.load(fc)
                            
                    # early stopping
                    pkl_file = os.path.join(canary_dir, f"final_{dir_name}_model_{metric}.pkl")
                    detailed_data[label]["models"][metric][f"{dir_name}_best_it"] = extract_early_stopping(pkl_file)
                    
                    # features
                    feat_file = os.path.join(local_dir, "registry", f"E2E_HourSet_07_{dir_name}_{metric}", "feature_importance.csv")
                    if not os.path.exists(feat_file):
                        # try E2E run
                        feat_file = os.path.join(local_dir, "reports", f"feature_importance_{dir_name}_{metric}.csv")
                        
                    if os.path.exists(feat_file):
                        detailed_data[label]["models"][metric][f"{dir_name}_feats"] = extract_features(feat_file)
                    else:
                        detailed_data[label]["models"][metric][f"{dir_name}_feats"] = (0, [], [])
                        
                    # prob stats
                    pred_file = os.path.join(canary_dir, f"oos_predictions_{dir_name}_{metric}.csv")
                    col_tgt = "prob_Buy" if dir_name == "long" else "prob_Sell"
                    detailed_data[label]["models"][metric][f"{dir_name}_probs"] = extract_prob_stats(pred_file, col_tgt)

    # 2. Run Strategy Optimization
    for m in model_results:
        pf = m["profit_factor"]
        model_name = m["model_name"]
        metric = m["metric"]
        local_dir = m["local_dir"]
        
        if pf > 1.0:
            print(f"Optimizing winner: {model_name} (PF {pf})")
            canary_dir = os.path.join(local_dir, "registry", "canary_output")
            pred_long_path = os.path.join(canary_dir, f"oos_predictions_long_{metric}.csv")
            pred_short_path = os.path.join(canary_dir, f"oos_predictions_short_{metric}.csv")
            merged_pred_path = os.path.join(canary_dir, f"oos_predictions_ensemble_{metric}.csv")
            
            if os.path.exists(pred_long_path) and os.path.exists(pred_short_path):
                df_l = pd.read_csv(pred_long_path)
                df_s = pd.read_csv(pred_short_path)
                
                # If there are duplicate columns outside DateTime, merge suffixes
                df_merged = pd.merge(df_l, df_s, on="DateTime", how="outer", suffixes=("_long", "_short"))
                df_merged = df_merged.fillna(0.0)
                
                # Reconstruct prob_Buy and prob_Sell if they got suffixes
                if "prob_Buy_long" in df_merged.columns:
                    df_merged["prob_Buy"] = df_merged["prob_Buy_long"] + df_merged.get("prob_Buy_short", 0.0)
                if "prob_Sell_short" in df_merged.columns:
                    df_merged["prob_Sell"] = df_merged["prob_Sell_short"] + df_merged.get("prob_Sell_long", 0.0)
                    
                df_merged.to_csv(merged_pred_path, index=False)
                
                config_path = os.path.join(canary_dir, f"ensemble_config_{metric}.json")
                if os.path.exists(config_path):
                    cmd = [
                        "python", "agent/strategy_optimizer.py",
                        "--config", config_path,
                        "--n-trials", "5",
                        "--predictions", merged_pred_path,
                        "--data", DATASET
                    ]
                    subprocess.run(cmd, check=False)
                    
                    opt_config = os.path.join(canary_dir, f"ensemble_config_{metric}_opt.json")
                    if os.path.exists(opt_config):
                        with open(opt_config, "r", encoding="utf-8-sig") as f:
                            opt_data = json.load(f)
                        opt_info = opt_data.get("optuna_info", {})
                        opt_metrics = opt_info.get("metrics", {})
                        opt_results[model_name] = {
                            "opt_pf": opt_metrics.get("profit_factor", 0.0),
                            "opt_pnl": opt_metrics.get("total_pnl", 0.0),
                            "opt_cfg": opt_data
                        }

    # 3. Build Markdown Report
    
    # --- Build ALL summary rows: Ensemble + individual Long + Short ---
    all_summary_rows = []
    
    for m in model_results:
        label = m["target_pair"]
        metric = m["metric"]
        m_name = m["model_name"]
        detail = detailed_data.get(label, {}).get("models", {}).get(metric, {})
        
        # Early stopping for this ensemble's component models
        long_es = detail.get("long_best_it", "—")
        short_es = detail.get("short_best_it", "—")
        
        pf = m["profit_factor"]
        trades = m["trades"]
        if pf > 1.3 and trades >= 10:
            verdict = "✅ Promote"
        elif pf > 1.0:
            verdict = "⚠️ Thin"
        else:
            verdict = "❌ No Edge"
        
        opt = opt_results.get(m_name, {})
        opt_pf_val = opt.get("opt_pf", "—")
        opt_pnl_val = opt.get("opt_pnl", "—")
        if isinstance(opt_pnl_val, float):
            opt_pnl_val = f"${opt_pnl_val:,.2f}"
        if isinstance(opt_pf_val, float):
            opt_pf_val = f"{opt_pf_val:.2f}"
        
        anchor = label.lower().replace(" ", "-").replace(".", "")
        
        # Ensemble row
        all_summary_rows.append({
            "sort_key": pf,
            "type": "Ensemble",
            "name": m_name,
            "anchor": anchor,
            "target": label,
            "metric": metric,
            "trades": trades,
            "wr": f"{m['win_rate']:.1f}%",
            "pf": f"{pf:.4f}",
            "pnl": f"${m['pnl']:,.2f}",
            "max_dd": f"${m['max_dd']:,.2f}",
            "long_es": str(long_es),
            "short_es": str(short_es),
            "threshold": str(m["threshold"]),
            "opt_pf": str(opt_pf_val),
            "opt_pnl": str(opt_pnl_val),
            "wall_time": m["wall_time"],
            "verdict": verdict,
        })
        
        # Individual Long row
        long_res = detail.get("long", {})
        long_pf = long_res.get("profit_factor", 0.0)
        if isinstance(long_pf, str) and long_pf == "Infinity": long_pf = 999.0
        long_trades = long_res.get("trade_count", 0)
        if long_pf > 1.3 and long_trades >= 10:
            long_verdict = "✅ Promote"
        elif long_pf > 1.0:
            long_verdict = "⚠️ Thin"
        else:
            long_verdict = "❌ No Edge"
        all_summary_rows.append({
            "sort_key": long_pf,
            "type": "Long",
            "name": f"{m_name}_long",
            "anchor": anchor,
            "target": label,
            "metric": metric,
            "trades": long_trades,
            "wr": f"{long_res.get('win_rate', 0.0):.1f}%",
            "pf": f"{long_pf:.4f}",
            "pnl": f"${long_res.get('total_pnl', 0.0):,.2f}",
            "max_dd": f"${long_res.get('max_drawdown', 0.0):,.2f}",
            "long_es": str(long_es),
            "short_es": "—",
            "threshold": str(m["threshold"]),
            "opt_pf": "—",
            "opt_pnl": "—",
            "wall_time": m["wall_time"],
            "verdict": long_verdict,
        })
        
        # Individual Short row
        short_res = detail.get("short", {})
        short_pf = short_res.get("profit_factor", 0.0)
        if isinstance(short_pf, str) and short_pf == "Infinity": short_pf = 999.0
        short_trades = short_res.get("trade_count", 0)
        if short_pf > 1.3 and short_trades >= 10:
            short_verdict = "✅ Promote"
        elif short_pf > 1.0:
            short_verdict = "⚠️ Thin"
        else:
            short_verdict = "❌ No Edge"
        all_summary_rows.append({
            "sort_key": short_pf,
            "type": "Short",
            "name": f"{m_name}_short",
            "anchor": anchor,
            "target": label,
            "metric": metric,
            "trades": short_trades,
            "wr": f"{short_res.get('win_rate', 0.0):.1f}%",
            "pf": f"{short_pf:.4f}",
            "pnl": f"${short_res.get('total_pnl', 0.0):,.2f}",
            "max_dd": f"${short_res.get('max_drawdown', 0.0):,.2f}",
            "long_es": "—",
            "short_es": str(short_es),
            "threshold": str(m["threshold"]),
            "opt_pf": "—",
            "opt_pnl": "—",
            "wall_time": m["wall_time"],
            "verdict": short_verdict,
        })
    
    # Sort all rows by PF descending
    all_summary_rows.sort(key=lambda x: x["sort_key"], reverse=True)
    
    # --- Fixed-width column padding ---
    headers = ["Rank", "Type", "Model Name", "Target Pair", "Metric", "Trades",
               "WR", "PF", "PnL", "Max DD", "Long ES", "Short ES",
               "Threshold", "Opt PF", "Opt PnL", "Wall Time", "Verdict"]
    
    # Pre-compute cell values for every row so we can measure max widths
    row_cells = []
    for idx, r in enumerate(all_summary_rows):
        cells = [
            str(idx + 1),
            r["type"],
            f"[{r['name']}](#{r['anchor']})" if r["type"] == "Ensemble" else r["name"],
            r["target"],
            r["metric"],
            str(r["trades"]),
            r["wr"],
            r["pf"],
            r["pnl"],
            r["max_dd"],
            r["long_es"],
            r["short_es"],
            r["threshold"],
            r["opt_pf"],
            r["opt_pnl"],
            r["wall_time"],
            r["verdict"],
        ]
        row_cells.append(cells)
    
    # Compute column widths (at least header width, at least 4)
    col_widths = [max(4, len(h)) for h in headers]
    for cells in row_cells:
        for i, c in enumerate(cells):
            col_widths[i] = max(col_widths[i], len(c))
    
    def pad_row(cells):
        parts = []
        for i, c in enumerate(cells):
            parts.append(f" {c:<{col_widths[i]}} ")
        return "|" + "|".join(parts) + "|"
    
    def separator_row():
        parts = []
        for w in col_widths:
            parts.append("-" * (w + 2))
        return "|" + "|".join(parts) + "|"
    
    lines = []
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines.append("# HourSet_07 Batch Scout Report\n")
    lines.append(f"> Generated: {timestamp}")
    lines.append(f"> Manifest: {MANIFEST}")
    lines.append("> Dataset: cl-1h_bk_HourSet_07.parquet (242 columns)")
    lines.append(f"> Train Cutoff: {TRAIN_CUTOFF}")
    lines.append(f"> Slippage: 0.01 per side  |  Commission: $2.50 per side")
    lines.append(f"> Total Wall Clock Time: {total_time_str}")
    lines.append(f"> Experiments Run: {len(experiments)} target pairs × 2 metrics = {len(experiments)*2} model pairs\n")
    
    lines.append("## Executive Summary & Rankings\n")
    lines.append(pad_row(headers))
    lines.append(separator_row())
    for cells in row_cells:
        lines.append(pad_row(cells))
        
    lines.append("\n---\n")
    lines.append("## Detailed Results per Target\n")
    
    # Render details
    for target_pair, details in detailed_data.items():
        anchor = target_pair.lower().replace(" ", "-").replace(".", "")
        lines.append(f"### <a id=\"{anchor}\"></a>{target_pair}\n")
        lines.append(f"**Wall Clock Time**: {details['wall_time']}\n")
        
        for dir_name in ["long", "short"]:
            target_col = f"TARGET_TRIPLE_{target_pair.replace(' ', '_')}_{dir_name.upper()}"
            lines.append(f"#### {dir_name.upper()} — {target_col}\n")
            
            for metric in ["logloss", "average_precision"]:
                lines.append(f"**{metric.replace('_', ' ').title()} Model**:\n")
                
                mdata = details["models"].get(metric, {})
                dres = mdata.get(dir_name, {})
                cfg = mdata.get(f"{dir_name}_cfg", {})
                best_it = mdata.get(f"{dir_name}_best_it", "Unknown")
                feats = mdata.get(f"{dir_name}_feats", (0, [], []))
                probs = mdata.get(f"{dir_name}_probs", ("N/A", "N/A", "N/A", "N/A"))
                
                # Format early stopping
                n_estimators = cfg.get("n_estimators", "Unknown")
                early_stopped = "Yes" if str(best_it) != str(n_estimators) and best_it != "Unknown" else "No"
                
                lines.append("| Metric | Value |")
                lines.append("|---|---|")
                lines.append(f"| Trades | {dres.get('trade_count', 0)} |")
                lines.append(f"| Win Rate | {dres.get('win_rate', 0.0):.1f}% |")
                pf_val = dres.get('profit_factor', 0.0)
                if isinstance(pf_val, str) and pf_val == "Infinity": pf_val = 999.0
                lines.append(f"| Profit Factor | {pf_val:.4f} |")
                lines.append(f"| Net PnL | ${dres.get('total_pnl', 0.0):,.2f} |")
                lines.append(f"| Max Drawdown | ${dres.get('max_drawdown', 0.0):,.2f} |")
                lines.append(f"| Features Trained | {feats[0]} |")
                lines.append(f"| n_estimators (config) | {n_estimators} |")
                lines.append(f"| best_iteration (actual) | {best_it} |")
                lines.append(f"| Early Stopped? | {early_stopped} |")
                lines.append(f"| num_leaves | {cfg.get('num_leaves', 'Unknown')} |")
                lines.append(f"| max_depth | {cfg.get('max_depth', 'Unknown')} |")
                lines.append(f"| learning_rate | {cfg.get('learning_rate', 'Unknown')} |")
                lines.append(f"| feature_fraction | {cfg.get('feature_fraction', 'Unknown')} |")
                lines.append(f"| Probability Min | {probs[0]} |")
                lines.append(f"| Probability Max | {probs[1]} |")
                lines.append(f"| Probability Mean | {probs[2]} |")
                lines.append(f"| Probability Median | {probs[3]} |")
                lines.append("")
                
                lines.append("**Top 10 Features** (by gain importance):")
                lines.append("| Rank | Feature | Importance |")
                lines.append("|---|---|---:|")
                for i, f in enumerate(feats[1]):
                    lines.append(f"| {i+1} | {f.get('feature', 'Unknown')} | {f.get('importance', 0.0):.4f} |")
                lines.append("")
                
                lines.append("**Bottom 10 Features**:")
                lines.append("| Rank | Feature | Importance |")
                lines.append("|---|---|---:|")
                for i, f in enumerate(feats[2]):
                    lines.append(f"| {feats[0]-9+i} | {f.get('feature', 'Unknown')} | {f.get('importance', 0.0):.4f} |")
                lines.append("")
            lines.append("---\n")
            
        # Ensemble Results
        lines.append("#### Ensemble Results\n")
        lines.append("| Metric | Logloss Ensemble | Avg Precision Ensemble |")
        lines.append("|---|---:|---:|")
        
        ml_ens = details["models"].get("logloss", {}).get("ensemble", {})
        map_ens = details["models"].get("average_precision", {}).get("ensemble", {})
        
        def safe_fmt(val, is_pct=False, is_money=False):
            if val is None or val == "N/A": return "N/A"
            if isinstance(val, str) and val == "Infinity": return "999.00"
            if is_pct: return f"{val:.1f}%"
            if is_money: return f"${val:,.2f}"
            return f"{val:.4f}" if isinstance(val, float) else str(val)
            
        lines.append(f"| Trades | {ml_ens.get('trade_count', 0)} | {map_ens.get('trade_count', 0)} |")
        lines.append(f"| Win Rate | {safe_fmt(ml_ens.get('win_rate'), is_pct=True)} | {safe_fmt(map_ens.get('win_rate'), is_pct=True)} |")
        lines.append(f"| Profit Factor | {safe_fmt(ml_ens.get('profit_factor'))} | {safe_fmt(map_ens.get('profit_factor'))} |")
        lines.append(f"| Net PnL | {safe_fmt(ml_ens.get('total_pnl'), is_money=True)} | {safe_fmt(map_ens.get('total_pnl'), is_money=True)} |")
        lines.append(f"| Max Drawdown | {safe_fmt(ml_ens.get('max_drawdown'), is_money=True)} | {safe_fmt(map_ens.get('max_drawdown'), is_money=True)} |")
        lines.append("\n")
        
        # Optimization Results
        lines.append("#### Strategy Optimization\n")
        for metric in ["logloss", "average_precision"]:
            m_name = f"{target_pair.replace(' ', '_')}_{metric}"
            if m_name in opt_results:
                opt = opt_results[m_name]
                cfg = opt["opt_cfg"]
                opt_info = cfg.get("optuna_info", {})
                b_metrics = opt_info.get("baseline_metrics", {})
                o_metrics = opt_info.get("metrics", {})
                
                lines.append(f"**{metric.title()}**\n")
                lines.append("| | Baseline | Optimized | Delta |")
                lines.append("|---|---:|---:|---:|")
                
                pf_base = b_metrics.get('profit_factor', 0.0)
                pf_opt = o_metrics.get('profit_factor', 0.0)
                lines.append(f"| Profit Factor | {pf_base:.4f} | {pf_opt:.4f} | {pf_opt - pf_base:+.4f} |")
                
                pnl_base = b_metrics.get('total_pnl', 0.0)
                pnl_opt = o_metrics.get('total_pnl', 0.0)
                lines.append(f"| Net PnL | ${pnl_base:,.2f} | ${pnl_opt:,.2f} | ${pnl_opt - pnl_base:+,.2f} |")
                
                dd_base = b_metrics.get('max_drawdown', 0.0)
                dd_opt = o_metrics.get('max_drawdown', 0.0)
                lines.append(f"| Max Drawdown | ${dd_base:,.2f} | ${dd_opt:,.2f} | ${dd_opt - dd_base:+,.2f} |")
                
                lines.append(f"| Threshold | {cfg.get('entry_threshold')} | {opt_info.get('params', {}).get('entry_threshold')} | - |")
                lines.append(f"| TP ATR Mult | {cfg.get('tp_atr_mult')} | {opt_info.get('params', {}).get('tp_atr_mult')} | - |")
                lines.append(f"| SL ATR Mult | {cfg.get('sl_atr_mult')} | {opt_info.get('params', {}).get('sl_atr_mult')} | - |")
                lines.append(f"| Trailing ATR | {cfg.get('trailing_atr_mult')} | {opt_info.get('params', {}).get('trailing_atr_mult')} | - |")
                lines.append(f"| Max Hold Bars | {cfg.get('max_hold_bars')} | {opt_info.get('params', {}).get('max_hold_bars')} | - |")
                lines.append("\n")
                
        lines.append("---\n")

    lines.append("## Observations & Recommendations\n")
    lines.append("*(To be completed after review)*\n")

    report_path = "reports/HourSet_07_Batch_Scout_Report.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"\nReport generated at: {report_path}")

if __name__ == "__main__":
    main()
