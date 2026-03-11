"""
Archive trained model artifacts into a registry bundle keyed by experiment ID.

Usage:
    python agent/archive_model.py --experiment-id EXP-017
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT_LOG = PROJECT_ROOT / "agent" / "experiment_log.json"
REPORT_LOG = PROJECT_ROOT / "REPORT.log"
THRESHOLD_SWEEP = PROJECT_ROOT / "reports" / "threshold_sweep.json"
REGISTRY_ROOT = PROJECT_ROOT / "models" / "registry"
REGISTRY_README = REGISTRY_ROOT / "README.md"


def _load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)

def _load_json_optional(path: Path | None) -> dict | None:
    if not path:
        return None
    if not path.exists():
        return None
    return _load_json(path)


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _find_experiment(experiment_id: str, log_data: dict) -> dict:
    for row in log_data.get("experiments", []):
        if row.get("id") == experiment_id:
            return row
    raise ValueError(f"Experiment ID not found: {experiment_id}")


def _extract_report_metrics(report_text: str, experiment_id: str) -> dict:
    """
    Parse summary metrics from REPORT.log for EXP-017 champion table.
    Falls back to empty values if not found.
    """
    metrics = {
        "win_rate": None,
        "profit_factor": None,
        "avg_pnl_per_trade_pct": None,
        "max_drawdown_pct": None,
        "total_trades": None,
        "tp_hits": None,
        "sl_hits": None,
    }
    if experiment_id != "EXP-017" or not report_text:
        return metrics

    patterns = {
        "win_rate": r"\|\s+\*\*Win Rate\*\*\s+\|\s+\*\*([0-9.]+%)\*\*\s+\|",
        "profit_factor": r"\|\s+\*\*Profit Factor\*\*\s+\|\s+\*\*([0-9.]+)\*\*\s+\|",
        "avg_pnl_per_trade_pct": r"\|\s+\*\*Avg PnL per Trade\*\*\s+\|\s+\*\*([+\-0-9.]+%)\*\*\s+\|",
        "max_drawdown_pct": r"\|\s+\*\*Max Drawdown\*\*\s+\|\s+\*\*([0-9.]+%)\*\*\s+\|",
        "total_trades": r"\|\s+Total Trades\s+\|\s+([0-9,]+)\s+\|",
        "tp_hits": r"\|\s+TP Hits\s+\|\s+([0-9,]+)\s+\|",
        "sl_hits": r"\|\s+SL Hits\s+\|\s+([0-9,]+)\s+\|",
    }
    for key, pattern in patterns.items():
        m = re.search(pattern, report_text)
        if m:
            value = m.group(1)
            if key in {"total_trades", "tp_hits", "sl_hits"}:
                metrics[key] = int(value.replace(",", ""))
            elif key == "profit_factor":
                metrics[key] = float(value)
            else:
                metrics[key] = value
    return metrics


def _extract_features_summary(report_text: str) -> str:
    if not report_text:
        return "Unknown"
    m = re.search(r"\*\s+\*\*Features:\*\*\s+(.+)", report_text)
    return m.group(1).strip() if m else "Unknown"

def _summarize_backtest(backtest_csv_path: Path | None) -> dict[str, Any] | None:
    if not backtest_csv_path or not backtest_csv_path.exists():
        return None
    import pandas as pd

    df = pd.read_csv(backtest_csv_path)
    if df.empty or "pnl" not in df.columns:
        return None
    win_rate = float((df["pnl"] > 0).mean())
    gross_profits = float(df.loc[df["pnl"] > 0, "pnl"].sum())
    gross_losses = float(abs(df.loc[df["pnl"] < 0, "pnl"].sum()))
    profit_factor = float(gross_profits / gross_losses) if gross_losses > 0 else float("inf")
    total_net_pnl = float(df["pnl"].sum())
    return {
        "total_trades": int(len(df)),
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "total_net_pnl": total_net_pnl,
        "tp_hits": int((df.get("reason") == "TP").sum()) if "reason" in df.columns else None,
        "sl_hits": int((df.get("reason") == "SL").sum()) if "reason" in df.columns else None,
        "timeouts": int((df.get("reason") == "Timeout").sum()) if "reason" in df.columns else None,
    }


def _ensure_registry_readme() -> None:
    REGISTRY_ROOT.mkdir(parents=True, exist_ok=True)
    if REGISTRY_README.exists():
        return
    REGISTRY_README.write_text(
        "# Model Registry\n\n"
        "Curated archived model bundles by Experiment ID.\n\n"
        "| Date | Experiment ID | Target | Type (Long/Short) | Win Rate | Profit Factor | Notes |\n"
        "|------|---------------|--------|-------------------|----------|---------------|-------|\n",
        encoding="utf-8",
    )


def _append_registry_row(
    date_str: str,
    experiment_id: str,
    target: str,
    trade_type: str,
    win_rate: str,
    profit_factor: str,
    notes: str,
) -> None:
    _ensure_registry_readme()
    row_prefix = f"| {date_str} | {experiment_id} | {target} | {trade_type} |"
    existing = _read_text(REGISTRY_README)
    if row_prefix in existing:
        return
    with REGISTRY_README.open("a", encoding="utf-8") as f:
        f.write(
            f"| {date_str} | {experiment_id} | {target} | {trade_type} | "
            f"{win_rate} | {profit_factor} | {notes} |\n"
        )


def _format_type(target_name: str) -> str:
    if target_name.endswith("_LONG"):
        return "Long"
    if target_name.endswith("_SHORT"):
        return "Short"
    return "Multi"


def archive_experiment(
    experiment_id: str,
    model_path: Path | None = None,
    vault_metrics_path: Path | None = None,
    backtest_results_path: Path | None = None,
    threshold_sweep_path: Path | None = None,
    selected_trade_threshold: float | None = None,
    notes: str | None = None,
    oos_predictions_path: Path | None = None,
    experiment_config_path: Path | None = None,
    feature_importance_path: Path | None = None,
) -> Path:
    log_data = _load_json(EXPERIMENT_LOG)
    exp = _find_experiment(experiment_id, log_data)

    strategy = exp.get("strategy", "UnknownStrategy")
    target = (exp.get("config") or {}).get("target_name") or (exp.get("changes") or {}).get("target_name") or "UNKNOWN_TARGET"
    trade_type = _format_type(target)
    timestamp = exp.get("timestamp", datetime.now().isoformat(timespec="seconds"))
    date_str = timestamp.split("T")[0]
    safe_name = f"{experiment_id}_{strategy}"
    bundle_dir = REGISTRY_ROOT / safe_name
    bundle_dir.mkdir(parents=True, exist_ok=True)

    default_model_path = PROJECT_ROOT / "models" / "final_model.pkl"
    selected_model_path = model_path if model_path else default_model_path
    if not selected_model_path.exists():
        raise FileNotFoundError(
            f"Model file not found: {selected_model_path}. "
            "Pass --model-path explicitly."
        )

    # 1) Trained model artifact (.pkl)
    archived_model_path = bundle_dir / selected_model_path.name
    shutil.copy2(selected_model_path, archived_model_path)

    # Optional: copy raw metrics artifacts (so bundle is self-contained)
    if vault_metrics_path and vault_metrics_path.exists():
        shutil.copy2(vault_metrics_path, bundle_dir / vault_metrics_path.name)
    if backtest_results_path and backtest_results_path.exists():
        shutil.copy2(backtest_results_path, bundle_dir / backtest_results_path.name)
    if threshold_sweep_path and threshold_sweep_path.exists():
        shutil.copy2(threshold_sweep_path, bundle_dir / threshold_sweep_path.name)

    # Copy OOS predictions (the expensive, reproducibility-critical artifact)
    if oos_predictions_path and oos_predictions_path.exists():
        shutil.copy2(oos_predictions_path, bundle_dir / "oos_predictions.csv")
        print(f"  Archived OOS predictions: {oos_predictions_path}")

    # Copy experiment config for full reproducibility
    if experiment_config_path and experiment_config_path.exists():
        shutil.copy2(experiment_config_path, bundle_dir / "experiment_config.json")
        print(f"  Archived experiment config: {experiment_config_path}")

    # Copy feature importance
    if feature_importance_path and feature_importance_path.exists():
        shutil.copy2(feature_importance_path, bundle_dir / "feature_importance.csv")
        print(f"  Archived feature importance: {feature_importance_path}")

    # 2) Experiment-specific config and metadata
    report_text = _read_text(REPORT_LOG)
    threshold_data = _load_json_optional(threshold_sweep_path) or (
        _load_json(THRESHOLD_SWEEP) if THRESHOLD_SWEEP.exists() else {}
    )
    features_summary = _extract_features_summary(report_text)
    report_metrics = _extract_report_metrics(report_text, experiment_id=experiment_id)
    backtest_summary = _summarize_backtest(backtest_results_path)
    vault_metrics = _load_json_optional(vault_metrics_path)

    config_payload = {
        "experiment_id": experiment_id,
        "strategy": strategy,
        "timestamp": timestamp,
        "target_name": target,
        "type": trade_type,
        "dataset_path": (exp.get("config") or {}).get("data_path"),
        "training_threshold": (exp.get("config") or {}).get("threshold"),
        "optimized_probability_threshold": threshold_data.get("best_threshold"),
        "selected_trade_threshold": selected_trade_threshold,
        "features_summary": features_summary,
        "model_params": (exp.get("config") or {}).get("model_params"),
        "model_file": {
            "archived_name": archived_model_path.name,
            "source_path": str(selected_model_path),
            "source_modified_utc": datetime.fromtimestamp(
                selected_model_path.stat().st_mtime,
                timezone.utc,
            ).isoformat(timespec="seconds"),
        },
        "source_experiment_record": exp,
    }
    (bundle_dir / "config.json").write_text(json.dumps(config_payload, indent=2), encoding="utf-8")

    # 3) Classification + backtest metrics bundle
    classification_metrics = {
        "vault_accuracy": (exp.get("metrics") or {}).get("vault_accuracy"),
        "signal_precision_buy": (exp.get("metrics") or {}).get("signal_precision_buy"),
        "signal_recall_buy": (exp.get("metrics") or {}).get("signal_recall_buy"),
        "signal_f1_buy": (exp.get("metrics") or {}).get("signal_f1_buy"),
        "n_samples": (exp.get("metrics") or {}).get("n_samples"),
    }
    metrics_payload = {
        "experiment_id": experiment_id,
        "strategy": strategy,
        "classification_metrics": classification_metrics,
        "vault_metrics_file": vault_metrics,
        "backtest_summary_from_backtest_csv": backtest_summary,
        "backtest_summary_from_report_log": report_metrics,
    }
    (bundle_dir / "metrics.json").write_text(json.dumps(metrics_payload, indent=2), encoding="utf-8")

    # Requested "backtest.csv" artifact (summary-style row for archived model)
    backtest_csv_headers = [
        "experiment_id",
        "strategy",
        "win_rate",
        "profit_factor",
        "avg_pnl_per_trade_pct",
        "max_drawdown_pct",
        "total_trades",
        "tp_hits",
        "sl_hits",
    ]
    bt = backtest_summary or {}
    backtest_csv_values = [
        experiment_id,
        strategy,
        f"{(bt.get('win_rate') or 0.0):.1%}" if backtest_summary else (report_metrics.get("win_rate") or ""),
        f"{(bt.get('profit_factor') or 0.0):.2f}" if backtest_summary else (report_metrics.get("profit_factor") or ""),
        report_metrics.get("avg_pnl_per_trade_pct") or "",
        report_metrics.get("max_drawdown_pct") or "",
        bt.get("total_trades") if backtest_summary else report_metrics.get("total_trades"),
        bt.get("tp_hits") if backtest_summary else report_metrics.get("tp_hits"),
        bt.get("sl_hits") if backtest_summary else report_metrics.get("sl_hits"),
    ]
    backtest_csv = ",".join(backtest_csv_headers) + "\n" + ",".join("" if v is None else str(v) for v in backtest_csv_values) + "\n"
    (bundle_dir / "backtest.csv").write_text(backtest_csv, encoding="utf-8")

    # 4) Registry catalog row
    win_rate_for_catalog = (
        f"{(bt.get('win_rate') or 0.0):.1%}" if backtest_summary else str(report_metrics.get("win_rate") or "N/A")
    )
    pf_for_catalog = (
        f"{(bt.get('profit_factor') or 0.0):.2f}" if backtest_summary else str(report_metrics.get("profit_factor") or "N/A")
    )
    _append_registry_row(
        date_str=date_str,
        experiment_id=experiment_id,
        target=target,
        trade_type=trade_type,
        win_rate=win_rate_for_catalog,
        profit_factor=pf_for_catalog,
        notes=notes
        or (
            f"{strategy}; trade_threshold={selected_trade_threshold}; "
            f"features={features_summary}; backtest sourced from {backtest_results_path.name if backtest_results_path else 'N/A'}"
        ),
    )

    print(f"Archived experiment bundle: {bundle_dir}")
    return bundle_dir


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--experiment-id", required=True, help="Experiment ID to archive, e.g., EXP-017")
    parser.add_argument(
        "--model-path",
        default=None,
        help="Optional explicit .pkl model artifact path to archive",
    )
    parser.add_argument("--vault-metrics-path", default=None, help="Optional path to vault_metrics.json to copy into bundle")
    parser.add_argument("--backtest-results-path", default=None, help="Optional path to backtest_results.csv to copy into bundle")
    parser.add_argument("--threshold-sweep-path", default=None, help="Optional path to threshold sweep JSON to copy into bundle")
    parser.add_argument("--selected-trade-threshold", type=float, default=None, help="Threshold actually used for trading/backtest")
    parser.add_argument("--notes", default=None, help="Optional notes for registry catalog row")
    parser.add_argument("--oos-predictions-path", default=None,
                        help="Path to OOS predictions CSV to archive")
    parser.add_argument("--experiment-config-path", default=None,
                        help="Path to experiment config JSON to archive")
    parser.add_argument("--feature-importance-path", default=None,
                        help="Path to feature importance CSV to archive")
    args = parser.parse_args()

    model_path = Path(args.model_path) if args.model_path else None
    vault_metrics_path = Path(args.vault_metrics_path) if args.vault_metrics_path else None
    backtest_results_path = Path(args.backtest_results_path) if args.backtest_results_path else None
    threshold_sweep_path = Path(args.threshold_sweep_path) if args.threshold_sweep_path else None
    oos_predictions_path = Path(args.oos_predictions_path) if args.oos_predictions_path else None
    experiment_config_path = Path(args.experiment_config_path) if args.experiment_config_path else None
    feature_importance_path = Path(args.feature_importance_path) if args.feature_importance_path else None
    archive_experiment(
        experiment_id=args.experiment_id,
        model_path=model_path,
        vault_metrics_path=vault_metrics_path,
        backtest_results_path=backtest_results_path,
        threshold_sweep_path=threshold_sweep_path,
        selected_trade_threshold=args.selected_trade_threshold,
        notes=args.notes,
        oos_predictions_path=oos_predictions_path,
        experiment_config_path=experiment_config_path,
        feature_importance_path=feature_importance_path,
    )


if __name__ == "__main__":
    main()
