"""
A/B Canary Test — Hybrid Pipeline (vectorbt + Optuna) vs Legacy Pure-Optuna.

Selects 2 targets (1 long + 1 short where possible) from a completed batch
directory and runs both pipelines sequentially, then generates a side-by-side
Markdown comparison report.

Usage:
    python agent/run_vbt_ab_test.py \\
        --batch-dir reports/batch_runs/batch_20260511_2116 \\
        [--n-trials-a 500] \\
        [--n-trials-b 150] \\
        [--vbt-top-n 20] \\
        [--holdout-months 4] \\
        [--objective sharpe] \\
        [--output-dir reports/ab_tests]

Pipelines:
    A (Control) — run_optimization()         legacy pure-Optuna, 500 trials
    B (Test)    — run_hybrid_optimization()  Stage 1 vbt + Stage 2 Optuna, 150 trials
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from statistics import mean

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

import pandas as pd

from agent.strategy_optimizer import (
    run_optimization,
    run_hybrid_optimization,
    extract_metrics,
    suppress_telegram,
)
from agent.backtest_engine import load_ohlcv, load_predictions
from agent.batch_post_optimizer import find_ohlcv_path, merge_predictions


# ---------------------------------------------------------------------------
# Target Discovery
# ---------------------------------------------------------------------------


def _find_prediction_files(canary_dir: str, prefix: str, metric: str) -> tuple[str, str]:
    """Return (long_pred_path, short_pred_path) using new/legacy naming."""
    long_new = os.path.join(canary_dir, f"oos_predictions_{prefix}_long_{metric}.csv")
    long_old = os.path.join(canary_dir, f"oos_predictions_long_{metric}.csv")
    short_new = os.path.join(canary_dir, f"oos_predictions_{prefix}_short_{metric}.csv")
    short_old = os.path.join(canary_dir, f"oos_predictions_short_{metric}.csv")

    long_pred = long_new if os.path.exists(long_new) else long_old
    short_pred = short_new if os.path.exists(short_new) else short_old
    return long_pred, short_pred


def _find_ensemble_config(canary_dir: str, prefix: str, metric: str) -> str:
    """Return ensemble config path using new/legacy naming."""
    new = os.path.join(canary_dir, f"{prefix}_{metric}.json")
    old = os.path.join(canary_dir, f"ensemble_config_{metric}.json")
    return new if os.path.exists(new) else old


def select_targets(
    progress: dict,
    batch_dir: str,
    metric: str = "logloss",
) -> list[dict]:
    """Auto-select 2 targets (prefer 1 long + 1 short) from the batch.

    Scans COMPLETED experiments in the order they appear in batch_progress.json.
    Returns a list of target dicts with keys:
        label, config_path, merged_path, side, metric, experiment_label
    """
    candidates_long: list[dict] = []
    candidates_short: list[dict] = []

    for exp in progress.get("experiments", []):
        if exp.get("status") != "COMPLETED":
            continue

        label = exp["label"]
        local_dir = exp["local_dir"]
        canary_dir = os.path.join(local_dir, "registry", "canary_output")
        prefix = exp.get("gcs_prefix", "")

        long_pred, short_pred = _find_prediction_files(canary_dir, prefix, metric)
        ens_config = _find_ensemble_config(canary_dir, prefix, metric)

        if not os.path.exists(ens_config):
            continue

        merged_path = os.path.join(canary_dir, f"_abtest_merged_{metric}.csv")
        if not os.path.exists(merged_path):
            if not os.path.exists(long_pred) or not os.path.exists(short_pred):
                continue
            merged_df = merge_predictions(long_pred, short_pred)
            merged_df.to_csv(merged_path)

        target_base = {
            "config_path": ens_config,
            "merged_path": merged_path,
            "metric": metric,
            "experiment_label": label,
        }

        if os.path.exists(long_pred) and not candidates_long:
            candidates_long.append({**target_base, "label": f"{label}_long_{metric}", "side": "long"})
        if os.path.exists(short_pred) and not candidates_short:
            candidates_short.append({**target_base, "label": f"{label}_short_{metric}", "side": "short"})

        if candidates_long and candidates_short:
            break  # Found 1 long + 1 short — done

    # Merge: prefer 1L+1S; fall back to 2 from the same side
    selected: list[dict] = []
    if candidates_long:
        selected.append(candidates_long[0])
    if candidates_short:
        selected.append(candidates_short[0])

    # If still only 1 target, try to find a 2nd from any side/experiment
    if len(selected) < 2:
        for exp in progress.get("experiments", []):
            if exp.get("status") != "COMPLETED":
                continue
            label = exp["label"]
            local_dir = exp["local_dir"]
            canary_dir = os.path.join(local_dir, "registry", "canary_output")
            prefix = exp.get("gcs_prefix", "")

            long_pred, short_pred = _find_prediction_files(canary_dir, prefix, metric)
            ens_config = _find_ensemble_config(canary_dir, prefix, metric)

            if not os.path.exists(ens_config):
                continue

            for side_key, pred_path in [("long", long_pred), ("short", short_pred)]:
                candidate = {
                    "label": f"{label}_{side_key}_{metric}",
                    "config_path": ens_config,
                    "merged_path": os.path.join(
                        local_dir, "registry", "canary_output", f"_abtest_merged_{metric}.csv"
                    ),
                    "side": side_key,
                    "metric": metric,
                    "experiment_label": label,
                }
                if os.path.exists(pred_path) and candidate not in selected:
                    selected.append(candidate)
                if len(selected) >= 2:
                    break
            if len(selected) >= 2:
                break

    return selected[:2]


# ---------------------------------------------------------------------------
# Single pipeline runner
# ---------------------------------------------------------------------------


def run_single_ab_target(
    pipeline: str,
    config_path: str,
    merged_path: str,
    ohlcv_path: str,
    side: str,
    n_trials: int,
    holdout_months: int,
    objective_metric: str,
    vbt_top_n: int = 20,
    label: str = "",
) -> dict:
    """Run one pipeline on one target and return a results dict.

    Args:
        pipeline: "A" (legacy) or "B" (hybrid).

    Returns:
        Dict with keys: pipeline, wall_time_s, train_pnl, train_sharpe,
        train_trades, train_wr, train_pf, holdout_pnl, params, error.
    """
    start = time.perf_counter()
    try:
        kwargs = dict(
            config_path=config_path,
            predictions_path=merged_path,
            ohlcv_path=ohlcv_path,
            holdout_months=holdout_months,
            objective_metric=objective_metric,
            optimize_side=side,
            quiet=True,
            n_trials=n_trials,
            label=label,
        )

        if pipeline == "A":
            best_cfg, best_result = run_optimization(**kwargs)
        else:
            best_cfg, best_result = run_hybrid_optimization(**kwargs, vbt_top_n=vbt_top_n)

        elapsed = time.perf_counter() - start
        metrics = extract_metrics(best_result)
        holdout_m = best_cfg.get("optuna_info", {}).get("holdout_metrics", {})

        # Extract the side-level params dict (the optimizer strips suffixes when saving)
        opt_info = best_cfg.get("optuna_info", {})
        params = opt_info.get("params", {})

        return {
            "pipeline": pipeline,
            "wall_time_s": round(elapsed, 1),
            "train_pnl": metrics.get("total_pnl", 0.0),
            "train_sharpe": metrics.get("sharpe_ratio", 0.0),
            "train_trades": metrics.get("trade_count", 0),
            "train_wr": metrics.get("win_rate", 0.0),
            "train_pf": metrics.get("profit_factor", 0.0),
            "holdout_pnl": holdout_m.get("total_pnl", None),
            "params": params,
            "error": None,
        }

    except Exception as e:
        elapsed = time.perf_counter() - start
        print(f"  [ERROR] Pipeline {pipeline}: {e}")
        return {
            "pipeline": pipeline,
            "wall_time_s": round(elapsed, 1),
            "train_pnl": None,
            "train_sharpe": None,
            "train_trades": None,
            "train_wr": None,
            "train_pf": None,
            "holdout_pnl": None,
            "params": {},
            "error": str(e),
        }


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def _fmt_pnl(v) -> str:
    if v is None:
        return "ERR"
    return f"${v:,.2f}"


def _fmt_pct(v) -> str:
    if v is None:
        return "ERR"
    return f"{v:.1%}"


def _fmt_float(v, decimals=4) -> str:
    if v is None:
        return "ERR"
    return f"{v:.{decimals}f}"


def _fmt_delta(a, b, is_pnl=False) -> str:
    """Format delta (B - A), with sign prefix."""
    if a is None or b is None:
        return "N/A"
    delta = b - a
    if is_pnl:
        sign = "+" if delta >= 0 else ""
        return f"{sign}${delta:,.2f}"
    sign = "+" if delta >= 0 else ""
    return f"{sign}{delta:.4f}"


def _target_table(target: dict, res_a: dict, res_b: dict) -> str:
    """Generate the per-target comparison markdown table."""

    def param(res: dict, key: str) -> str:
        p = res.get("params", {})
        v = p.get(key)
        if v is None:
            # Try stripped key (optimizer saves without suffix)
            v = p.get(key.replace(f"_{target['side']}", ""))
        return str(v) if v is not None else "-"

    rows = [
        ("Wall Time (s)",        f"{res_a['wall_time_s']:.1f}",            f"{res_b['wall_time_s']:.1f}",
         _fmt_delta(res_a['wall_time_s'], res_b['wall_time_s'])),
        ("Wall Time (min)",      f"{res_a['wall_time_s']/60:.2f}",          f"{res_b['wall_time_s']/60:.2f}",
         _fmt_delta(res_a['wall_time_s']/60, res_b['wall_time_s']/60)),
        ("Train PnL",            _fmt_pnl(res_a['train_pnl']),              _fmt_pnl(res_b['train_pnl']),
         _fmt_delta(res_a['train_pnl'], res_b['train_pnl'], is_pnl=True)),
        ("Train Sharpe",         _fmt_float(res_a['train_sharpe']),         _fmt_float(res_b['train_sharpe']),
         _fmt_delta(res_a['train_sharpe'], res_b['train_sharpe'])),
        ("Holdout PnL",          _fmt_pnl(res_a['holdout_pnl']),            _fmt_pnl(res_b['holdout_pnl']),
         _fmt_delta(res_a['holdout_pnl'], res_b['holdout_pnl'], is_pnl=True)),
        ("Trade Count (train)",  str(res_a['train_trades']),                str(res_b['train_trades']),
         _fmt_delta(res_a['train_trades'] or 0, res_b['train_trades'] or 0)),
        ("Win Rate (train)",     _fmt_pct(res_a['train_wr']),               _fmt_pct(res_b['train_wr']), ""),
        ("Profit Factor (train)",_fmt_float(res_a['train_pf'], 3),          _fmt_float(res_b['train_pf'], 3), ""),
        ("Best entry_threshold", param(res_a, "entry_threshold"),           param(res_b, "entry_threshold"), ""),
        ("Best tp_atr_mult",     param(res_a, "tp_atr_mult"),               param(res_b, "tp_atr_mult"), ""),
        ("Best sl_atr_mult",     param(res_a, "sl_atr_mult"),               param(res_b, "sl_atr_mult"), ""),
        ("Best max_hold_bars",   param(res_a, "max_hold_bars"),             param(res_b, "max_hold_bars"), ""),
    ]

    if res_a.get("error"):
        rows.insert(0, ("ERROR (A)", res_a["error"], "", ""))
    if res_b.get("error"):
        rows.insert(0, ("ERROR (B)", "", res_b["error"], ""))

    header = "| Metric | Pipeline A (Control) | Pipeline B (Test) | Delta (B \u2212 A) |"
    sep    = "|---|---|---|---|"
    body   = "\n".join(f"| {r} | {a} | {b} | {d} |" for r, a, b, d in rows)
    return f"{header}\n{sep}\n{body}"


def generate_report(
    targets: list[dict],
    results_a: list[dict],
    results_b: list[dict],
    args: argparse.Namespace,
) -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    batch_name = os.path.basename(args.batch_dir)
    total_time_a = sum(r["wall_time_s"] for r in results_a)
    total_time_b = sum(r["wall_time_s"] for r in results_b)
    speedup = total_time_a / total_time_b if total_time_b > 0 else 0.0

    # Holdout delta (B avg - A avg), ignoring Nones
    ho_a = [r["holdout_pnl"] for r in results_a if r.get("holdout_pnl") is not None]
    ho_b = [r["holdout_pnl"] for r in results_b if r.get("holdout_pnl") is not None]
    avg_ho_a = mean(ho_a) if ho_a else None
    avg_ho_b = mean(ho_b) if ho_b else None
    avg_ho_delta = (avg_ho_b - avg_ho_a) if (avg_ho_a is not None and avg_ho_b is not None) else None

    # Verdict
    if avg_ho_delta is not None:
        if speedup >= 1.5 and abs(avg_ho_delta) < 500:
            verdict = "**ADOPT HYBRID** \u2705"
            verdict_detail = (
                f"Pipeline B was **{speedup:.2f}\u00d7 faster** and holdout PnL "
                f"delta was ${avg_ho_delta:+,.2f} (within \u00b1$500 tolerance)."
            )
        elif speedup >= 1.5:
            verdict = "**NEEDS REVIEW** \u26a0\ufe0f"
            verdict_detail = (
                f"Pipeline B was **{speedup:.2f}\u00d7 faster** but holdout PnL "
                f"delta was ${avg_ho_delta:+,.2f} (exceeds \u00b1$500 threshold). "
                "Investigate before promoting to production."
            )
        else:
            verdict = "**REVERT TO LEGACY** \u274c"
            verdict_detail = (
                f"Pipeline B achieved only **{speedup:.2f}\u00d7 speedup** "
                "(threshold: 1.5\u00d7). Hybrid Stage 1 did not provide sufficient "
                "warm-starting benefit for this batch."
            )
    else:
        verdict = "**INCONCLUSIVE** \u2753"
        verdict_detail = "Holdout metrics unavailable (0 holdout months or pipeline errors)."

    lines = [
        "# A/B Canary Test Report \u2014 Hybrid Pipeline vs Legacy Optuna",
        "",
        f"Generated: {ts}",
        f"Batch: `{batch_name}`",
        f"Objective: {args.objective}",
        f"Holdout: {args.holdout_months} months",
        "",
        "---",
        "",
        "## Pipeline Configuration",
        "",
        "| Setting | Pipeline A (Control) | Pipeline B (Test) |",
        "|---|---|---|",
        "| Method | Pure Optuna | vectorbt Stage 1 + Optuna Stage 2 |",
        f"| Stage 1 Trials | \u2014 | {args.vbt_top_n} (enqueued warm-start) |",
        f"| Stage 2 Trials | {args.n_trials_a} | {args.n_trials_b} |",
        f"| Total Effective Trials | {args.n_trials_a} | {args.vbt_top_n + args.n_trials_b} |",
        "",
        "---",
        "",
        "## Results by Target",
        "",
    ]

    for i, (target, res_a, res_b) in enumerate(zip(targets, results_a, results_b), 1):
        side_label = target["side"].upper()
        metric_label = target["metric"]
        exp_label = target["experiment_label"]
        lines.append(f"### Target {i}: {exp_label} \u2014 {side_label} ({metric_label})")
        lines.append("")
        lines.append(_target_table(target, res_a, res_b))
        lines.append("")

    lines += [
        "---",
        "",
        "## Aggregate Summary",
        "",
        "| Metric | Pipeline A | Pipeline B | Winner |",
        "|---|---|---|---|",
        f"| Total Wall Time (s) | {total_time_a:.1f} | {total_time_b:.1f} | "
        f"{'B \u2705' if total_time_b < total_time_a else 'A'} |",
        f"| Speedup Factor | \u2014 | {speedup:.2f}\u00d7 | "
        f"{'B \u2705' if speedup >= 1.5 else 'A \u26a0\ufe0f'} |",
        f"| Avg Holdout PnL | {_fmt_pnl(avg_ho_a)} | {_fmt_pnl(avg_ho_b)} | "
        f"{'B \u2705' if (avg_ho_delta or 0) > 0 else 'A' if (avg_ho_delta or 0) < 0 else 'Tie'} |",
        f"| Holdout PnL delta (B \u2212 A) | \u2014 | {_fmt_pnl(avg_ho_delta)} | |",
        "",
        "---",
        "",
        "## Verdict",
        "",
        f"### {verdict}",
        "",
        verdict_detail,
        "",
        "**Decision thresholds:**",
        "- ADOPT HYBRID: speedup \u2265 1.5\u00d7 AND |holdout PnL delta| < $500 per target avg",
        "- NEEDS REVIEW: speedup \u2265 1.5\u00d7 BUT |holdout PnL delta| \u2265 $500",
        "- REVERT TO LEGACY: speedup < 1.5\u00d7",
        "",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="A/B Canary Test: Hybrid Pipeline (vbt+Optuna) vs Legacy Optuna"
    )
    parser.add_argument("--batch-dir", required=True, help="Path to batch directory")
    parser.add_argument(
        "--n-trials-a", type=int, default=500,
        help="Pipeline A trial count (legacy Optuna, default: 500)",
    )
    parser.add_argument(
        "--n-trials-b", type=int, default=150,
        help="Pipeline B Stage 2 trial count (post warm-start, default: 150)",
    )
    parser.add_argument(
        "--vbt-top-n", type=int, default=20,
        help="Stage 1 top-N configs to inject into Optuna warm-start (default: 20)",
    )
    parser.add_argument(
        "--holdout-months", type=int, default=4,
        help="Holdout months (default: 4)",
    )
    parser.add_argument(
        "--objective", choices=["sharpe", "sortino"], default="sharpe",
        help="Optimization objective (default: sharpe)",
    )
    parser.add_argument(
        "--metric", default="logloss",
        help="Prediction metric to use for target selection (default: logloss)",
    )
    parser.add_argument(
        "--output-dir", default="reports/ab_tests",
        help="Directory for the output report (default: reports/ab_tests)",
    )
    parser.add_argument(
        "--ohlcv-path", default=None,
        help="Override: explicit path to local OHLCV parquet (skips find_ohlcv_path)",
    )
    args = parser.parse_args()

    # Suppress per-optimization Telegram spam during A/B test
    suppress_telegram(True)

    batch_dir = args.batch_dir
    progress_path = os.path.join(batch_dir, "batch_progress.json")
    if not os.path.exists(progress_path):
        print(f"ERROR: {progress_path} not found")
        sys.exit(1)

    with open(progress_path, encoding="utf-8-sig") as f:
        progress = json.load(f)

    # Resolve OHLCV
    manifest_path = progress.get("manifest", "")
    if manifest_path == "manifest.json":
        manifest_path = os.path.join(batch_dir, manifest_path)
    elif not os.path.isabs(manifest_path):
        manifest_path = os.path.join(PROJECT_ROOT, manifest_path)
    if args.ohlcv_path:
        ohlcv_path = args.ohlcv_path
        print(f"OHLCV data (override): {ohlcv_path}")
    else:
        ohlcv_path = find_ohlcv_path(manifest_path)
        print(f"OHLCV data: {ohlcv_path}")

    # Select targets
    targets = select_targets(progress, batch_dir, metric=args.metric)
    if not targets:
        print("ERROR: No valid targets found in batch directory.")
        sys.exit(1)

    print(f"\nSelected {len(targets)} targets:")
    for i, t in enumerate(targets, 1):
        print(f"  Target {i}: {t['experiment_label']} — {t['side'].upper()} ({t['metric']})")
        print(f"            config: {t['config_path']}")
        print(f"            preds:  {t['merged_path']}")

    # ── Pipeline A (Control) ──────────────────────────────────────────────
    print(f"\n{'='*70}")
    print(f"PIPELINE A (Control) — Legacy Optuna, {args.n_trials_a} trials")
    print(f"{'='*70}")

    results_a: list[dict] = []
    for i, target in enumerate(targets, 1):
        print(f"\n  [A] Target {i}/{len(targets)}: {target['label']}")
        res = run_single_ab_target(
            pipeline="A",
            config_path=target["config_path"],
            merged_path=target["merged_path"],
            ohlcv_path=ohlcv_path,
            side=target["side"],
            n_trials=args.n_trials_a,
            holdout_months=args.holdout_months,
            objective_metric=args.objective,
            label=f"AB_A_{target['label']}",
        )
        results_a.append(res)
        status = f"ERR: {res['error']}" if res["error"] else (
            f"PnL=${res['train_pnl']:,.0f}  Holdout=${res['holdout_pnl']:,.0f}"
            if res["holdout_pnl"] is not None else f"PnL=${res['train_pnl']:,.0f}"
        )
        print(f"    Done in {res['wall_time_s']:.1f}s — {status}")

    # ── Pipeline B (Test) ─────────────────────────────────────────────────
    print(f"\n{'='*70}")
    print(f"PIPELINE B (Test) — Hybrid vbt+Optuna, {args.vbt_top_n} warm-start + {args.n_trials_b} trials")
    print(f"{'='*70}")

    results_b: list[dict] = []
    for i, target in enumerate(targets, 1):
        print(f"\n  [B] Target {i}/{len(targets)}: {target['label']}")
        res = run_single_ab_target(
            pipeline="B",
            config_path=target["config_path"],
            merged_path=target["merged_path"],
            ohlcv_path=ohlcv_path,
            side=target["side"],
            n_trials=args.n_trials_b,
            holdout_months=args.holdout_months,
            objective_metric=args.objective,
            vbt_top_n=args.vbt_top_n,
            label=f"AB_B_{target['label']}",
        )
        results_b.append(res)
        status = f"ERR: {res['error']}" if res["error"] else (
            f"PnL=${res['train_pnl']:,.0f}  Holdout=${res['holdout_pnl']:,.0f}"
            if res["holdout_pnl"] is not None else f"PnL=${res['train_pnl']:,.0f}"
        )
        print(f"    Done in {res['wall_time_s']:.1f}s — {status}")

    # ── Report ────────────────────────────────────────────────────────────
    report_md = generate_report(targets, results_a, results_b, args)

    os.makedirs(args.output_dir, exist_ok=True)
    ts_suffix = datetime.now().strftime("%Y%m%d_%H%M")
    report_path = os.path.join(
        args.output_dir,
        f"vbt_ab_test_results_{os.path.basename(batch_dir)}_{ts_suffix}.md",
    )
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_md)

    print(f"\n{'='*70}")
    print(f"REPORT SAVED: {report_path}")
    print(f"{'='*70}")

    # Quick summary to stdout
    total_a = sum(r["wall_time_s"] for r in results_a)
    total_b = sum(r["wall_time_s"] for r in results_b)
    speedup = total_a / total_b if total_b > 0 else 0
    print(f"\nTotal A: {total_a:.0f}s | Total B: {total_b:.0f}s | Speedup: {speedup:.2f}x")


if __name__ == "__main__":
    main()
