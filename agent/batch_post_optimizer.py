"""
Batch Post-Optimizer — Run strategy optimization on all experiments in a batch.

After a batch scout/sweep run completes, this script:
  1. Discovers all completed experiments in a batch directory
  2. For each experiment x metric, optimizes LONG, SHORT, and ENSEMBLE independently
  3. Generates batch_summary_optimized.md with pre/post comparison

Usage:
    python agent/batch_post_optimizer.py --batch-dir reports/batch_runs/batch_20260511_2116
    python agent/batch_post_optimizer.py --batch-dir reports/batch_runs/batch_20260511_2116 --n-trials 500
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

import pandas as pd
from agent.backtest_engine import BacktestEngine, load_ohlcv, load_predictions
from agent.strategy_optimizer import run_optimization, extract_metrics


def find_ohlcv_path(manifest_path: str) -> str:
    """Resolve the local OHLCV parquet from the batch manifest."""
    with open(manifest_path) as f:
        manifest = json.load(f)
    gcs_path = manifest.get("defaults", {}).get("gcs_data_path", "")
    # Extract filename from GCS path: gs://bucket/data/cl-1h_bk_HourSet_08.parquet
    basename = os.path.basename(gcs_path)
    # Try common local locations
    candidates = [
        os.path.join("data", "processed", basename),
        os.path.join("data", "processed", basename.replace("cl-1h_bk_", "CL_")),
        os.path.join("data", "processed", basename.replace("cl-5m_bk_", "CL_")),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    raise FileNotFoundError(f"Cannot find local OHLCV for {gcs_path}. Tried: {candidates}")


def merge_predictions(long_path: str, short_path: str) -> pd.DataFrame:
    """Merge long + short predictions into a single DataFrame."""
    long_df = load_predictions(long_path)
    short_df = load_predictions(short_path)

    # Extract prob columns
    long_col = [c for c in long_df.columns if "buy" in c.lower()][0]
    short_col = [c for c in short_df.columns if "sell" in c.lower()][0]

    long_probs = long_df[[long_col]].rename(columns={long_col: "prob_Buy"})
    short_probs = short_df[[short_col]].rename(columns={short_col: "prob_Sell"})

    merged = long_probs.join(short_probs, how="outer").fillna(0.0)
    return merged


def run_single_optimization(
    config_path: str,
    predictions_path: str,
    ohlcv_path: str,
    n_trials: int,
    min_trades: int,
    label: str,
) -> dict:
    """Run strategy_optimizer on a single config and return results."""
    print(f"\n{'='*60}")
    print(f"OPTIMIZING: {label}")
    print(f"{'='*60}")

    try:
        best_cfg, best_result = run_optimization(
            config_path=config_path,
            n_trials=n_trials,
            predictions_path=predictions_path,
            ohlcv_path=ohlcv_path,
        )
        best_metrics = extract_metrics(best_result)
        return {
            "status": "OK",
            "config": best_cfg,
            "metrics": best_metrics,
        }
    except Exception as e:
        print(f"  ERROR: {e}")
        return {"status": "FAILED", "error": str(e)}


def align_markdown_table(lines: list[str]) -> str:
    if not lines:
        return ""
    parsed_rows = []
    for line in lines:
        if not line.strip():
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        parsed_rows.append(cells)
    if not parsed_rows:
        return ""
    max_cols = max(len(r) for r in parsed_rows)
    widths = [0] * max_cols
    for row in parsed_rows:
        for i, cell in enumerate(row):
            if i < max_cols and not set(cell).issubset({'-', ' '}):
                widths[i] = max(widths[i], len(cell))
    aligned_lines = []
    for row in parsed_rows:
        formatted_cells = []
        for i in range(max_cols):
            cell = row[i] if i < len(row) else ""
            if set(cell).issubset({'-', ' '}) and cell:
                formatted_cells.append("-" * widths[i])
            else:
                formatted_cells.append(cell.ljust(widths[i]))
        aligned_lines.append("| " + " | ".join(formatted_cells) + " |")
    return "\n".join(aligned_lines)


def generate_optimized_report(
    batch_dir: str,
    progress: dict,
    all_results: dict,
    ohlcv_path: str,
) -> str:
    """Generate batch_summary_optimized.md with pre/post comparison."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = []
    lines.append(f"# Batch Experiment Summary (Optimized) - {os.path.basename(batch_dir)}")
    lines.append(f"\nGenerated: {ts}")
    lines.append(f"Manifest: {progress.get('manifest', 'unknown')}")
    lines.append(f"Baseline Report: batch_summary.md")
    lines.append("")

    # Build comparison tables per direction
    for section_name, direction_key, metric_keys in [
        ("Long Model", "long", ["logloss", "average_precision"]),
        ("Short Model", "short", ["logloss", "average_precision"]),
        ("Ensemble", "ensemble", ["logloss", "average_precision"]),
    ]:
        for metric in metric_keys:
            lines.append(f"### {section_name} ({metric.replace('_', ' ').title()})")
            lines.append("")
            lines.append("| Experiment | Trades (pre) | Trades (opt) | PF (pre) | PF (opt) | PnL (pre) | PnL (opt) | Opt Thr | Opt TP | Opt SL | Opt Trail | Opt Cool | Opt Hold | Opt Consec |")
            lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")

            for exp in progress.get("experiments", []):
                if exp.get("status") != "COMPLETED":
                    continue
                label = exp["label"]
                local_dir = exp["local_dir"]

                # Load baseline from pipeline_summary.json
                summary_path = os.path.join(local_dir, "pipeline_summary.json")
                if not os.path.exists(summary_path):
                    continue
                with open(summary_path, encoding="utf-8-sig") as f:
                    summary = json.load(f)
                bt = summary.get("backtest_results", {})

                if direction_key == "ensemble":
                    base_key = f"ensemble_{metric}"
                else:
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
                    # Handle both in-memory (with full config) and JSON-loaded (stripped) structures
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
                    lines.append(
                        f"| {label} | {base_trades} | {opt_trades} | "
                        f"{base_pf:.2f} | {opt_pf:.2f} | "
                        f"${base_pnl:,.0f} | ${opt_pnl:,.0f} | "
                        f"{opt_thr} | {opt_tp} | {opt_sl} | {opt_trail} | {opt_cool} | {opt_hold} | {opt_consec} |"
                    )
                else:
                    reason = opt.get("error", "not run") if opt else "not run"
                    lines.append(
                        f"| {label} | {base_trades} | - | "
                        f"{base_pf:.2f} | - | "
                        f"${base_pnl:,.0f} | - | - | - | - | - | - | - | - |"
                    )
            lines.append("")

    # Detailed optimization parameters
    lines.append("---")
    lines.append("")
    lines.append("## Optimized Parameters Detail")
    lines.append("")
    for key, result in sorted(all_results.items()):
        if result.get("status") != "OK":
            continue
        if "optuna_info" in result:
            opt_info = result["optuna_info"]
        else:
            opt_info = result.get("config", {}).get("optuna_info", {})
            
        params = opt_info.get("params", {})
        metrics = result.get("metrics", {})
        baseline = opt_info.get("baseline_metrics", {})

        lines.append(f"### {key}")
        lines.append("")
        lines.append("| Parameter | Baseline | Optimized |")
        lines.append("|---|---|---|")
        lines.append(f"| Trades | {baseline.get('trade_count', '-')} | {metrics.get('trade_count', '-')} |")
        lines.append(f"| Profit Factor | {baseline.get('profit_factor', '-')} | {metrics.get('profit_factor', '-')} |")
        lines.append(f"| Win Rate | {baseline.get('win_rate', '-')} | {metrics.get('win_rate', '-')} |")
        lines.append(f"| PnL | ${baseline.get('total_pnl', 0):,.2f} | ${metrics.get('total_pnl', 0):,.2f} |")
        lines.append(f"| Max Drawdown | ${baseline.get('max_drawdown', 0):,.2f} | ${metrics.get('max_drawdown', 0):,.2f} |")
        lines.append(f"| Threshold | - | {params.get('entry_threshold', '-')} |")
        lines.append(f"| TP ATR Mult | - | {params.get('tp_atr_mult', '-')} |")
        lines.append(f"| SL ATR Mult | - | {params.get('sl_atr_mult', '-')} |")
        lines.append(f"| Trailing ATR | - | {params.get('trailing_atr_mult', '-')} |")
        lines.append(f"| Cooldown Bars | - | {params.get('cooldown_bars', '-')} |")
        lines.append(f"| Max Hold Bars | - | {params.get('max_hold_bars', '-')} |")
        lines.append(f"| Consec Signal | - | {params.get('consecutive_signal_threshold', '-')} |")
        lines.append(f"| Trials | - | {opt_info.get('n_trials', '-')} |")
        lines.append(f"| Wall Time | - | {opt_info.get('wall_time_seconds', '-')}s |")
        lines.append("")

    out_lines = []
    table_lines = []
    for line in lines:
        if line.strip().startswith("|"):
            table_lines.append(line)
        else:
            if table_lines:
                out_lines.append(align_markdown_table(table_lines))
                table_lines = []
            out_lines.append(line)
    if table_lines:
        out_lines.append(align_markdown_table(table_lines))
    return "\n".join(out_lines)


def main():
    parser = argparse.ArgumentParser(description="Batch Post-Optimizer")
    parser.add_argument("--batch-dir", required=True, help="Path to batch directory")
    parser.add_argument("--n-trials", type=int, default=500, help="Optuna trials per optimization")
    parser.add_argument("--min-trades", type=int, default=10, help="Min trades for valid trial")
    args = parser.parse_args()

    batch_dir = args.batch_dir
    progress_path = os.path.join(batch_dir, "batch_progress.json")
    if not os.path.exists(progress_path):
        print(f"ERROR: {progress_path} not found")
        sys.exit(1)

    with open(progress_path, encoding="utf-8-sig") as f:
        progress = json.load(f)

    # Resolve OHLCV data
    manifest_path = progress.get("manifest", "")
    if not os.path.isabs(manifest_path):
        manifest_path = os.path.join(PROJECT_ROOT, manifest_path)
    ohlcv_path = find_ohlcv_path(manifest_path)
    print(f"OHLCV data: {ohlcv_path}")

    # Patch strategy_optimizer min_trades threshold
    import agent.strategy_optimizer as so
    original_make_objective = so.make_objective

    def patched_make_objective(base_cfg, predictions_df, ohlcv_df, results_cache=None):
        obj = original_make_objective(base_cfg, predictions_df, ohlcv_df, results_cache)

        def patched_objective(trial):
            result = obj(trial)
            # The original returns (0.0, -999999.0) for < 50 trades
            # We re-check with our lower threshold
            if results_cache and trial.number in results_cache:
                cached = results_cache[trial.number]
                if cached.trade_count < args.min_trades:
                    return 0.0, -999999.0
                return cached.profit_factor, cached.max_drawdown
            return result

        return patched_objective

    so.make_objective = patched_make_objective

    ohlcv_df = load_ohlcv(ohlcv_path)
    all_results = {}
    total_start = time.perf_counter()

    for exp in progress.get("experiments", []):
        if exp.get("status") != "COMPLETED":
            continue

        label = exp["label"]
        local_dir = exp["local_dir"]
        canary_dir = os.path.join(local_dir, "registry", "canary_output")

        for metric in ["logloss", "average_precision"]:
            long_pred = os.path.join(canary_dir, f"oos_predictions_long_{metric}.csv")
            short_pred = os.path.join(canary_dir, f"oos_predictions_short_{metric}.csv")
            ens_config = os.path.join(canary_dir, f"ensemble_config_{metric}.json")

            if not os.path.exists(ens_config):
                print(f"  Skipping {label}/{metric}: no ensemble config")
                continue

            # --- LONG optimization ---
            if os.path.exists(long_pred):
                key = f"{label}|long|{metric}"
                # Create a long-only config
                long_cfg_path = os.path.join(canary_dir, f"_opt_long_{metric}.json")
                with open(ens_config) as f:
                    cfg = json.load(f)
                # Point predictions to long only
                cfg["models"]["long"]["predictions_path"] = long_pred.replace("\\", "/")
                with open(long_cfg_path, "w") as f:
                    json.dump(cfg, f, indent=2)

                result = run_single_optimization(
                    config_path=long_cfg_path,
                    predictions_path=long_pred,
                    ohlcv_path=ohlcv_path,
                    n_trials=args.n_trials,
                    min_trades=args.min_trades,
                    label=f"{label} LONG {metric}",
                )
                all_results[key] = result
                if os.path.exists(long_cfg_path):
                    os.remove(long_cfg_path)

            # --- SHORT optimization ---
            if os.path.exists(short_pred):
                key = f"{label}|short|{metric}"
                short_cfg_path = os.path.join(canary_dir, f"_opt_short_{metric}.json")
                with open(ens_config) as f:
                    cfg = json.load(f)
                cfg["models"]["short"]["predictions_path"] = short_pred.replace("\\", "/")
                with open(short_cfg_path, "w") as f:
                    json.dump(cfg, f, indent=2)

                result = run_single_optimization(
                    config_path=short_cfg_path,
                    predictions_path=short_pred,
                    ohlcv_path=ohlcv_path,
                    n_trials=args.n_trials,
                    min_trades=args.min_trades,
                    label=f"{label} SHORT {metric}",
                )
                all_results[key] = result
                if os.path.exists(short_cfg_path):
                    os.remove(short_cfg_path)

            # --- ENSEMBLE optimization ---
            if os.path.exists(long_pred) and os.path.exists(short_pred):
                key = f"{label}|ensemble|{metric}"
                # Create merged predictions
                merged_path = os.path.join(canary_dir, f"_merged_ens_{metric}.csv")
                merged_df = merge_predictions(long_pred, short_pred)
                merged_df.to_csv(merged_path)

                result = run_single_optimization(
                    config_path=ens_config,
                    predictions_path=merged_path,
                    ohlcv_path=ohlcv_path,
                    n_trials=args.n_trials,
                    min_trades=args.min_trades,
                    label=f"{label} ENSEMBLE {metric}",
                )
                all_results[key] = result
                if os.path.exists(merged_path):
                    os.remove(merged_path)

    elapsed = time.perf_counter() - total_start
    print(f"\n{'='*60}")
    print(f"ALL OPTIMIZATIONS COMPLETE - {elapsed:.0f}s ({elapsed/60:.1f} min)")
    print(f"{'='*60}")

    # Generate report
    report = generate_optimized_report(batch_dir, progress, all_results, ohlcv_path)
    report_path = os.path.join(batch_dir, "batch_summary_optimized.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\nOptimized report saved: {report_path}")

    # Save raw results JSON
    results_json_path = os.path.join(batch_dir, "optimization_results.json")
    serializable = {}
    for k, v in all_results.items():
        sv = {"status": v["status"]}
        if v.get("metrics"):
            sv["metrics"] = v["metrics"]
        if v.get("config", {}).get("optuna_info"):
            sv["optuna_info"] = v["config"]["optuna_info"]
        if v.get("error"):
            sv["error"] = v["error"]
        serializable[k] = sv
    with open(results_json_path, "w") as f:
        json.dump(serializable, f, indent=2, default=str)
    print(f"Raw results: {results_json_path}")


if __name__ == "__main__":
    main()
