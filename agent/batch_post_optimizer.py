"""
Batch Post-Optimizer — Run strategy optimization on all experiments in a batch.

After a batch scout/sweep run completes, this script:
  1. Discovers all completed experiments in a batch directory
  2. For each experiment x metric, optimizes LONG, SHORT, and ENSEMBLE independently
  3. Generates batch_summary_optimized.md with pre/post comparison

Usage:
    python agent/batch_post_optimizer.py --batch-dir reports/batch_runs/batch_20260511_2116
    python agent/batch_post_optimizer.py --batch-dir reports/batch_runs/batch_20260511_2116 --n-trials 500
    python agent/batch_post_optimizer.py --batch-dir reports/batch_runs/batch_20260511_2116 --n-trials 500 --workers 4
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

import pandas as pd
from agent.backtest_engine import BacktestEngine, load_ohlcv, load_ohlcv_dual, load_predictions
from agent.strategy_optimizer import run_optimization, extract_metrics, send_telegram, suppress_telegram


import re as _re

def shorten_model_name(name: str) -> str:
    """Shorten verbose model names for readability.
    
    'sweep_hs10_3x1_6h_20260609_0026_registry_E2E_HourSet_10_long_average_precision' -> 'HS10_AP_LONG'
    'registry_E2E_HourSet_10_short_logloss' -> 'HS10_LL_SHORT'
    """
    # Strip experiment prefix (everything before 'registry_' or 'E2E_')
    n = name
    if "registry_E2E_" in n:
        n = n[n.index("registry_E2E_"):]
    elif "E2E_" in n:
        n = n[n.index("E2E_"):]
    n = n.replace("registry_", "").replace("E2E_", "")
    
    # Extract HourSet number
    hs_match = _re.search(r'HourSet_(\d+)', n)
    hs = f"HS{hs_match.group(1)}" if hs_match else ""
    
    # Extract direction
    direction = ""
    if "_long_" in n or n.endswith("_long"):
        direction = "LONG"
    elif "_short_" in n or n.endswith("_short"):
        direction = "SHORT"
    
    # Extract metric
    metric = ""
    if "average_precision" in n or "average_pre" in n:
        metric = "AP"
    elif "logloss" in n:
        metric = "LL"
    
    parts = [p for p in [hs, metric, direction] if p]
    return "_".join(parts) if parts else name


def shorten_model_side(name: str) -> str:
    """Extract just the metric + direction from a model name for ensemble display.
    
    'sweep_hs10_3x1_6h_20260609_0026_registry_E2E_HourSet_10_long_average_precision' -> 'AP_LONG'
    'registry_E2E_HourSet_10_short_logloss' -> 'LL_SHORT'
    """
    # Strip experiment prefix (everything before 'registry_' or 'E2E_')
    n = name
    if "registry_E2E_" in n:
        n = n[n.index("registry_E2E_"):]
    elif "E2E_" in n:
        n = n[n.index("E2E_"):]
    n = n.replace("registry_", "").replace("E2E_", "")
    direction = ""
    if "_long_" in n or n.endswith("_long"):
        direction = "LONG"
    elif "_short_" in n or n.endswith("_short"):
        direction = "SHORT"
    metric = ""
    if "average_precision" in n or "average_pre" in n:
        metric = "AP"
    elif "logloss" in n:
        metric = "LL"
    parts = [p for p in [metric, direction] if p]
    return "_".join(parts) if parts else name



def _unique_model_name(model_path: str) -> str:
    """Build a unique model name from a model directory path.
    
    Combines the experiment directory (e.g. 'sweep_hs10_3x1_6h_20260609_0026')
    with the model basename (e.g. 'E2E_HourSet_10_long_logloss') to produce a
    globally unique key. Skips intermediate 'registry' and 'canary_output' dirs.
    
    Must match the logic in sweep_ensembles.py._unique_model_name() exactly.
    
    Example:
        'reports/sweep_hs10_3x1_6h_20260609_0026/registry/canary_output/registry/E2E_HourSet_10_long_logloss'
        -> 'sweep_hs10_3x1_6h_20260609_0026_E2E_HourSet_10_long_logloss'
    """
    parts = model_path.replace("\\", "/").split("/")
    basename = parts[-1]  # e.g. "E2E_HourSet_10_long_logloss"
    
    # Walk backwards from the model dir to find the experiment directory
    # (skip 'registry', 'canary_output', 'reports' and similar generic names)
    skip_dirs = {"registry", "canary_output", "reports", "batch_runs", ".", ""}
    experiment_dir = ""
    for part in reversed(parts[:-1]):
        if part.lower() not in skip_dirs:
            experiment_dir = part
            break
    
    if experiment_dir:
        return f"{experiment_dir}_{basename}"
    return basename


def _build_gcs_prefix_to_label(progress: dict) -> dict:
    """Build a mapping from gcs_prefix -> experiment label from batch_progress.json.
    
    E.g. 'sweep_hs10_3x1_6h_20260608_2220' -> 'HS10 3x1 6H'
    Also builds a stripped version without timestamp for fuzzy matching:
    'sweep_hs10_3x1_6h' -> 'HS10 3x1 6H'
    """
    mapping = {}
    for exp in progress.get("experiments", []):
        gcs_prefix = exp.get("gcs_prefix", "")
        label = exp.get("label", "")
        if gcs_prefix and label:
            mapping[gcs_prefix] = label
            # Also store a version stripped of the trailing timestamp
            # e.g. 'sweep_hs10_3x1_6h_20260608_2220' -> 'sweep_hs10_3x1_6h'
            stripped = _re.sub(r'_\d{8}_\d{4}$', '', gcs_prefix)
            if stripped != gcs_prefix:
                mapping[stripped] = label
    return mapping


def _label_from_pred_path(pred_path: str, gcs_to_label: dict) -> str:
    """Extract experiment label from a prediction path by matching directory names.
    
    Path like:
      reports/sweep_hs10_3x1_6h_20260608_2220/registry/canary_output/E2E_.../oos_predictions.csv
    Contains 'sweep_hs10_3x1_6h_20260608_2220' which maps to 'HS10 3x1 6H'.
    """
    parts = pred_path.replace("\\", "/").split("/")
    for part in parts:
        if part in gcs_to_label:
            return gcs_to_label[part]
    return ""


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
        # Cross-resolution fallback: 1h GCS name -> 5m local (or vice versa)
        os.path.join("data", "processed", basename.replace("cl-1h_bk_", "cl-5m_bk_")),
        os.path.join("data", "processed", basename.replace("cl-5m_bk_", "cl-1h_bk_")),
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
    holdout_months: int = 0,
    n_jobs: int = 1,
    quiet: bool = False,
    objective_metric: str = "sharpe",
    optimize_side: str | None = None,
    exec_ohlcv_path: str | None = None,
    slippage_per_side: float | None = None,
) -> dict:
    """Run strategy_optimizer on a single config and return results."""
    # Suppress per-worker Telegram notifications — the batch orchestrator
    # sends its own milestone progress messages.  Without this, 32 workers
    # each sending start+complete messages (64 total) overwhelm Telegram's
    # 1 msg/sec/chat rate limit and most messages get silently dropped.
    suppress_telegram(True)

    print(f"\n{'='*60}")
    print(f"OPTIMIZING: {label}")
    print(f"{'='*60}")

    try:
        best_cfg, best_result = run_optimization(
            config_path=config_path,
            n_trials=n_trials,
            predictions_path=predictions_path,
            ohlcv_path=ohlcv_path,
            holdout_months=holdout_months,
            n_jobs=n_jobs,
            quiet=quiet,
            label=label,
            objective_metric=objective_metric,
            optimize_side=optimize_side,
            exec_ohlcv_path=exec_ohlcv_path,
            slippage_per_side=slippage_per_side,
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
    wall_time_seconds: float = 0.0,
    n_trials: int = 0,
    n_workers: int = 1,
    objective_metric: str = "sharpe",
    task_experiment_labels: dict | None = None,
) -> str:
    """Generate batch_summary_optimized_{objective}.md with pre/post comparison."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    obj_title = objective_metric.capitalize()
    lines = []
    lines.append(f"# Batch Experiment Summary (Optimized - {obj_title}) - {os.path.basename(batch_dir)}")
    lines.append(f"\nGenerated: {ts}")
    lines.append(f"Manifest: {progress.get('manifest', 'unknown')}")
    lines.append(f"Baseline Report: batch_summary.md")
    lines.append(f"Objective: {obj_title}")
    lines.append(f"Total Wall Time: {wall_time_seconds:.0f}s ({wall_time_seconds/60:.1f} min)")
    lines.append(f"Trials per target: {n_trials} | Workers: {n_workers}")
    lines.append("")

    has_ensembles = any(k.count('|') == 1 for k in all_results.keys())

    # Resolve experiment labels for ensemble keys
    exp_labels = task_experiment_labels or {}

    if has_ensembles:
        lines.append(f"### Ensembles (Top {len(all_results)})")
        lines.append("")
        lines.append("| # | Experiment | Long Model | Short Model | Trades (pre) T/L/S | Trades (opt) T/L/S | Trades (ho) T/L/S | PF (pre) | PF (opt) | PnL (pre) | PnL (opt) | PnL (holdout) | Opt Thr | Best Trial |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for idx, (key, opt) in enumerate(all_results.items(), 1):
            # Derive experiment + model display names
            parts = key.split('|')
            long_model = shorten_model_side(parts[0]) if len(parts) == 2 else key
            short_model = shorten_model_side(parts[1]) if len(parts) == 2 else ""

            # Get experiment labels from the threaded metadata
            labels_info = exp_labels.get(key, {})
            long_exp = labels_info.get("long_label", "")
            short_exp = labels_info.get("short_label", "")
            if long_exp and short_exp:
                experiment_col = f"{long_exp} / {short_exp}"
            elif long_exp:
                experiment_col = long_exp
            elif short_exp:
                experiment_col = short_exp
            else:
                experiment_col = "-"

            if long_exp:
                long_model = f"{long_model} ({long_exp})"
            if short_exp:
                short_model = f"{short_model} ({short_exp})"

            if opt.get("status") == "OK":
                om = opt.get("metrics", {})
                opt_info = opt.get("optuna_info", opt.get("config", {}).get("optuna_info", {}))
                ho_metrics = opt_info.get("holdout_metrics", {})
                ho_pnl = f"${ho_metrics['total_pnl']:,.0f}" if ho_metrics else "-"
                if ho_metrics:
                    ho_t = ho_metrics.get('trade_count', '-')
                    ho_l = ho_metrics.get('buy_trades', '-')
                    ho_s = ho_metrics.get('sell_trades', '-')
                    ho_trades = f"{ho_t}/{ho_l}/{ho_s}"
                else:
                    ho_trades = "-"
                trial_num = opt_info.get('trial_number', '-')
                n_t = opt_info.get('n_trials', '?')
                baseline = opt_info.get("baseline_metrics", {})

                # Baseline metrics
                base_t = baseline.get('trade_count', '-')
                base_l = baseline.get('buy_trades', '-')
                base_s = baseline.get('sell_trades', '-')
                base_trades = f"{base_t}/{base_l}/{base_s}"
                base_pf = f"{baseline['profit_factor']:.2f}" if 'profit_factor' in baseline else '-'
                base_pnl = f"${baseline['total_pnl']:,.0f}" if 'total_pnl' in baseline else '-'

                # Optimized metrics
                opt_t = om.get('trade_count', 0)
                opt_l = om.get('buy_trades', 0)
                opt_s = om.get('sell_trades', 0)
                opt_trades = f"{opt_t}/{opt_l}/{opt_s}"
                opt_pf = f"{om.get('profit_factor', 0.0):.2f}"
                opt_pnl = f"${om.get('total_pnl', 0.0):,.0f}"
                
                long_params = opt_info.get("long_params", {})
                short_params = opt_info.get("short_params", {})
                lv = long_params.get('entry_threshold', '-')
                sv = short_params.get('entry_threshold', '-')
                if isinstance(lv, float): lv = round(lv, 2)
                if isinstance(sv, float): sv = round(sv, 2)
                opt_thr = f"{lv} / {sv}"

                lines.append(
                    f"| {idx} | {experiment_col} | {long_model} | {short_model} | {base_trades} | {opt_trades} | {ho_trades} | "
                    f"{base_pf} | {opt_pf} | {base_pnl} | {opt_pnl} | {ho_pnl} | {opt_thr} | "
                    f"#{trial_num}/{n_t} |"
                )
            elif opt.get("status") == "FAILED":
                lines.append(
                    f"| {idx} | {experiment_col} | {long_model} | {short_model} | - | FAILED | - | - | - | - | - | - | - | - |"
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
                lines.append("| Experiment | Trades (pre) T/L/S | Trades (opt) T/L/S | PF (pre) | PF (opt) | PnL (pre) | PnL (opt) | PnL (holdout) | Opt Thr | Opt TP | Opt SL | Opt Trail | Opt Cool | Opt Hold | Opt Consec | Best Trial |")
                lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")

                for exp in progress.get("experiments", []):
                    if exp.get("status") != "COMPLETED":
                        continue
                    label = exp["label"]
                    local_dir = exp.get("local_dir", os.path.join(batch_dir, exp.get("gcs_prefix", "")))

                    # Look up optimized result first
                    opt_key = f"{label}|{direction_key}|{metric}"
                    opt = all_results.get(opt_key, {})

                    if opt.get("status") == "OK":
                        om = opt.get("metrics", {})
                        if "optuna_info" in opt:
                            opt_info = opt["optuna_info"]
                        else:
                            opt_info = opt.get("config", {}).get("optuna_info", {})

                        # Use the optimizer's internal baseline to ensure apples-to-apples comparison
                        baseline = opt_info.get("baseline_metrics", {})
                        base_t = baseline.get("trade_count", 0)
                        base_l = baseline.get("buy_trades", 0)
                        base_s = baseline.get("sell_trades", 0)
                        base_trades = f"{base_t}/{base_l}/{base_s}"
                        base_pf = baseline.get("profit_factor", 0.0)
                        base_pnl = baseline.get("total_pnl", 0.0)

                        params = opt_info.get("params", {})
                        if not params and "all_trial_params" in opt_info:
                            suffix = f"_{direction_key}"
                            params = {
                                k.replace(suffix, ""): v
                                for k, v in opt_info["all_trial_params"].items()
                                if k.endswith(suffix)
                            }
                        opt_t = om.get("trade_count", 0)
                        opt_l = om.get("buy_trades", 0)
                        opt_s = om.get("sell_trades", 0)
                        opt_trades = f"{opt_t}/{opt_l}/{opt_s}"
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
                        # Fallback to pipeline_summary.json if optimization didn't complete
                        summary_path = os.path.join(local_dir, "pipeline_summary.json")
                        if os.path.exists(summary_path):
                            with open(summary_path, encoding="utf-8-sig") as f:
                                summary = json.load(f)
                            bt = summary.get("backtest_results", {})
                            base_key = f"{direction_key}_{metric}"
                            base = bt.get(base_key, {})
                            base_t = base.get("trade_count", 0)
                            base_l = base.get("buy_trades", 0)
                            base_s = base.get("sell_trades", 0)
                            base_trades = f"{base_t}/{base_l}/{base_s}"
                            base_pf = base.get("profit_factor", 0.0)
                            base_pnl = base.get("total_pnl", 0.0)
                        else:
                            base_trades = "-/-/-"
                            base_pf = 0.0
                            base_pnl = 0.0
                            
                        reason = opt.get("error", "not run") if opt else "not run"
                        lines.append(
                            f"| {label} | {base_trades} | - | "
                            f"{base_pf:.2f} | - | "
                            f"${base_pnl:,.0f} | - | - | - | - | - | - | - | - | - | - |"
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
        is_ensemble = '|' in key and len(key.split('|')) == 2
        if not params and "all_trial_params" in opt_info and not is_ensemble:
            direction_key = key.split('|')[1] if len(key.split('|')) > 1 else ""
            if direction_key:
                suffix = f"_{direction_key}"
                params = {
                    k.replace(suffix, ""): v
                    for k, v in opt_info["all_trial_params"].items()
                    if k.endswith(suffix)
                }
        metrics = result.get("metrics", {})
        baseline = opt_info.get("baseline_metrics", {})
        ho_metrics = opt_info.get("holdout_metrics", {})

        # Build a readable heading with experiment label if available
        labels_info = exp_labels.get(key, {})
        long_exp = labels_info.get("long_label", "")
        short_exp = labels_info.get("short_label", "")
        if long_exp or short_exp:
            exp_tag = f"{long_exp or '?'} / {short_exp or '?'}"
            parts = key.split('|')
            if len(parts) == 2:
                long_side = shorten_model_side(parts[0])
                short_side = shorten_model_side(parts[1])
                heading = f"{exp_tag} | {long_side} + {short_side}"
            else:
                heading = f"{exp_tag} | {key}"
        else:
            heading = key
        lines.append(f"### {heading}")
        lines.append("")
        
        b_buy = baseline.get("buy_trades", "-")
        b_sell = baseline.get("sell_trades", "-")
        b_tot = baseline.get("trade_count", "-")
        
        m_buy = metrics.get("buy_trades", "-")
        m_sell = metrics.get("sell_trades", "-")
        m_tot = metrics.get("trade_count", "-")
        
        h_buy = ho_metrics.get("buy_trades", "-") if ho_metrics else "-"
        h_sell = ho_metrics.get("sell_trades", "-") if ho_metrics else "-"
        h_tot = ho_metrics.get("trade_count", "-") if ho_metrics else "-"

        lines.append(f"**Trade Breakdown (Long / Short)**:")
        lines.append(f"- **Pre-Optimization**: {b_buy} / {b_sell} (Total: {b_tot})")
        lines.append(f"- **Post-Optimization**: {m_buy} / {m_sell} (Total: {m_tot})")
        lines.append(f"- **Holdout**: {h_buy} / {h_sell} (Total: {h_tot})")
        lines.append("")

        lines.append("| Parameter | Baseline | Optimized |")
        lines.append("|---|---|---|")
        lines.append(f"| Trades | {baseline.get('trade_count', '-')} | {metrics.get('trade_count', '-')} |")
        lines.append(f"| Profit Factor | {baseline.get('profit_factor', '-')} | {metrics.get('profit_factor', '-')} |")
        lines.append(f"| Win Rate | {baseline.get('win_rate', '-')} | {metrics.get('win_rate', '-')} |")
        lines.append(f"| PnL | ${baseline.get('total_pnl', 0):,.2f} | ${metrics.get('total_pnl', 0):,.2f} |")
        lines.append(f"| Max Drawdown | ${baseline.get('max_drawdown', 0):,.2f} | ${metrics.get('max_drawdown', 0):,.2f} |")

        # Ensemble mode stores suffixed param keys (entry_threshold_long, etc.) in params,
        # but the un-suffixed keys are in long_params/short_params.
        long_params = opt_info.get("long_params", {})
        short_params = opt_info.get("short_params", {})

        if is_ensemble and (long_params or short_params):
            # Show per-side params as "L / S" values
            param_keys = [
                ("Threshold", "entry_threshold"),
                ("TP ATR Mult", "tp_atr_mult"),
                ("SL ATR Mult", "sl_atr_mult"),
                ("Trailing ATR", "trailing_atr_mult"),
                ("Cooldown Bars", "cooldown_bars"),
                ("Max Hold Bars", "max_hold_bars"),
                ("Consec Signal", "consecutive_signal_threshold"),
                ("ATR Period", "atr_period"),
            ]
            for display_name, param_key in param_keys:
                lv = long_params.get(param_key, '-')
                sv = short_params.get(param_key, '-')
                lines.append(f"| {display_name} | - | {lv} / {sv} |")
        else:
            lines.append(f"| Threshold | - | {params.get('entry_threshold', '-')} |")
            lines.append(f"| TP ATR Mult | - | {params.get('tp_atr_mult', '-')} |")
            lines.append(f"| SL ATR Mult | - | {params.get('sl_atr_mult', '-')} |")
            lines.append(f"| Trailing ATR | - | {params.get('trailing_atr_mult', '-')} |")
            lines.append(f"| Cooldown Bars | - | {params.get('cooldown_bars', '-')} |")
            lines.append(f"| Max Hold Bars | - | {params.get('max_hold_bars', '-')} |")
            lines.append(f"| Consec Signal | - | {params.get('consecutive_signal_threshold', '-')} |")

        trial_num = opt_info.get('trial_number', '-')
        lines.append(f"| Best Trial | - | #{trial_num}/{opt_info.get('n_trials', '-')} |")
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


def _finalize_objective_results(
    objective_metric: str,
    all_results: dict,
    args,
    ohlcv_path: str,
    batch_dir: str,
    progress: dict,
    obj_elapsed: float,
    task_experiment_labels: dict | None = None,
):
    """Generate report and save results JSON for a single objective."""
    ok_count = sum(1 for v in all_results.values() if v.get('status') == 'OK')
    fail_count = sum(1 for v in all_results.values() if v.get('status') == 'FAILED')

    prefix = "ensembles_" if args.target_pairs_json else ""
    report_name = f"batch_summary_optimized_{prefix}{objective_metric}.md"

    send_telegram(
        f"[Batch Post-Optimizer] COMPLETE ({objective_metric})\n"
        f"Batch: {os.path.basename(batch_dir)}\n"
        f"Results: {ok_count} OK, {fail_count} failed\n"
        f"Wall time: {obj_elapsed/60:.1f} min\n"
        f"Report: {report_name}"
    )

    # Generate report
    report = generate_optimized_report(
        batch_dir, progress, all_results, ohlcv_path,
        wall_time_seconds=obj_elapsed,
        n_trials=args.n_trials,
        n_workers=args.workers,
        objective_metric=objective_metric,
        task_experiment_labels=task_experiment_labels,
    )
    report_path = os.path.join(batch_dir, report_name)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\nOptimized report saved: {report_path}")

    # Save raw results JSON
    results_json_path = os.path.join(batch_dir, f"optimization_results_{prefix}{objective_metric}.json")
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


def _run_all_objectives_concurrent(
    objectives: list[str],
    opt_tasks: list,
    args,
    ohlcv_path: str,
    batch_dir: str,
    progress: dict,
    n_workers: int,
    total_start: float,
    task_experiment_labels: dict | None = None,
):
    """Run all optimization tasks for all objectives concurrently in a single pool.

    Tasks for each objective are dispatched together into one ProcessPoolExecutor.
    Results are bucketed by objective for separate report generation.
    """
    obj_start = time.perf_counter()

    # Expand tasks: each (task, objective) pair becomes a dispatchable unit
    # Format: (task_key, ens_config, merged_path, label, metric, side, objective_metric)
    expanded_tasks = []
    for obj in objectives:
        for task in opt_tasks:
            expanded_tasks.append((*task, obj))

    total_task_count = len(expanded_tasks)

    print(f"\n{'='*60}")
    print(f"RUNNING {total_task_count} OPTIMIZATIONS — {', '.join(o.upper() for o in objectives)} CONCURRENT (workers={n_workers})")
    print(f"{'='*60}")

    # Telegram progress tracking (unified across all objectives)
    _completed_count = 0
    _last_tg_time = time.perf_counter()
    _TG_INTERVAL_SECS = 30 * 60
    _milestone_pcts = {25, 50, 75, 100}
    _milestones_sent = set()

    def _maybe_send_progress(completed, total, label, status, obj_metric):
        nonlocal _last_tg_time, _milestones_sent
        now = time.perf_counter()
        pct = int(100 * completed / total) if total > 0 else 0
        elapsed_min = (now - total_start) / 60
        is_milestone = pct in _milestone_pcts and pct not in _milestones_sent
        is_time_update = (now - _last_tg_time) >= _TG_INTERVAL_SECS
        if is_milestone or is_time_update:
            if is_milestone:
                _milestones_sent.add(pct)
            _last_tg_time = now
            obj_label = "/".join(o.upper() for o in objectives)
            send_telegram(
                f"[Batch Post-Optimizer] Progress ({obj_label})\n"
                f"{completed}/{total} optimizations done ({pct}%)\n"
                f"Latest: [{obj_metric}] {label} -> {status}\n"
                f"Elapsed: {elapsed_min:.0f} min"
            )

    obj_label = "/".join(o.upper() for o in objectives)
    send_telegram(
        f"[Batch Post-Optimizer] STARTING ({obj_label})\n"
        f"Batch: {os.path.basename(batch_dir)}\n"
        f"{total_task_count} optimizations ({len(opt_tasks)}/objective × {len(objectives)} objectives), {n_workers} workers\n"
        f"{args.n_trials} trials/optimization, {args.holdout_months}mo holdout"
    )

    # Results bucketed by objective: {"sharpe": {task_key: result, ...}, "sortino": {...}}
    results_by_objective = {obj: {} for obj in objectives}

    if n_workers > 1:
        futures = {}
        with ProcessPoolExecutor(max_workers=n_workers) as pool:
            for task_key, ens_config, merged_path, label, metric, side, obj_metric in expanded_tasks:
                future = pool.submit(
                    run_single_optimization,
                    config_path=ens_config,
                    predictions_path=merged_path,
                    ohlcv_path=ohlcv_path,
                    n_trials=args.n_trials,
                    min_trades=args.min_trades,
                    label=f"{label} {(side or 'ensemble').upper()} {metric}",
                    holdout_months=args.holdout_months,
                    n_jobs=args.jobs,
                    quiet=True,
                    objective_metric=obj_metric,
                    optimize_side=side,
                    exec_ohlcv_path=args.exec_data,
                    slippage_per_side=args.slippage_per_side,
                )
                futures[future] = (task_key, merged_path, label, metric, side, obj_metric)

            for future in as_completed(futures):
                task_key, merged_path, label, metric, side, obj_metric = futures[future]
                try:
                    result = future.result()
                except Exception as e:
                    print(f"  ERROR in {task_key} ({obj_metric}): {e}")
                    result = {"status": "FAILED", "error": str(e)}
                results_by_objective[obj_metric][task_key] = result
                _completed_count += 1
                _maybe_send_progress(
                    _completed_count, total_task_count,
                    f"{label} {side or 'ensemble'} {metric}",
                    result.get('status', '?'), obj_metric,
                )
    else:
        for task_key, ens_config, merged_path, label, metric, side, obj_metric in expanded_tasks:
            result = run_single_optimization(
                config_path=ens_config,
                predictions_path=merged_path,
                ohlcv_path=ohlcv_path,
                n_trials=args.n_trials,
                min_trades=args.min_trades,
                label=f"{label} {(side or 'ensemble').upper()} {metric}",
                holdout_months=args.holdout_months,
                n_jobs=args.jobs,
                quiet=False,
                objective_metric=obj_metric,
                optimize_side=side,
                exec_ohlcv_path=args.exec_data,
                slippage_per_side=args.slippage_per_side,
            )
            results_by_objective[obj_metric][task_key] = result
            _completed_count += 1
            _maybe_send_progress(
                _completed_count, total_task_count,
                f"{label} {side or 'ensemble'} {metric}",
                result.get('status', '?'), obj_metric,
            )

    obj_elapsed = time.perf_counter() - obj_start

    # Generate per-objective reports and results JSONs
    for obj in objectives:
        _finalize_objective_results(
            objective_metric=obj,
            all_results=results_by_objective[obj],
            args=args,
            ohlcv_path=ohlcv_path,
            batch_dir=batch_dir,
            progress=progress,
            obj_elapsed=obj_elapsed,
            task_experiment_labels=task_experiment_labels,
        )

    return obj_elapsed


def main():
    parser = argparse.ArgumentParser(description="Batch Post-Optimizer")
    parser.add_argument("--batch-dir", required=True, help="Path to batch directory")
    parser.add_argument("--target-pairs-json", type=str, default=None, help="JSON file with top N pairs to optimize")
    parser.add_argument("--n-trials", type=int, default=500, help="Optuna trials per optimization")
    parser.add_argument("--min-trades", type=int, default=10, help="Min trades for valid trial")
    parser.add_argument("--exec-data", default=None, help="Optional: path to raw unadjusted execution data")
    parser.add_argument("--slippage-per-side", type=float, default=None, help="Slippage to apply per side in points")
    parser.add_argument(
        "--holdout-months", type=int, default=4,
        help="Reserve last N months of predictions as unseen holdout (default: 4)"
    )
    parser.add_argument(
        "--no-filter", action="store_true",
        help="Disable min_sharpe hurdle filter to keep best trial even if unprofitable"
    )
    parser.add_argument(
        "--jobs", type=int, default=1,
        help="Number of parallel Optuna trial evaluations (default: 1)"
    )
    parser.add_argument(
        "--workers", type=int, default=0,
        help="Number of parallel experiment optimizations (default: 0 = auto, matches task count)"
    )
    parser.add_argument(
        "--mem-per-worker-gb", type=float, default=1.5,
        help="Estimated memory per worker in GB for auto-capping (default: 1.5)"
    )
    parser.add_argument(
        "--objective", choices=["sharpe", "sortino", "both"], default="sharpe",
        help="Objective function: sharpe (default), sortino, or both (runs sequentially)"
    )
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
    if manifest_path == "manifest.json":
        manifest_path = os.path.join(batch_dir, manifest_path)
    elif not os.path.isabs(manifest_path):
        manifest_path = os.path.join(PROJECT_ROOT, manifest_path)
    ohlcv_path = find_ohlcv_path(manifest_path)
    print(f"OHLCV data: {ohlcv_path}")

    # Patch strategy_optimizer min_sharpe threshold if --no-filter
    import agent.strategy_optimizer as so

    if args.no_filter:
        original_save_best = so.TopKTracker.save_best
        def patched_save_best(self, min_sharpe=-99999.0):
            return original_save_best(self, min_sharpe=-99999.0)
        so.TopKTracker.save_best = patched_save_best

    if args.exec_data:
        ohlcv_df, _ = load_ohlcv_dual(ohlcv_path)
        _, ohlcv_exec_df = load_ohlcv_dual(args.exec_data)
    else:
        ohlcv_df, ohlcv_exec_df = load_ohlcv_dual(ohlcv_path)
    
    total_start = time.perf_counter()

    # Build list of PER-SIDE optimization tasks
    # Each task: (task_key, config_path, merged_path, label, metric, side)
    opt_tasks = []
    # Map task_key -> {"long_label": ..., "short_label": ...} for experiment labelling
    task_experiment_labels = {}
    
    if args.target_pairs_json:
        if not os.path.exists(args.target_pairs_json):
            print(f"ERROR: Provided --target-pairs-json file does not exist: {args.target_pairs_json}")
            sys.exit(1)
        
        print(f"Loading top target pairs from {args.target_pairs_json}")
        with open(args.target_pairs_json, "r") as f:
            top_pairs = json.load(f)
            
        base_config_name = progress.get("defaults", {}).get("strategy_config", "hourly_ensemble_010.json")
        base_config_path = os.path.join(PROJECT_ROOT, "configs", "strategies", base_config_name)
        if not os.path.exists(base_config_path):
            print(f"ERROR: base config not found: {base_config_path}")
            sys.exit(1)
        
        # Build gcs_prefix -> experiment label mapping from batch_progress.json
        gcs_to_label = _build_gcs_prefix_to_label(progress)
            
        # Map model directory names to their oos_predictions.csv paths
        # sweep_ensembles.py uses directory names like "registry_E2E_HourSet_10_long_logloss"
        # and the predictions are at <dir>/oos_predictions.csv
        pred_map = {}
        
        # Build list of directories to search: batch_dir + all experiment local_dirs
        search_dirs = [batch_dir]
        for exp in progress.get("experiments", []):
            local_dir = exp.get("local_dir", "")
            if local_dir and os.path.isdir(local_dir) and local_dir not in search_dirs:
                search_dirs.append(local_dir)
        
        for search_dir in search_dirs:
            for root, dirs, files in os.walk(search_dir):
                for file in files:
                    if file == "oos_predictions.csv":
                        # Key matches sweep_ensembles.py._unique_model_name()
                        # e.g. "sweep_hs10_3x1_6h_20260609_0026_E2E_HourSet_10_long_logloss"
                        key = _unique_model_name(root)
                        pred_map[key] = os.path.join(root, file)
                    # Also support flat CSV naming: oos_predictions_<prefix>.csv
                    elif file.startswith("oos_predictions_") and file.endswith(".csv"):
                        stem = file.replace(".csv", "")
                        pred_map[stem] = os.path.join(root, file)
        
        print(f"  Discovered {len(pred_map)} prediction paths across {len(search_dirs)} directories")
                    
        for i, pair in enumerate(top_pairs):
            long_prefix = pair["target_long"]
            short_prefix = pair["target_short"]
            
            # Try exact match first, then legacy fallback with "registry_" prefix stripped
            long_pred_path = pred_map.get(long_prefix) or pred_map.get(long_prefix.replace("registry_", "", 1))
            short_pred_path = pred_map.get(short_prefix) or pred_map.get(short_prefix.replace("registry_", "", 1))
            
            if not long_pred_path or not short_pred_path:
                print(f"Skipping pair: missing pred path for {long_prefix} or {short_prefix}")
                if not long_pred_path:
                    print(f"  -> No match for long '{long_prefix}' in {len(pred_map)} discovered paths")
                if not short_pred_path:
                    print(f"  -> No match for short '{short_prefix}' in {len(pred_map)} discovered paths")
                continue
            
            # Resolve experiment labels from prediction paths
            long_exp_label = _label_from_pred_path(long_pred_path, gcs_to_label)
            short_exp_label = _label_from_pred_path(short_pred_path, gcs_to_label)
                
            merged_filename = f"_merged_ens_{long_prefix}_vs_{short_prefix}.csv"
            canary_dir = os.path.dirname(long_pred_path)
            merged_path = os.path.join(canary_dir, merged_filename)
            
            if not os.path.exists(merged_path):
                merged_df = merge_predictions(long_pred_path, short_pred_path)
                merged_df.to_csv(merged_path)
                
            label = f"Ensemble_{i+1}"
            task_key = f"{long_prefix}|{short_prefix}"
            
            # Store experiment labels for this task_key
            task_experiment_labels[task_key] = {
                "long_label": long_exp_label,
                "short_label": short_exp_label,
            }
            
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
                    opt_tasks.append((task_key, ens_config, merged_path, label, metric, side))

    # Auto-detect worker count: 0 = match task count (max parallelism)
    if len(opt_tasks) == 0:
        print("CRITICAL: No optimization tasks generated! This usually indicates 0 trades in backtest or missing artifacts.")
        sys.exit(1)

    # Determine which objectives to run
    objectives = ["sharpe", "sortino"] if args.objective == "both" else [args.objective]

    # Total tasks = per-objective tasks × number of objectives (all run concurrently)
    total_tasks = len(opt_tasks) * len(objectives)

    if args.workers <= 0:
        n_workers = total_tasks
        print(f"  Auto workers: {n_workers} (one per task, {len(opt_tasks)} tasks × {len(objectives)} objectives)")
    else:
        n_workers = min(args.workers, total_tasks)

    # Memory-based safety cap — prevents OOM regardless of requested workers
    try:
        import os as _os
        mem_gb = _os.sysconf('SC_PAGE_SIZE') * _os.sysconf('SC_PHYS_PAGES') / (1024**3)
        mem_safe_workers = max(1, int(mem_gb / args.mem_per_worker_gb))
        if n_workers > mem_safe_workers:
            print(f"  Memory cap: {n_workers} workers -> {mem_safe_workers} "
                  f"(detected {mem_gb:.0f} GB RAM, {args.mem_per_worker_gb:.1f} GB/worker budget)")
            n_workers = mem_safe_workers
    except (ValueError, AttributeError, OSError):
        pass  # Windows or unavailable — skip memory check

    # Run all objectives concurrently in a single process pool
    _run_all_objectives_concurrent(
        objectives=objectives,
        opt_tasks=opt_tasks,
        args=args,
        ohlcv_path=ohlcv_path,
        batch_dir=batch_dir,
        progress=progress,
        n_workers=n_workers,
        total_start=total_start,
        task_experiment_labels=task_experiment_labels,
    )

    total_elapsed = time.perf_counter() - total_start
    print(f"\n{'='*60}")
    print(f"ALL OPTIMIZATIONS COMPLETE - {total_elapsed:.0f}s ({total_elapsed/60:.1f} min)")
    print(f"Objectives: {', '.join(objectives)}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()

