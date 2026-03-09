"""
Optuna Strategy Parameter Optimizer for CL_Analyst.

Tunes **trading strategy parameters** (TP/SL multipliers, thresholds,
cooldown, max hold bars, trailing stop) using BacktestEngine against OOS
predictions.  This is distinct from ``optuna_lgbm_search.py`` which tunes
LightGBM *model* hyperparameters.

Usage:
    python agent/strategy_optimizer.py \\
        --config configs/strategies/manatee2.json \\
        --n-trials 1000

    python agent/strategy_optimizer.py \\
        --config configs/strategies/koala2.json \\
        --n-trials 1000
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

import optuna
from agent.backtest_engine import BacktestEngine, BacktestResult, load_ohlcv, load_predictions

optuna.logging.set_verbosity(optuna.logging.WARNING)


# ---------------------------------------------------------------------------
# Metrics helpers
# ---------------------------------------------------------------------------


def compute_sharpe(equity_curve: list[float], bars_per_year: int = 105120) -> float:
    """Annualized Sharpe ratio from bar-by-bar equity curve."""
    if len(equity_curve) < 2:
        return 0.0
    returns = np.diff(equity_curve)
    if np.std(returns) == 0:
        return 0.0
    return float(np.mean(returns) / np.std(returns) * np.sqrt(bars_per_year))


def compute_sortino(equity_curve: list[float], bars_per_year: int = 105120) -> float:
    """Annualized Sortino ratio (downside deviation only)."""
    if len(equity_curve) < 2:
        return 0.0
    returns = np.diff(equity_curve)
    downside = returns[returns < 0]
    if len(downside) == 0 or np.std(downside) == 0:
        return float("inf") if np.mean(returns) > 0 else 0.0
    return float(np.mean(returns) / np.std(downside) * np.sqrt(bars_per_year))


def compute_calmar(total_pnl: float, max_drawdown: float, years: float) -> float:
    """Calmar ratio: annualized return / max drawdown."""
    if max_drawdown >= 0 or years <= 0:
        return 0.0
    annualized_return = total_pnl / years
    return float(annualized_return / abs(max_drawdown))


def extract_metrics(result: BacktestResult) -> dict:
    """Extract comprehensive metrics from a BacktestResult."""
    trades = result.trades
    if not trades:
        return {
            "total_pnl": 0, "profit_factor": 0, "win_rate": 0,
            "trade_count": 0, "max_drawdown": 0,
            "sharpe_ratio": 0, "sortino_ratio": 0, "calmar_ratio": 0,
            "avg_trade_pnl": 0, "avg_win": 0, "avg_loss": 0,
            "largest_win": 0, "largest_loss": 0,
            "avg_duration_bars": 0,
            "pct_tp": 0, "pct_sl": 0, "pct_time_barrier": 0, "pct_trailing_be": 0,
            "profit_per_drawdown": 0, "expectancy": 0,
        }

    wins = [t.net_pnl_dollars for t in trades if t.net_pnl_dollars > 0]
    losses = [t.net_pnl_dollars for t in trades if t.net_pnl_dollars <= 0]
    durations = [t.duration_bars for t in trades]

    # Years of data for Calmar
    if result.start_dt and result.end_dt:
        years = (result.end_dt - result.start_dt).total_seconds() / (365.25 * 86400)
    else:
        years = 1.0

    # Exit distribution
    exit_dist = result.exit_distribution
    n = len(trades)

    dd = result.max_drawdown

    return {
        "total_pnl": round(result.total_pnl, 2),
        "profit_factor": round(result.profit_factor, 4),
        "win_rate": round(result.win_rate, 4),
        "trade_count": result.trade_count,
        "max_drawdown": round(dd, 2),
        "sharpe_ratio": round(compute_sharpe(result.equity_curve), 4),
        "sortino_ratio": round(compute_sortino(result.equity_curve), 4),
        "calmar_ratio": round(compute_calmar(result.total_pnl, dd, years), 4),
        "avg_trade_pnl": round(np.mean([t.net_pnl_dollars for t in trades]), 2),
        "avg_win": round(np.mean(wins), 2) if wins else 0,
        "avg_loss": round(np.mean(losses), 2) if losses else 0,
        "largest_win": round(max(wins), 2) if wins else 0,
        "largest_loss": round(min(losses), 2) if losses else 0,
        "avg_duration_bars": round(np.mean(durations), 1),
        "pct_tp": round(exit_dist.get("TP", {}).get("pct", 0), 2),
        "pct_sl": round(exit_dist.get("SL", {}).get("pct", 0), 2),
        "pct_time_barrier": round(exit_dist.get("TIME_BARRIER", {}).get("pct", 0), 2),
        "pct_trailing_be": round(exit_dist.get("TRAILING_BE", {}).get("pct", 0), 2),
        "profit_per_drawdown": round(abs(result.total_pnl / dd), 4) if dd < 0 else 0,
        "expectancy": round(
            result.win_rate * (np.mean(wins) if wins else 0)
            + (1 - result.win_rate) * (np.mean(losses) if losses else 0), 2
        ),
    }


# ---------------------------------------------------------------------------
# Optuna objective
# ---------------------------------------------------------------------------


def make_objective(
    base_cfg: dict,
    predictions_df: pd.DataFrame,
    ohlcv_df: pd.DataFrame,
    results_cache: dict | None = None,
):
    """Create a closure that Optuna can call with trial params.

    If *results_cache* is provided (dict), the full BacktestResult is
    stored under the trial number so the callback can read metrics
    without re-running the backtest.
    """

    def objective(trial: optuna.Trial) -> tuple[float, float]:
        cfg = copy.deepcopy(base_cfg)

        # Search space
        cfg["tp_atr_mult"] = trial.suggest_float("tp_atr_mult", 1.5, 7.0, step=0.5)
        cfg["sl_atr_mult"] = trial.suggest_float("sl_atr_mult", 0.5, 3.0, step=0.5)
        cfg["trailing_atr_mult"] = trial.suggest_float("trailing_atr_mult", 0.5, 5.0, step=0.5)
        cfg["cooldown_bars"] = trial.suggest_int("cooldown_bars", 0, 30, step=5)
        cfg["max_hold_bars"] = trial.suggest_int("max_hold_bars", 72, 576, step=72)

        # Threshold — update in the models section if ensemble, else top-level
        threshold = trial.suggest_float("entry_threshold", 0.50, 0.80, step=0.05)
        cfg["entry_threshold"] = threshold
        if "models" in cfg:
            for direction in cfg["models"]:
                cfg["models"][direction]["threshold"] = threshold

        engine = BacktestEngine.from_config(cfg)
        result = engine.run(predictions_df, ohlcv_df)

        # Cache result for the callback (avoids re-running the backtest)
        if results_cache is not None:
            results_cache[trial.number] = result

        # Reject degenerate configs (too few trades)
        if result.trade_count < 50:
            return 0.0, -999999.0

        # Multi-objective: maximize PF, maximize DD (DD is negative, so larger = better)
        return result.profit_factor, result.max_drawdown

    return objective


# ---------------------------------------------------------------------------
# Save trial config
# ---------------------------------------------------------------------------


def save_trial_config(
    base_cfg: dict,
    trial: optuna.Trial,
    metrics: dict,
    output_dir: str,
    model_name: str,
) -> str:
    """Save a trial config JSON to the lab directory."""
    cfg = copy.deepcopy(base_cfg)

    # Apply trial params
    cfg["tp_atr_mult"] = trial.params["tp_atr_mult"]
    cfg["sl_atr_mult"] = trial.params["sl_atr_mult"]
    cfg["trailing_atr_mult"] = trial.params["trailing_atr_mult"]
    cfg["cooldown_bars"] = trial.params["cooldown_bars"]
    cfg["max_hold_bars"] = trial.params["max_hold_bars"]
    threshold = trial.params["entry_threshold"]
    cfg["entry_threshold"] = threshold
    if "models" in cfg:
        for direction in cfg["models"]:
            cfg["models"][direction]["threshold"] = threshold

    # Add optimization info (new section, won't break existing code)
    cfg["optuna_info"] = {
        "trial_number": trial.number,
        "timestamp": pd.Timestamp.now().isoformat(timespec="seconds"),
        "optimizer": "strategy_optimizer",
        "params": dict(trial.params),
        "metrics": metrics,
    }

    fname = f"{model_name}_trial_{trial.number:04d}.json"
    fpath = os.path.join(output_dir, fname)
    with open(fpath, "w") as f:
        json.dump(cfg, f, indent=4)

    return fpath


# ---------------------------------------------------------------------------
# Main optimizer
# ---------------------------------------------------------------------------


def run_optimization(
    config_path: str,
    n_trials: int = 1000,
    predictions_path: str | None = None,
    ohlcv_path: str | None = None,
) -> tuple[dict, BacktestResult]:
    """Run strategy parameter optimization for a single model.

    Returns:
        Tuple of (best_config, best_result).
    """
    start_time = time.perf_counter()

    # Load base config
    with open(config_path) as f:
        base_cfg = json.load(f)

    model_name = base_cfg.get("nickname", Path(config_path).stem)
    print("=" * 70)
    print(f"STRATEGY PARAMETER OPTIMIZATION: {model_name}")
    print(f"  Config: {config_path}")
    print(f"  Trials: {n_trials}")
    print("=" * 70)

    # Resolve predictions and OHLCV from training_info if not provided
    training_info = base_cfg.get("training_info", {})
    if predictions_path is None:
        predictions_path = training_info.get("oos_predictions", "reports/oos_predictions.csv")
    if ohlcv_path is None:
        ohlcv_path = training_info.get("data", "data/processed/CL_set_06.parquet")

    print(f"  Predictions: {predictions_path}")
    print(f"  OHLCV data: {ohlcv_path}")

    # Load data
    print("\nLoading data...")
    predictions_df = load_predictions(predictions_path)
    ohlcv_df = load_ohlcv(ohlcv_path)
    print(f"  Predictions: {len(predictions_df):,} rows  cols={list(predictions_df.columns)}")
    print(f"  OHLCV: {len(ohlcv_df):,} rows")
    print(f"  Date range: {predictions_df.index.min()} to {predictions_df.index.max()}")

    # Create lab directory for trial configs
    lab_dir = os.path.join("configs", "lab", "ensemble2")
    os.makedirs(lab_dir, exist_ok=True)
    print(f"  Trial configs: {lab_dir}/")

    # Run baseline first
    print("\n--- BASELINE ---")
    baseline_engine = BacktestEngine.from_config(base_cfg)
    baseline_result = baseline_engine.run(predictions_df, ohlcv_df, label="Baseline")
    baseline_metrics = extract_metrics(baseline_result)
    print(f"  PnL: ${baseline_metrics['total_pnl']:,.2f}  "
          f"PF: {baseline_metrics['profit_factor']:.2f}  "
          f"WR: {baseline_metrics['win_rate']:.1%}  "
          f"Trades: {baseline_metrics['trade_count']}  "
          f"DD: ${baseline_metrics['max_drawdown']:,.2f}")

    # Create Optuna study (multi-objective)
    study = optuna.create_study(
        directions=["maximize", "maximize"],  # PF, max_drawdown (less negative = better)
        study_name=f"strategy_opt_{model_name}",
    )

    # Shared cache: objective stores BacktestResult, callback reads it
    results_cache: dict[int, BacktestResult] = {}
    objective = make_objective(base_cfg, predictions_df, ohlcv_df, results_cache)

    # Collect all trial results for CSV
    all_trial_rows: list[dict] = []

    def trial_callback(study: optuna.Study, trial: optuna.trial.FrozenTrial) -> None:
        """Log each trial and save its config."""
        if trial.state != optuna.trial.TrialState.COMPLETE:
            return

        pf, dd = trial.values

        # Retrieve cached result (no re-run needed)
        cached_result = results_cache.pop(trial.number, None)
        if cached_result is not None:
            metrics = extract_metrics(cached_result)
        else:
            # Fallback: re-run if cache miss (shouldn't happen)
            metrics = {"profit_factor": pf, "max_drawdown": dd}

        # Save trial config
        save_trial_config(base_cfg, trial, metrics, lab_dir, model_name.lower().replace(" ", "_"))

        # Build CSV row
        row = {"trial_number": trial.number, **trial.params, **metrics}
        all_trial_rows.append(row)

        # Progress logging every 50 trials
        if (trial.number + 1) % 50 == 0:
            elapsed = time.perf_counter() - start_time
            print(f"  Trial {trial.number + 1}/{n_trials}  "
                  f"PF={pf:.2f}  DD=${dd:,.0f}  "
                  f"[{elapsed:.0f}s elapsed]")

    print(f"\n--- OPTIMIZING ({n_trials} trials) ---")
    study.optimize(
        objective,
        n_trials=n_trials,
        callbacks=[trial_callback],
        show_progress_bar=True,
    )

    elapsed = time.perf_counter() - start_time

    # --- Select best trial (highest PF from Pareto front) ---
    pareto_trials = study.best_trials
    best_trial = max(pareto_trials, key=lambda t: t.values[0])  # highest PF

    print(f"\n--- RESULTS ({elapsed:.0f}s) ---")
    print(f"Pareto-optimal trials: {len(pareto_trials)}")
    print(f"Best trial #{best_trial.number}:")
    print(f"  Params: {dict(best_trial.params)}")
    print(f"  PF={best_trial.values[0]:.4f}  DD=${best_trial.values[1]:,.2f}")

    # Run final backtest with best params for full metrics
    best_cfg = copy.deepcopy(base_cfg)
    best_cfg["tp_atr_mult"] = best_trial.params["tp_atr_mult"]
    best_cfg["sl_atr_mult"] = best_trial.params["sl_atr_mult"]
    best_cfg["trailing_atr_mult"] = best_trial.params["trailing_atr_mult"]
    best_cfg["cooldown_bars"] = best_trial.params["cooldown_bars"]
    best_cfg["max_hold_bars"] = best_trial.params["max_hold_bars"]
    threshold = best_trial.params["entry_threshold"]
    best_cfg["entry_threshold"] = threshold
    if "models" in best_cfg:
        for direction in best_cfg["models"]:
            best_cfg["models"][direction]["threshold"] = threshold

    best_engine = BacktestEngine.from_config(best_cfg)
    best_result = best_engine.run(predictions_df, ohlcv_df, label="Optimized")
    best_metrics = extract_metrics(best_result)

    print(f"\n  OPTIMIZED: PnL=${best_metrics['total_pnl']:,.2f}  "
          f"PF={best_metrics['profit_factor']:.2f}  "
          f"WR={best_metrics['win_rate']:.1%}  "
          f"Trades={best_metrics['trade_count']}  "
          f"DD=${best_metrics['max_drawdown']:,.2f}  "
          f"Sharpe={best_metrics['sharpe_ratio']:.2f}")
    print(f"  BASELINE:  PnL=${baseline_metrics['total_pnl']:,.2f}  "
          f"PF={baseline_metrics['profit_factor']:.2f}  "
          f"WR={baseline_metrics['win_rate']:.1%}  "
          f"Trades={baseline_metrics['trade_count']}  "
          f"DD=${baseline_metrics['max_drawdown']:,.2f}")

    # Save optimized config
    opt_config_name = Path(config_path).stem + "_opt.json"
    opt_config_path = os.path.join(os.path.dirname(config_path), opt_config_name)

    best_cfg["optuna_info"] = {
        "trial_number": best_trial.number,
        "timestamp": pd.Timestamp.now().isoformat(timespec="seconds"),
        "optimizer": "strategy_optimizer",
        "n_trials": n_trials,
        "params": dict(best_trial.params),
        "metrics": best_metrics,
        "baseline_metrics": baseline_metrics,
        "wall_time_seconds": round(elapsed, 1),
    }

    with open(opt_config_path, "w") as f:
        json.dump(best_cfg, f, indent=4)
    print(f"\nSaved optimized config: {opt_config_path}")

    # Save CSV report
    csv_path = f"reports/strategy_optimization_{model_name.lower().replace(' ', '_')}.csv"
    if all_trial_rows:
        csv_df = pd.DataFrame(all_trial_rows)
        csv_df = csv_df.sort_values("profit_factor", ascending=False)
        csv_df.to_csv(csv_path, index=False)
        print(f"Saved trial report: {csv_path} ({len(csv_df)} rows)")

    return best_cfg, best_result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Optuna Strategy Parameter Optimizer — tunes trading params via backtest"
    )
    parser.add_argument(
        "--config", required=True,
        help="Path to base strategy JSON config"
    )
    parser.add_argument(
        "--n-trials", type=int, default=1000,
        help="Number of Optuna trials (default: 1000)"
    )
    parser.add_argument(
        "--predictions", default=None,
        help="Override: path to OOS predictions CSV"
    )
    parser.add_argument(
        "--data", default=None,
        help="Override: path to OHLCV parquet"
    )
    args = parser.parse_args()

    run_optimization(
        config_path=args.config,
        n_trials=args.n_trials,
        predictions_path=args.predictions,
        ohlcv_path=args.data,
    )


if __name__ == "__main__":
    main()

