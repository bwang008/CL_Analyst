"""
Experiment Runner for CL_Analyst Model Improvement.

Orchestrates a single experiment end-to-end:
1. Accepts experiment config
2. Creates/loads the appropriate dataset
3. Runs train_and_evaluate() with the specified config
4. Captures all metrics
5. Appends results to agent/experiment_log.json
6. Returns pass/fail verdict

Usage:
    python agent/experiment_runner.py --quick-test
    python agent/experiment_runner.py --experiment S1a
"""

import os
import sys
import json
import time
from datetime import datetime

# Ensure project root is on path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

from main import train_and_evaluate, log_train_run, get_processed_cl_df
from src.data_processor import DataProcessor


EXPERIMENT_LOG_PATH = os.path.join(PROJECT_ROOT, "agent", "experiment_log.json")
STRATEGY_QUEUE_PATH = os.path.join(PROJECT_ROOT, "agent", "strategy_queue.json")


def load_experiment_log():
    """Load the experiment log, or create a fresh one."""
    if os.path.exists(EXPERIMENT_LOG_PATH):
        with open(EXPERIMENT_LOG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"experiments": []}


def save_experiment_log(log_data):
    """Save experiment log to disk."""
    with open(EXPERIMENT_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(log_data, f, indent=2, default=str)


def load_strategy_queue():
    """Load strategy queue."""
    with open(STRATEGY_QUEUE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_strategy_queue(queue_data):
    """Save strategy queue."""
    with open(STRATEGY_QUEUE_PATH, "w", encoding="utf-8") as f:
        json.dump(queue_data, f, indent=2, default=str)


def update_strategy_status(strategy_id, status):
    """Update the status of a strategy in the queue."""
    queue = load_strategy_queue()
    for s in queue["strategies"]:
        if s["id"] == strategy_id:
            s["status"] = status
            break
    save_strategy_queue(queue)


def generate_experiment_id(log_data):
    """Generate the next experiment ID."""
    existing = log_data.get("experiments", [])
    return f"EXP-{len(existing) + 1:03d}"


def run_experiment(
    experiment_id: str,
    strategy_id: str,
    hypothesis: str,
    changes: dict,
    # Data config
    data_path: str = "data/processed/CL_set_03.parquet",
    dataset_version: str = None,
    input_path: str = "data/raw/CL.csv",
    force_reprocess: bool = False,
    # Target config
    target_name: str = "TARGET_DIR_8PCT_MULTI",
    threshold: float = 0.08,
    horizon: int = 576,
    # Training config
    method: str = "walk_forward",
    balance_mode: str = "weight",
    holdout_pct: float = 0.15,
    purge_bars: int = 576,
    min_train_bars: int = 8640,
    fold_size_bars: int = 8640,
    model_params: dict = None,
    random_state: int = None,
    # Output config
    output_dir: str = "reports",
    model_dir: str = "models",
    checkpoint_dir: str = "reports/checkpoints",
):
    """
    Run a single experiment and log results.

    Returns:
        dict: Experiment result with metrics and verdict
    """
    print("=" * 70)
    print(f"EXPERIMENT {experiment_id}: {hypothesis}")
    print("=" * 70)

    start_time = time.perf_counter()
    timestamp = datetime.now().isoformat(timespec="seconds")

    # Step 1: Ensure data is available
    if dataset_version and (force_reprocess or not os.path.exists(data_path)):
        print(f"\n[Data] Processing dataset version: {dataset_version}")
        get_processed_cl_df(
            input_path=input_path,
            dataset_version=dataset_version,
            threshold=threshold,
            horizon=horizon,
            force_reprocess=force_reprocess,
        )
        # Update data_path to match the processor output
        processor = DataProcessor(
            input_path=input_path,
            dataset_version=dataset_version,
        )
        data_path = processor.output_path

    # Step 2: Run training
    try:
        results = train_and_evaluate(
            data_path=data_path,
            holdout_pct=holdout_pct,
            purge_bars=purge_bars,
            min_train_bars=min_train_bars,
            fold_size_bars=fold_size_bars,
            threshold=threshold,
            output_dir=output_dir,
            model_dir=model_dir,
            model_params=model_params,
            verbose=True,
            method=method,
            target_name=target_name,
            balance_mode=balance_mode,
            random_state=random_state,
            checkpoint_path=os.path.join(checkpoint_dir, f"{experiment_id}.joblib"),
        )
    except Exception as e:
        error_result = {
            "id": experiment_id,
            "timestamp": timestamp,
            "strategy": strategy_id,
            "hypothesis": hypothesis,
            "changes": changes,
            "error": str(e),
            "verdict": "error",
        }
        _append_to_log(error_result)
        print(f"\n[ERROR] Experiment failed: {e}")
        return error_result

    # Step 3: Extract metrics
    elapsed = time.perf_counter() - start_time
    vault_result = results.get("vault_result") or {}

    def _scalar(v):
        if isinstance(v, dict):
            vals = [x for x in v.values() if x is not None]
            return float(sum(vals) / len(vals)) if vals else None
        return v

    def _class_metric(v, cls):
        if isinstance(v, dict):
            return v.get(cls)
        return None

    metrics = {
        "vault_accuracy": vault_result.get("accuracy"),
        "signal_precision_buy": _class_metric(vault_result.get("precision"), "Buy"),
        "signal_recall_buy": _class_metric(vault_result.get("recall"), "Buy"),
        "signal_f1_buy": _class_metric(vault_result.get("f1"), "Buy"),
        "signal_precision_sell": _class_metric(vault_result.get("precision"), "Sell"),
        "signal_recall_sell": _class_metric(vault_result.get("recall"), "Sell"),
        "signal_f1_sell": _class_metric(vault_result.get("f1"), "Sell"),
        "hold_precision": _class_metric(vault_result.get("precision"), "Hold"),
        "hold_recall": _class_metric(vault_result.get("recall"), "Hold"),
        "hold_f1": _class_metric(vault_result.get("f1"), "Hold"),
        "n_samples": vault_result.get("n_samples"),
        "wall_time_seconds": elapsed,
    }

    # Step 4: Determine verdict
    verdict = evaluate_verdict(metrics)

    # Step 5: Build experiment record
    experiment_record = {
        "id": experiment_id,
        "timestamp": timestamp,
        "strategy": strategy_id,
        "hypothesis": hypothesis,
        "changes": changes,
        "config": {
            "data_path": data_path,
            "target_name": target_name,
            "threshold": threshold,
            "horizon": horizon,
            "method": method,
            "balance_mode": balance_mode,
            "model_params": model_params,
        },
        "metrics": metrics,
        "verdict": verdict,
    }

    # Step 6: Log results
    _append_to_log(experiment_record)

    # Also log to the standard train_runs.log
    log_train_run(
        report_path=os.path.join("reports", "train_runs.log"),
        target_name=target_name,
        method=method,
        balance_mode=balance_mode,
        data_path=data_path,
        results=results,
    )

    # Print summary
    print("\n" + "=" * 70)
    print(f"EXPERIMENT {experiment_id} COMPLETE")
    print(f"  Verdict: {verdict}")
    print(f"  Vault Accuracy: {metrics.get('vault_accuracy')}")
    print(f"  Signal Precision (Buy): {metrics.get('signal_precision_buy')}")
    print(f"  Signal Recall (Buy):    {metrics.get('signal_recall_buy')}")
    print(f"  Signal F1 (Buy):        {metrics.get('signal_f1_buy')}")
    print(f"  Wall Time:              {elapsed:.1f}s")
    print("=" * 70)

    return experiment_record


def evaluate_verdict(metrics):
    """
    Evaluate experiment verdict based on metrics.

    Tiers:
    - 'promising': signal F1 > 0.10 (baseline is ~0)
    - 'improvement': signal precision OR recall > 0.10
    - 'marginal': any signal metric > 0.02
    - 'no_improvement': no significant change
    """
    buy_precision = metrics.get("signal_precision_buy") or 0
    buy_recall = metrics.get("signal_recall_buy") or 0
    buy_f1 = metrics.get("signal_f1_buy") or 0

    if buy_f1 > 0.10:
        return "promising"
    elif buy_precision > 0.10 or buy_recall > 0.10:
        return "improvement"
    elif buy_precision > 0.02 or buy_recall > 0.02 or buy_f1 > 0.02:
        return "marginal"
    else:
        return "no_improvement"


def _append_to_log(record):
    """Append an experiment record to the log file."""
    log_data = load_experiment_log()
    log_data["experiments"].append(record)
    save_experiment_log(log_data)


def quick_test():
    """Run a quick smoke test with method=simple on default data."""
    print("\n=== QUICK TEST: Smoke test with simple split ===\n")

    log_data = load_experiment_log()
    exp_id = generate_experiment_id(log_data)

    return run_experiment(
        experiment_id=exp_id,
        strategy_id="SMOKE_TEST",
        hypothesis="Quick smoke test to verify experiment runner works",
        changes={"test": True},
        data_path="data/processed/CL_set_03.parquet",
        target_name="TARGET_DIR_4PCT_LONG",
        method="simple",
        balance_mode="downsample",
        threshold=0.04,
    )


if __name__ == "__main__":
    args = sys.argv[1:]

    if "--quick-test" in args:
        quick_test()
    elif "--experiment" in args:
        exp_idx = args.index("--experiment")
        if exp_idx + 1 < len(args):
            strategy_id = args[exp_idx + 1]
            print(f"Running strategy: {strategy_id}")
            # Load strategy from queue and run
            queue = load_strategy_queue()
            strategy = None
            for s in queue["strategies"]:
                if s["id"] == strategy_id:
                    strategy = s
                    break
            if strategy is None:
                print(f"Strategy {strategy_id} not found in queue")
                sys.exit(1)

            update_strategy_status(strategy_id, "running")
            log_data = load_experiment_log()
            exp_id = generate_experiment_id(log_data)

            config = strategy.get("config", {})
            result = run_experiment(
                experiment_id=exp_id,
                strategy_id=strategy_id,
                hypothesis=strategy["hypothesis"],
                changes=config,
                **{k: v for k, v in config.items()
                   if k in run_experiment.__code__.co_varnames},
            )

            status = "done" if result.get("verdict") != "error" else "failed"
            update_strategy_status(strategy_id, status)
        else:
            print("Usage: python agent/experiment_runner.py --experiment S1a")
    else:
        print("Usage:")
        print("  python agent/experiment_runner.py --quick-test")
        print("  python agent/experiment_runner.py --experiment S1a")
