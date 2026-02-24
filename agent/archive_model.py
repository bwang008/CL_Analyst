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
from datetime import UTC, datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT_LOG = PROJECT_ROOT / "agent" / "experiment_log.json"
REPORT_LOG = PROJECT_ROOT / "REPORT.log"
THRESHOLD_SWEEP = PROJECT_ROOT / "reports" / "threshold_sweep.json"
REGISTRY_ROOT = PROJECT_ROOT / "models" / "registry"
REGISTRY_README = REGISTRY_ROOT / "README.md"


def _load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


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


def archive_experiment(experiment_id: str, model_path: Path | None = None) -> Path:
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

    # 2) Experiment-specific config and metadata
    report_text = _read_text(REPORT_LOG)
    threshold_data = _load_json(THRESHOLD_SWEEP) if THRESHOLD_SWEEP.exists() else {}
    features_summary = _extract_features_summary(report_text)
    report_metrics = _extract_report_metrics(report_text, experiment_id=experiment_id)

    config_payload = {
        "experiment_id": experiment_id,
        "strategy": strategy,
        "timestamp": timestamp,
        "target_name": target,
        "type": trade_type,
        "dataset_path": (exp.get("config") or {}).get("data_path"),
        "training_threshold": (exp.get("config") or {}).get("threshold"),
        "optimized_probability_threshold": threshold_data.get("best_threshold"),
        "features_summary": features_summary,
        "model_params": (exp.get("config") or {}).get("model_params"),
        "model_file": {
            "archived_name": archived_model_path.name,
            "source_path": str(selected_model_path),
            "source_modified_utc": datetime.fromtimestamp(
                selected_model_path.stat().st_mtime,
                UTC,
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
    backtest_csv_values = [
        experiment_id,
        strategy,
        report_metrics.get("win_rate"),
        report_metrics.get("profit_factor"),
        report_metrics.get("avg_pnl_per_trade_pct"),
        report_metrics.get("max_drawdown_pct"),
        report_metrics.get("total_trades"),
        report_metrics.get("tp_hits"),
        report_metrics.get("sl_hits"),
    ]
    backtest_csv = ",".join(backtest_csv_headers) + "\n" + ",".join("" if v is None else str(v) for v in backtest_csv_values) + "\n"
    (bundle_dir / "backtest.csv").write_text(backtest_csv, encoding="utf-8")

    # 4) Registry catalog row
    _append_registry_row(
        date_str=date_str,
        experiment_id=experiment_id,
        target=target,
        trade_type=trade_type,
        win_rate=str(report_metrics.get("win_rate") or "N/A"),
        profit_factor=str(report_metrics.get("profit_factor") or "N/A"),
        notes=(
            f"{strategy}; features={features_summary}; "
            "backtest summary sourced from REPORT.log"
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
    args = parser.parse_args()

    model_path = Path(args.model_path) if args.model_path else None
    archive_experiment(experiment_id=args.experiment_id, model_path=model_path)


if __name__ == "__main__":
    main()
