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
from agent.backtest_engine import BacktestEngine, load_ohlcv, load_predictions
from agent.strategy_optimizer import run_optimization, extract_metrics, send_telegram, suppress_telegram


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
    holdout_months: int = 0,
    n_jobs: int = 1,
    quiet: bool = False,
    objective_metric: str = "sharpe",
    optimize_side: str | None = None,
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
                local_dir = exp["local_dir"]

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


def _run_for_objective(
    objective_metric: str,
    opt_tasks: list,
    args,
    ohlcv_path: str,
    batch_dir: str,
    progress: dict,
    n_workers: int,
    total_start: float,
):
    """Run all optimization tasks for a single objective (sharpe or sortino).

    Returns the wall time for this objective's run.
    """
    obj_start = time.perf_counter()
    all_results = {}

    print(f"\n{'='*60}")
    print(f"RUNNING {len(opt_tasks)} OPTIMIZATIONS — {objective_metric.upper()} (workers={n_workers})")
    print(f"{'='*60}")

    # Telegram batch-level progress tracking
    _total_tasks = len(opt_tasks)
    _completed_count = 0
    _last_tg_time = time.perf_counter()
    _TG_INTERVAL_SECS = 30 * 60
    _milestone_pcts = {25, 50, 75, 100}
    _milestones_sent = set()

    def _maybe_send_progress(completed, total, label, status):
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
            send_telegram(
                f"[Batch Post-Optimizer] Progress ({objective_metric})\n"
                f"{completed}/{total} optimizations done ({pct}%)\n"
                f"Latest: {label} -> {status}\n"
                f"Elapsed: {elapsed_min:.0f} min"
            )

    send_telegram(
        f"[Batch Post-Optimizer] STARTING ({objective_metric})\n"
        f"Batch: {os.path.basename(batch_dir)}\n"
        f"{_total_tasks} optimizations, {n_workers} workers\n"
        f"{args.n_trials} trials/optimization, {args.holdout_months}mo holdout"
    )

    if n_workers > 1:
        futures = {}
        with ProcessPoolExecutor(max_workers=n_workers) as pool:
            for task_key, ens_config, merged_path, label, metric, side in opt_tasks:
                future = pool.submit(
                    run_single_optimization,
                    config_path=ens_config,
                    predictions_path=merged_path,
                    ohlcv_path=ohlcv_path,
                    n_trials=args.n_trials,
                    min_trades=args.min_trades,
                    label=f"{label} {side.upper()} {metric}",
                    holdout_months=args.holdout_months,
                    n_jobs=args.jobs,
                    quiet=True,
                    objective_metric=objective_metric,
                    optimize_side=side,
                )
                futures[future] = (task_key, merged_path, label, metric, side)

            for future in as_completed(futures):
                task_key, merged_path, label, metric, side = futures[future]
                try:
                    result = future.result()
                except Exception as e:
                    print(f"  ERROR in {task_key}: {e}")
                    result = {"status": "FAILED", "error": str(e)}
                all_results[task_key] = result
                _completed_count += 1
                _maybe_send_progress(_completed_count, _total_tasks, f"{label} {side} {metric}", result.get('status', '?'))
    else:
        for task_key, ens_config, merged_path, label, metric, side in opt_tasks:
            result = run_single_optimization(
                config_path=ens_config,
                predictions_path=merged_path,
                ohlcv_path=ohlcv_path,
                n_trials=args.n_trials,
                min_trades=args.min_trades,
                label=f"{label} {side.upper()} {metric}",
                holdout_months=args.holdout_months,
                n_jobs=args.jobs,
                quiet=(args.workers > 1),
                objective_metric=objective_metric,
                optimize_side=side,
            )
            all_results[task_key] = result
            _completed_count += 1
            _maybe_send_progress(_completed_count, _total_tasks, f"{label} {side} {metric}", result.get('status', '?'))

    obj_elapsed = time.perf_counter() - obj_start

    # Final batch-level summary
    ok_count = sum(1 for v in all_results.values() if v.get('status') == 'OK')
    fail_count = sum(1 for v in all_results.values() if v.get('status') == 'FAILED')
    report_name = f"batch_summary_optimized_{objective_metric}.md"
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
    )
    report_path = os.path.join(batch_dir, report_name)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\nOptimized report saved: {report_path}")

    # Save raw results JSON
    results_json_path = os.path.join(batch_dir, f"optimization_results_{objective_metric}.json")
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

    return obj_elapsed


def main():
    parser = argparse.ArgumentParser(description="Batch Post-Optimizer")
    parser.add_argument("--batch-dir", required=True, help="Path to batch directory")
    parser.add_argument("--n-trials", type=int, default=500, help="Optuna trials per optimization")
    parser.add_argument("--min-trades", type=int, default=10, help="Min trades for valid trial")
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

    ohlcv_df = load_ohlcv(ohlcv_path)
    total_start = time.perf_counter()

    # Build list of PER-SIDE optimization tasks
    # Each task: (task_key, config_path, merged_path, label, metric, side)
    opt_tasks = []
    for exp in progress.get("experiments", []):
        if exp.get("status") != "COMPLETED":
            continue

        label = exp["label"]
        local_dir = exp["local_dir"]
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
    if args.workers <= 0:
        n_workers = len(opt_tasks) if opt_tasks else 1
        print(f"  Auto workers: {n_workers} (one per task)")
    else:
        n_workers = min(args.workers, len(opt_tasks)) if opt_tasks else 1

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

    # Determine which objectives to run
    objectives = ["sharpe", "sortino"] if args.objective == "both" else [args.objective]

    for obj in objectives:
        _run_for_objective(
            objective_metric=obj,
            opt_tasks=opt_tasks,
            args=args,
            ohlcv_path=ohlcv_path,
            batch_dir=batch_dir,
            progress=progress,
            n_workers=n_workers,
            total_start=total_start,
        )

    total_elapsed = time.perf_counter() - total_start
    print(f"\n{'='*60}")
    print(f"ALL OPTIMIZATIONS COMPLETE - {total_elapsed:.0f}s ({total_elapsed/60:.1f} min)")
    print(f"Objectives: {', '.join(objectives)}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()

