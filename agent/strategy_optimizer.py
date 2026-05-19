"""
Optuna Strategy Parameter Optimizer for CL_Analyst.

Tunes **trading strategy parameters** (TP/SL multipliers, thresholds,
cooldown, max hold bars, trailing stop) using BacktestEngine against OOS
predictions.  This is distinct from ``optuna_lgbm_search.py`` which tunes
LightGBM *model* hyperparameters.

For TieredEnsembleStrategy configs, BOTH sides (long/short) are optimized
**simultaneously** in a single Optuna trial with asymmetric parameters.
The objective is the Annualized Monthly Sharpe Ratio (Consistency Score)
of the combined portfolio.

Usage:
    python agent/strategy_optimizer.py \\
        --config configs/strategies/manatee2.json \\
        --n-trials 1000

    python agent/strategy_optimizer.py \\
        --config configs/strategies/hourly_ensemble_008.json \\
        --n-trials 500
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

import optuna
from agent.backtest_engine import BacktestEngine, BacktestResult, load_ohlcv, load_predictions
from src.live_execution.strategies.execution_models import create_execution_strategy

optuna.logging.set_verbosity(optuna.logging.WARNING)


# ---------------------------------------------------------------------------
# Telegram progress notifications
# ---------------------------------------------------------------------------

_tg_config: dict | None = None


def _load_telegram_config() -> dict:
    """Lazily load Telegram config from .env or environment variables."""
    global _tg_config
    if _tg_config is not None:
        return _tg_config
    env_vars = {}
    env_path = os.path.join(PROJECT_ROOT, ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    env_vars[key.strip()] = val.strip()
    _tg_config = {
        "token": env_vars.get("TELEGRAM_BOT_TOKEN", os.environ.get("TELEGRAM_BOT_TOKEN", "")),
        "chat_id": env_vars.get("TELEGRAM_CHAT_ID", os.environ.get("TELEGRAM_CHAT_ID", "")),
    }
    return _tg_config


def send_telegram(message: str) -> None:
    """Send a Telegram notification. Silently skips if not configured."""
    import urllib.request
    import urllib.parse
    cfg = _load_telegram_config()
    if not cfg["token"] or not cfg["chat_id"]:
        return
    url = f"https://api.telegram.org/bot{cfg['token']}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": cfg["chat_id"], "text": message}).encode()
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        urllib.request.urlopen(req, timeout=8)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Trade floor penalty — prevents Optuna from gaming the Consistency Score
# with hyper-selective configs that produce only 2-6 trades.
# ---------------------------------------------------------------------------
import math

TRADES_PER_YEAR_FLOOR = 36  # Minimum ~3 trades/month combined (long+short)


def _trade_floor_weight(trade_count: int, trade_floor: float,
                        steepness: float = 6.0) -> float:
    """Smooth penalty multiplier in [0, 1] for the trade floor constraint.

    Provides a TPE-friendly gradient that teaches the sampler to move toward
    higher-activity parameter regions, rather than the uninformative -9999.0
    cliff used for zero-trade trials.

    - trade_count >= trade_floor  →  1.0  (ceiling: no churn reward)
    - trade_count << trade_floor  →  ~0.0 (kills hyper-selective configs)
    - transition zone             →  smooth sigmoid ramp
    """
    if trade_count >= trade_floor:
        return 1.0

    ratio = trade_count / trade_floor
    # Sigmoid centered at 50% of the floor
    raw = 1.0 / (1.0 + math.exp(-steepness * (ratio - 0.5)))
    # Normalize so that ratio=1.0 maps exactly to weight=1.0
    at_floor = 1.0 / (1.0 + math.exp(-steepness * 0.5))
    return raw / at_floor


# ---------------------------------------------------------------------------
# Metrics helpers
# ---------------------------------------------------------------------------
import heapq

class TopKTracker:
    """Tracks the top K configurations by Consistency Score and generates a markdown summary."""
    def __init__(self, k=5, save_dir="configs/strategies/candidates"):
        self.k = k
        self.save_dir = save_dir
        self.top_configs = [] # Min-heap of (score, trial_number, config_dict, metrics)
        self._lock = threading.Lock()
        os.makedirs(self.save_dir, exist_ok=True)

    def add(self, score, trial_number, config, metrics=None):
        with self._lock:
            heapq.heappush(self.top_configs, (score, trial_number, config, metrics or {}))
            if len(self.top_configs) > self.k:
                heapq.heappop(self.top_configs)

    def save_best(self, min_sharpe=0.5):
        with self._lock:
            sorted_configs = sorted(self.top_configs, key=lambda x: x[0], reverse=True)
        
        md_lines = [
            "# Candidate Configurations Summary",
            "",
            "| Rank | File Name | Trial | Sharpe | PnL | Trades | Win Rate | PF |",
            "|---|---|---|---|---|---|---|---|"
        ]

        saved_count = 0
        for rank, (score, trial_num, config, metrics) in enumerate(sorted_configs):
            if score < min_sharpe:
                print(f"[*] Dropping Trial {trial_num} (Score: {score:.2f} < Min: {min_sharpe})")
                continue

            saved_count += 1
            filename = f"Rank_{saved_count}_Trial_{trial_num}_Score_{score:.2f}.json"
            filepath = os.path.join(self.save_dir, filename)
            
            config["_optimization_metadata"] = {
                "rank": saved_count,
                "trial": trial_num,
                "consistency_score": score,
                "performance_metrics": metrics
            }
            with open(filepath, 'w') as f:
                json.dump(config, f, indent=4)
                
            pnl = metrics.get('total_pnl', 0.0)
            trades = metrics.get('trade_count', 0)
            wr = metrics.get('win_rate', 0.0)
            pf = metrics.get('profit_factor', 0.0)
            
            md_lines.append(f"| {saved_count} | `{filename}` | {trial_num} | {score:.2f} | ${pnl:,.2f} | {trades} | {wr:.1f}% | {pf:.2f} |")

        if saved_count > 0:
            summary_path = os.path.join(self.save_dir, "CANDIDATES_SUMMARY.md")
            with open(summary_path, "w") as f:
                f.write("\n".join(md_lines))
                
            print(f"[*] Saved {saved_count} candidate configurations and summary to {self.save_dir}")
        else:
            print(f"[*] No candidate met the min_sharpe hurdle rate of {min_sharpe}.")

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
# Prediction loading helpers
# ---------------------------------------------------------------------------


def _resolve_prob_column(df: pd.DataFrame, keyword: str) -> str | None:
    """Find a column containing `keyword` (case-insensitive)."""
    kw = keyword.lower()
    for col in df.columns:
        if kw in col.lower():
            return col
    return None


def _load_ensemble_predictions(base_cfg: dict) -> pd.DataFrame:
    """Merge long + short OOS predictions from the models block."""
    models = base_cfg.get("models", {})
    long_path = models.get("long", {}).get("predictions_path")
    short_path = models.get("short", {}).get("predictions_path")

    dfs = {}
    if long_path and os.path.exists(long_path):
        long_df = load_predictions(long_path)
        col = _resolve_prob_column(long_df, "buy")
        if col:
            dfs["prob_Buy"] = long_df[col]
    if short_path and os.path.exists(short_path):
        short_df = load_predictions(short_path)
        col = _resolve_prob_column(short_df, "sell")
        if col:
            dfs["prob_Sell"] = short_df[col]

    if not dfs:
        raise FileNotFoundError(
            f"Cannot load ensemble predictions: long={long_path}, short={short_path}"
        )
    merged = pd.DataFrame(dfs).fillna(0.0)
    return merged


# ---------------------------------------------------------------------------
# Optuna objective
# ---------------------------------------------------------------------------


def make_objective(
    base_cfg: dict,
    predictions_df: pd.DataFrame,
    ohlcv_df: pd.DataFrame,
    results_cache: dict | None = None,
    tracker: "TopKTracker | None" = None,
):
    """Create a closure that Optuna can call with trial params.

    Simultaneous mode: suggests asymmetric parameters for BOTH long and
    short sides in every trial.  Runs a single ensemble backtest and
    returns the Annualized Monthly Sharpe Ratio (Consistency Score).

    Args:
        base_cfg: Base strategy config dict.
        predictions_df: OOS predictions DataFrame.
        ohlcv_df: OHLCV DataFrame.
        results_cache: Optional dict to store BacktestResult per trial.
        tracker: Optional TopKTracker to collect top configs.
    """
    # Pre-create a strategy instance for parameter routing
    strategy = create_execution_strategy(base_cfg)
    is_tiered = (
        base_cfg.get("execution_class") == "TieredEnsembleStrategy"
        and base_cfg.get("long", {}).get("tiers")
        and base_cfg.get("short", {}).get("tiers")
    )

    # Compute trade floor from prediction data span (once, not per trial)
    _backtest_years = (predictions_df.index.max() - predictions_df.index.min()).days / 365.25
    _trade_floor = max(1.0, TRADES_PER_YEAR_FLOOR * _backtest_years)

    def _suggest_side_params(trial: optuna.Trial, suffix: str) -> dict:
        """Suggest params for one side with the given suffix."""
        params = {
            "tp_atr_mult": trial.suggest_float(f"tp_atr_mult_{suffix}", 1.5, 10.0, step=0.25),
            "sl_atr_mult": trial.suggest_float(f"sl_atr_mult_{suffix}", 0.5, 4.5, step=0.25),
            "trailing_atr_mult": trial.suggest_float(f"trailing_atr_mult_{suffix}", 0.5, 5.0, step=0.25),
            "trailing_sl_atr_offset": trial.suggest_float(f"trailing_sl_atr_offset_{suffix}", 1.0, 5.0, step=0.5),
            "cooldown_bars": trial.suggest_int(f"cooldown_bars_{suffix}", 1, 21, step=2),
            "max_hold_bars": trial.suggest_int(f"max_hold_bars_{suffix}", 24, 240, step=24),
            "consecutive_signal_threshold": trial.suggest_int(f"consecutive_signal_threshold_{suffix}", 0, 4, step=1),
            "entry_threshold": trial.suggest_float(f"entry_threshold_{suffix}", 0.50, 0.80, step=0.01),
            "atr_period": trial.suggest_int(f"atr_period_{suffix}", 10, 40, step=2),
        }
        return params

    def objective(trial: optuna.Trial) -> float:
        cfg = copy.deepcopy(base_cfg)

        if is_tiered:
            # Simultaneous: suggest asymmetric params for both sides
            long_params = _suggest_side_params(trial, "long")
            short_params = _suggest_side_params(trial, "short")
            strategy.apply_trial_params(cfg, long_params, side="long")
            strategy.apply_trial_params(cfg, short_params, side="short")
        else:
            # Non-tiered: single set of params
            params = {
                "tp_atr_mult": trial.suggest_float("tp_atr_mult", 1.5, 10.0, step=0.25),
                "sl_atr_mult": trial.suggest_float("sl_atr_mult", 0.5, 4.5, step=0.25),
                "trailing_atr_mult": trial.suggest_float("trailing_atr_mult", 0.5, 5.0, step=0.25),
                "trailing_sl_atr_offset": trial.suggest_float("trailing_sl_atr_offset", 1.0, 5.0, step=0.5),
                "cooldown_bars": trial.suggest_int("cooldown_bars", 1, 21, step=2),
                "max_hold_bars": trial.suggest_int("max_hold_bars", 24, 240, step=24),
                "consecutive_signal_threshold": trial.suggest_int("consecutive_signal_threshold", 0, 4, step=1),
                "entry_threshold": trial.suggest_float("entry_threshold", 0.50, 0.80, step=0.01),
                "atr_period": trial.suggest_int("atr_period", 10, 40, step=2),
            }
            if base_cfg.get("execution_class") == "BreakoutStraddleStrategy":
                params["breakout_window"] = trial.suggest_int("breakout_window", 2, 24, step=2)
            strategy.apply_trial_params(cfg, params)

        engine = BacktestEngine.from_config(cfg)
        result = engine.run(predictions_df, ohlcv_df)

        if results_cache is not None:
            results_cache[trial.number] = result

        # --- Consistency Score: Annualized Monthly Sharpe ---
        if result.trade_count == 0 or not result.trades:
            return -9999.0

        trade_records = []
        for t in result.trades:
            trade_records.append({"exit_dt": t.exit_dt, "pnl": t.net_pnl_dollars})
        trades_df = pd.DataFrame(trade_records)
        trades_df["exit_dt"] = pd.to_datetime(trades_df["exit_dt"])
        trades_df = trades_df.set_index("exit_dt").sort_index()

        monthly_pnls = trades_df["pnl"].resample("ME").sum().dropna()

        monthly_pnl_vals = monthly_pnls.values
        std_pnl = float(np.std(monthly_pnl_vals))

        if len(monthly_pnl_vals) == 0 or std_pnl < 1e-9:
            return -9999.0

        annualized_sharpe = float(
            (np.mean(monthly_pnl_vals) / std_pnl) * np.sqrt(12)
        )

        # --- Trade Floor Penalty ---
        # Negative Sharpe returned as-is (multiplying by weight < 1 would
        # *improve* a negative score — the opposite of the intended effect).
        if annualized_sharpe > 0:
            weight = _trade_floor_weight(result.trade_count, _trade_floor)
            final_score = annualized_sharpe * weight
        else:
            final_score = annualized_sharpe

        # Track top configs (penalized score so ranking matches Optuna)
        if tracker is not None:
            metrics = extract_metrics(result)
            tracker.add(final_score, trial.number, cfg, metrics=metrics)

        return final_score

    return objective





# ---------------------------------------------------------------------------
# Main optimizer
# ---------------------------------------------------------------------------


def run_optimization(
    config_path: str,
    n_trials: int = 1000,
    predictions_path: str | None = None,
    ohlcv_path: str | None = None,
    holdout_months: int | None = None,
    n_jobs: int = 1,
    quiet: bool = False,
    label: str = "",
) -> tuple[dict, BacktestResult]:
    """Run strategy parameter optimization.

    For TieredEnsembleStrategy configs, BOTH sides are optimized
    simultaneously in a single study.  The objective is the Annualized
    Monthly Sharpe Ratio (Consistency Score) of the combined portfolio.

    Args:
        holdout_months: If set, reserve the last N months of predictions
            as an unseen holdout.  Optuna only sees data before the cutoff.
            Falls back to config key ``holdout_months`` when *None*.

    Returns:
        Tuple of (best_config, best_result).  Holdout metrics (if any)
        are stored in ``best_config["optuna_info"]["holdout_metrics"]``.
    """
    start_time = time.perf_counter()

    with open(config_path) as f:
        base_cfg = json.load(f)

    model_name = base_cfg.get("nickname", Path(config_path).stem)
    if label:
        model_name = f"{label} | {model_name}"
    is_tiered = (
        base_cfg.get("execution_class") == "TieredEnsembleStrategy"
        and base_cfg.get("long", {}).get("tiers")
        and base_cfg.get("short", {}).get("tiers")
    )

    print("=" * 70)
    print(f"STRATEGY PARAMETER OPTIMIZATION: {model_name}")
    print(f"  Config: {config_path}")
    print(f"  Trials: {n_trials}")
    print(f"  Mode: {'SIMULTANEOUS ENSEMBLE' if is_tiered else 'SINGLE CONFIG'}")
    print(f"  Objective: Annualized Monthly Sharpe (Consistency Score)")
    print("=" * 70)

    # Resolve OHLCV
    training_info = base_cfg.get("training_info", {})
    if ohlcv_path is None:
        ohlcv_path = training_info.get("data", "data/processed/CL_set_06.parquet")

    # Resolve predictions — ensemble configs need merging from per-model files
    if predictions_path is not None:
        predictions_df = load_predictions(predictions_path)
    elif "models" in base_cfg:
        try:
            predictions_df = _load_ensemble_predictions(base_cfg)
            predictions_path = "<merged from models block>"
        except FileNotFoundError:
            predictions_path = training_info.get("oos_predictions", "reports/oos_predictions.csv")
            predictions_df = load_predictions(predictions_path)
    else:
        predictions_path = training_info.get("oos_predictions", "reports/oos_predictions.csv")
        predictions_df = load_predictions(predictions_path)

    print(f"  Predictions: {predictions_path}")
    print(f"  OHLCV data: {ohlcv_path}")

    print("\nLoading data...")
    ohlcv_df = load_ohlcv(ohlcv_path)
    print(f"  Predictions: {len(predictions_df):,} rows  cols={list(predictions_df.columns)}")
    print(f"  OHLCV: {len(ohlcv_df):,} rows")
    print(f"  Date range: {predictions_df.index.min()} to {predictions_df.index.max()}")

    # ── Holdout split ─────────────────────────────────────────────────
    _holdout_months = holdout_months if holdout_months is not None else base_cfg.get("holdout_months", 0)
    holdout_preds = None
    holdout_cutoff = None
    if _holdout_months > 0:
        pred_end = predictions_df.index.max()
        holdout_cutoff = pred_end - pd.DateOffset(months=_holdout_months)
        holdout_preds = predictions_df[predictions_df.index >= holdout_cutoff].copy()
        predictions_df = predictions_df[predictions_df.index < holdout_cutoff].copy()
        print(f"  Holdout:  {_holdout_months} months reserved "
              f"({holdout_cutoff.date()} -> {pred_end.date()}, "
              f"{len(holdout_preds):,} bars)")
        print(f"  Optimizer window: "
              f"{predictions_df.index.min().date()} -> {predictions_df.index.max().date()} "
              f"({len(predictions_df):,} bars)")

    # Run baseline
    print("\n--- BASELINE ---")
    baseline_engine = BacktestEngine.from_config(base_cfg)
    baseline_result = baseline_engine.run(predictions_df, ohlcv_df, label="Baseline")
    baseline_metrics = extract_metrics(baseline_result)
    print(f"  PnL: ${baseline_metrics['total_pnl']:,.2f}  "
          f"PF: {baseline_metrics['profit_factor']:.2f}  "
          f"WR: {baseline_metrics['win_rate']:.1%}  "
          f"Trades: {baseline_metrics['trade_count']}  "
          f"DD: ${baseline_metrics['max_drawdown']:,.2f}")

    # ── Unified optimization (simultaneous for tiered, single for others) ─
    results_cache: dict[int, BacktestResult] = {}
    tracker = TopKTracker(k=5, save_dir="configs/strategies/candidates")
    objective = make_objective(base_cfg, predictions_df, ohlcv_df, results_cache, tracker=tracker)

    study = optuna.create_study(
        direction="maximize",
        study_name=f"strategy_opt_{model_name}",
    )

    def trial_callback(study_: optuna.Study, trial: optuna.trial.FrozenTrial) -> None:
        if trial.state != optuna.trial.TrialState.COMPLETE:
            return
        completed = trial.number + 1
        if completed % 50 == 0:
            elapsed = time.perf_counter() - start_time
            print(f"  Trial {completed}/{n_trials}  "
                  f"Sharpe={trial.value:.3f}  [{elapsed:.0f}s]")

    print(f"\n--- OPTIMIZING ({n_trials} trials, n_jobs={n_jobs}) ---")
    send_telegram(
        f"[Strategy Optimizer] {model_name}\n"
        f"Started: {n_trials} trials (n_jobs={n_jobs})"
    )
    try:
        study.optimize(
            objective, n_trials=n_trials, n_jobs=n_jobs,
            callbacks=[trial_callback],
            show_progress_bar=(n_jobs == 1 and not quiet),
        )
    except KeyboardInterrupt:
        print("\n  [!] Optimization interrupted by user.")
    finally:
        tracker.save_best()

    elapsed = time.perf_counter() - start_time
    best_trial = study.best_trial

    print(f"\n--- RESULTS ({elapsed:.0f}s) ---")
    print(f"Best trial #{best_trial.number}: Sharpe={best_trial.value:.4f}")
    print(f"  Params: {dict(best_trial.params)}")

    # End notification with best performance
    best_result = results_cache.get(best_trial.number)
    if best_result:
        best_m = extract_metrics(best_result)
        send_telegram(
            f"[Strategy Optimizer] {model_name}\n"
            f"COMPLETE ({elapsed:.0f}s / {elapsed/60:.1f}m)\n"
            f"Best Trial: #{best_trial.number}/{n_trials}\n"
            f"Best Sharpe: {best_trial.value:.4f}\n"
            f"PnL: ${best_m['total_pnl']:,.2f}  PF: {best_m['profit_factor']:.2f}\n"
            f"Trades: {best_m['trade_count']}  WR: {best_m['win_rate']:.1%}\n"
            f"DD: ${best_m['max_drawdown']:,.2f}"
        )
    else:
        send_telegram(
            f"[Strategy Optimizer] {model_name}\n"
            f"COMPLETE ({elapsed:.0f}s / {elapsed/60:.1f}m)\n"
            f"Best Trial: #{best_trial.number}/{n_trials}\n"
            f"Best Sharpe: {best_trial.value:.4f}"
        )

    # Reconstruct best config from best trial params
    best_cfg = copy.deepcopy(base_cfg)
    strategy = create_execution_strategy(best_cfg)
    if is_tiered:
        # Split suffixed params back into per-side dicts
        long_params = {k.replace("_long", ""): v for k, v in best_trial.params.items() if k.endswith("_long")}
        short_params = {k.replace("_short", ""): v for k, v in best_trial.params.items() if k.endswith("_short")}
        strategy.apply_trial_params(best_cfg, long_params, side="long")
        strategy.apply_trial_params(best_cfg, short_params, side="short")
    else:
        strategy.apply_trial_params(best_cfg, dict(best_trial.params))

    # Final ensemble backtest with best params
    best_engine = BacktestEngine.from_config(best_cfg)
    best_result = best_engine.run(predictions_df, ohlcv_df, label="Optimized_Ensemble")
    best_metrics = extract_metrics(best_result)

    print(f"\n  OPTIMIZED: PnL=${best_metrics['total_pnl']:,.2f}  "
          f"PF={best_metrics['profit_factor']:.2f}  "
          f"WR={best_metrics['win_rate']:.1%}  "
          f"Trades={best_metrics['trade_count']}  "
          f"DD=${best_metrics['max_drawdown']:,.2f}")
    print(f"  BASELINE:  PnL=${baseline_metrics['total_pnl']:,.2f}  "
          f"PF={baseline_metrics['profit_factor']:.2f}  "
          f"WR={baseline_metrics['win_rate']:.1%}  "
          f"Trades={baseline_metrics['trade_count']}  "
          f"DD=${baseline_metrics['max_drawdown']:,.2f}")

    # Build optuna_info with both per-side and ensemble details
    if is_tiered:
        best_cfg["optuna_info"] = {
            "trial_number": best_trial.number,
            "timestamp": pd.Timestamp.now().isoformat(timespec="seconds"),
            "optimizer": "strategy_optimizer",
            "mode": "simultaneous_ensemble",
            "n_trials": n_trials,
            "long_params": long_params,
            "short_params": short_params,
            "params": dict(best_trial.params),
            "consistency_score": best_trial.value,
            "ensemble_metrics": best_metrics,
            "baseline_metrics": baseline_metrics,
            "wall_time_seconds": round(elapsed, 1),
        }
    else:
        best_cfg["optuna_info"] = {
            "trial_number": best_trial.number,
            "timestamp": pd.Timestamp.now().isoformat(timespec="seconds"),
            "optimizer": "strategy_optimizer",
            "mode": "single_config",
            "n_trials": n_trials,
            "params": dict(best_trial.params),
            "consistency_score": best_trial.value,
            "metrics": best_metrics,
            "baseline_metrics": baseline_metrics,
            "wall_time_seconds": round(elapsed, 1),
        }

    # ── Holdout backtest (unseen by Optuna) ────────────────────────────
    if holdout_preds is not None and len(holdout_preds) > 0:
        print(f"\n--- HOLDOUT BACKTEST ({_holdout_months} months, unseen by optimizer) ---")
        holdout_engine = BacktestEngine.from_config(best_cfg)
        holdout_result = holdout_engine.run(holdout_preds, ohlcv_df, label="Holdout")
        holdout_metrics = extract_metrics(holdout_result)
        best_cfg["optuna_info"]["holdout_metrics"] = holdout_metrics
        best_cfg["optuna_info"]["holdout_months"] = _holdout_months
        best_cfg["optuna_info"]["holdout_cutoff"] = holdout_cutoff.isoformat()
        print(f"  HOLDOUT:   PnL=${holdout_metrics['total_pnl']:,.2f}  "
              f"PF={holdout_metrics['profit_factor']:.2f}  "
              f"WR={holdout_metrics['win_rate']:.1%}  "
              f"Trades={holdout_metrics['trade_count']}  "
              f"DD=${holdout_metrics['max_drawdown']:,.2f}")
    elif _holdout_months > 0:
        print("\n  WARNING: Holdout period has 0 prediction rows — skipping holdout backtest.")

    # Save optimized config
    if _holdout_months > 0:
        best_cfg["holdout_months"] = _holdout_months
    opt_config_name = Path(config_path).stem + "_opt.json"
    opt_config_path = os.path.join(os.path.dirname(config_path), opt_config_name)
    with open(opt_config_path, "w") as f:
        json.dump(best_cfg, f, indent=4)
    print(f"\nSaved optimized config: {opt_config_path}")

    # Extract standalone side configs
    if is_tiered:
        for side in ["long", "short"]:
            side_cfg = copy.deepcopy(best_cfg)
            # Remove the other side's models
            if "models" in side_cfg:
                other_side = "short" if side == "long" else "long"
                if other_side in side_cfg["models"]:
                    del side_cfg["models"][other_side]
            
            # Disable the other side in tiered setup by setting threshold to 1.0
            other_side_key = "short" if side == "long" else "long"
            if other_side_key in side_cfg:
                if "tiers" in side_cfg[other_side_key]:
                    for tier in side_cfg[other_side_key]["tiers"]:
                        tier["min_prob"] = 1.0

            side_config_name = Path(config_path).stem + f"_opt_{side}.json"
            side_config_path = os.path.join(os.path.dirname(config_path), side_config_name)
            with open(side_config_path, "w") as f:
                json.dump(side_cfg, f, indent=4)
            print(f"Saved {side}-only config: {side_config_path}")

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
    parser.add_argument(
        "--holdout-months", type=int, default=None,
        help="Reserve last N months of predictions as unseen holdout "
             "(overrides config holdout_months)"
    )
    parser.add_argument(
        "--jobs", type=int, default=1,
        help="Number of parallel Optuna trial evaluations (default: 1)"
    )
    args = parser.parse_args()

    run_optimization(
        config_path=args.config,
        n_trials=args.n_trials,
        predictions_path=args.predictions,
        ohlcv_path=args.data,
        holdout_months=args.holdout_months,
        n_jobs=args.jobs,
    )


if __name__ == "__main__":
    main()
