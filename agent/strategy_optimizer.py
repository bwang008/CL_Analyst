"""
Optuna Strategy Parameter Optimizer for CL_Analyst.

Tunes **trading strategy parameters** (TP/SL multipliers, thresholds,
cooldown, max hold bars, trailing stop) using BacktestEngine against OOS
predictions.  This is distinct from ``optuna_lgbm_search.py`` which tunes
LightGBM *model* hyperparameters.

Supports three optimization modes:
  - **simultaneous ensemble**: optimizes BOTH sides in a single trial
  - **single-side (long/short)**: optimizes one side only, disabling the other
  - **single config**: for non-tiered strategies

Supports two objective functions:
  - **sharpe**: Annualized Monthly Sharpe Ratio (default)
  - **sortino**: Annualized Monthly Sortino Ratio (downside deviation only)

Usage:
    python agent/strategy_optimizer.py \\
        --config configs/strategies/manatee2.json \\
        --n-trials 1000

    python agent/strategy_optimizer.py \\
        --config configs/strategies/hourly_ensemble_008.json \\
        --n-trials 500 --objective sortino --side long
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Any

import logging


import warnings
warnings.filterwarnings("ignore", category=UserWarning, module=".*execution_models")

import numpy as np
import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

import optuna
from agent.backtest_engine import BacktestEngine, BacktestResult, load_ohlcv, load_ohlcv_dual, load_predictions
from src.live_execution.strategies.execution_models import create_execution_strategy

optuna.logging.set_verbosity(optuna.logging.WARNING)

# ---------------------------------------------------------------------------
# Optional vectorbt import — used only by Stage 1 prescreener
# ---------------------------------------------------------------------------
try:
    import vectorbt as vbt
    _VBT_AVAILABLE = True
except ImportError:
    _VBT_AVAILABLE = False

# ---------------------------------------------------------------------------
# Per-objective seed offsets — ensures different objectives explore different
# parameter spaces even when the base random_seed is identical.
# ---------------------------------------------------------------------------
_OBJECTIVE_SEED_OFFSETS = {
    "sharpe": 0,
    "sortino": 1,
    "block_min": 2,
    "block_median": 3,
    "block_mean_std": 4,
}


# ---------------------------------------------------------------------------
# Best result tracking — O(1) memory replacement for unbounded results_cache
# ---------------------------------------------------------------------------

@dataclass
class BestResultTracker:
    """Tracks the single best trial result during optimization.

    Replaces the previous ``dict[int, BacktestResult]`` cache which stored
    every trial's full result (equity curves, trade lists) and grew
    linearly with trial count, causing OOM at ~1500 trials × 14 workers.
    """
    score: float = float("-inf")
    trial_number: int = -1
    result: Optional[Any] = field(default=None, repr=False)  # BacktestResult

    def update_if_better(self, new_score: float, trial_number: int, result: Any) -> bool:
        """Update the tracked best if ``new_score`` exceeds the current best."""
        if new_score > self.score:
            self.score = new_score
            self.trial_number = trial_number
            self.result = result
            return True
        return False


# ---------------------------------------------------------------------------
# Telegram progress notifications
# ---------------------------------------------------------------------------

_tg_config: dict | None = None
_tg_last_send: float = 0.0  # monotonic timestamp of last successful send
_tg_lock = threading.Lock()  # serialize sends within a process
_tg_suppress = False  # when True, send_telegram is a no-op (batch mode)
_TG_MIN_GAP = 0.5  # minimum seconds between sends (Telegram limit: 1/sec)


def suppress_telegram(suppress: bool = True) -> None:
    """Toggle per-worker Telegram suppression for batch mode."""
    global _tg_suppress
    _tg_suppress = suppress


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
    """Send a Telegram notification with rate-limiting and retry.

    Enforces a minimum 0.5s gap between sends to stay under Telegram's
    1 msg/sec/chat limit.  On 429 (Too Many Requests), retries after the
    server-specified delay (up to 2 retries).

    Silently skips if not configured or if ``suppress_telegram(True)``
    has been called (used by batch_post_optimizer to silence per-worker
    start/complete spam).
    """
    print(f"\n[Telegram Output]\n{message}\n", flush=True)
    if _tg_suppress:
        return
    import urllib.request
    import urllib.parse
    cfg = _load_telegram_config()
    if not cfg["token"] or not cfg["chat_id"]:
        return
    url = f"https://api.telegram.org/bot{cfg['token']}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": cfg["chat_id"], "text": message}).encode()

    global _tg_last_send
    max_retries = 2
    for attempt in range(max_retries + 1):
        with _tg_lock:
            # Enforce minimum gap between sends
            now = time.monotonic()
            gap = _TG_MIN_GAP - (now - _tg_last_send)
            if gap > 0:
                time.sleep(gap)
            try:
                req = urllib.request.Request(url, data=data, method="POST")
                urllib.request.urlopen(req, timeout=8)
                _tg_last_send = time.monotonic()
                return  # success
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < max_retries:
                    # Parse retry_after from response
                    try:
                        import json as _json
                        body = _json.loads(e.read().decode())
                        retry_after = body.get("parameters", {}).get("retry_after", 1)
                    except Exception:
                        retry_after = 1
                    _tg_last_send = time.monotonic()
                    time.sleep(retry_after)
                    continue
                return  # give up silently
            except Exception:
                return  # network error, give up silently


# ---------------------------------------------------------------------------
# Trade floor penalty — prevents Optuna from gaming the Consistency Score
# with hyper-selective configs that produce only 2-6 trades.
# ---------------------------------------------------------------------------
import math

TRADES_PER_YEAR_FLOOR = 100  # Minimum ~8 trades/month combined (long+short)
TRADES_PER_YEAR_FLOOR_SINGLE = 50  # Minimum ~4 trades/month for single-side optimization

# Ceiling on the annualized Sharpe/Sortino optimization OBJECTIVE (not the displayed
# metric). Low-trade configs have a tiny downside_dev -> exploding ratio; this cap stops
# that becoming an unbounded reward. Lowering it makes the trade-floor penalty more
# dominant: a low-trade config's penalized score (cap * floor_weight) must exceed a real
# high-trade config's score to win — at cap=5 it usually no longer can. (Was 10.0.)
OBJECTIVE_SCORE_CAP = 5.0


def _trade_floor_weight(trade_count: int, trade_floor: float,
                        steepness: float = 6.0) -> float:
    """Smooth penalty multiplier in [0, 1] for the trade floor constraint.

    Provides a TPE-friendly gradient that teaches the sampler to move toward
    higher-activity parameter regions, rather than the uninformative -9999.0
    cliff used for zero-trade trials.

    - trade_count >= trade_floor  ->  1.0  (ceiling: no churn reward)
    - trade_count << trade_floor  ->  ~0.0 (kills hyper-selective configs)
    - transition zone             ->  smooth sigmoid ramp
    """
    if trade_count >= trade_floor:
        return 1.0

    ratio = trade_count / trade_floor
    # Sigmoid centered at 50% of the floor
    raw = 1.0 / (1.0 + math.exp(-steepness * (ratio - 0.5)))
    # Normalize so that ratio=1.0 maps exactly to weight=1.0
    at_floor = 1.0 / (1.0 + math.exp(-steepness * 0.5))
    return raw / at_floor


def _apply_trade_floor_penalty(raw_score: float, trade_count: int, trade_floor: float) -> float:
    """Apply the trade-floor penalty multiplier to a raw objective score."""
    if raw_score > 0:
        weight = _trade_floor_weight(trade_count, trade_floor)
        return raw_score * weight
    return raw_score


# ---------------------------------------------------------------------------
# Block-wise Sharpe objective — partition the in-sample monthly PnL series
# into contiguous calendar blocks and aggregate per-block Sharpes. Rewards
# consistency across regimes instead of one lucky stretch.
# ---------------------------------------------------------------------------

BLOCK_OBJECTIVE_METRICS = {"block_min", "block_median", "block_mean_std"}


def _monthly_pnl_series(result: "BacktestResult") -> pd.Series:
    """Monthly PnL series from a backtest result (``resample('M').sum()``)."""
    if not getattr(result, "trades", None):
        return pd.Series(dtype=float)
    trades_df = pd.DataFrame(
        [{"exit_dt": t.exit_dt, "pnl": t.net_pnl_dollars} for t in result.trades]
    )
    trades_df["exit_dt"] = pd.to_datetime(trades_df["exit_dt"])
    trades_df = trades_df.set_index("exit_dt").sort_index()
    return trades_df["pnl"].resample("M").sum().dropna()


def _block_sharpe_details(
    monthly_pnls: pd.Series,
    window_start,
    window_end,
    n_blocks: int,
) -> dict:
    """Per-block annualized monthly Sharpes over a fixed calendar partition.

    The partition covers the FULL calendar month range of
    [window_start, window_end] (the month containing window_end included),
    NOT the trade span — a config that only trades part of the window must
    not get its blocks squeezed into its active period. Months without
    trades count as 0 PnL. Remainder months go to the EARLIEST blocks
    (42 -> 14/14/14, 41 -> 14/14/13, 43 -> 15/14/14).

    Returns {"block_sharpes": [...], "block_bounds": [(start, end), ...]}
    where each block Sharpe is clipped to +/-OBJECTIVE_SCORE_CAP and a
    degenerate block (no trades, all-zero, or std < 1e-9) scores 0.0.
    """
    start_p = pd.Timestamp(window_start).to_period("M")
    end_p = pd.Timestamp(window_end).to_period("M")
    full_range = pd.period_range(start_p, end_p, freq="M")
    n_months = len(full_range)
    if n_months < n_blocks:
        raise ValueError(
            f"Cannot partition {n_months} calendar month(s) "
            f"({start_p} .. {end_p}) into {n_blocks} blocks."
        )

    if len(monthly_pnls) > 0:
        vals = pd.Series(
            np.asarray(monthly_pnls.values, dtype=float),
            index=pd.DatetimeIndex(monthly_pnls.index).to_period("M"),
        )
        vals = vals.reindex(full_range, fill_value=0.0)
    else:
        vals = pd.Series(0.0, index=full_range)

    base, rem = divmod(n_months, n_blocks)
    sizes = [base + 1 if i < rem else base for i in range(n_blocks)]

    block_sharpes: list[float] = []
    block_bounds: list[tuple[str, str]] = []
    pos = 0
    for size in sizes:
        block_periods = full_range[pos:pos + size]
        block_vals = vals.iloc[pos:pos + size].to_numpy(dtype=float)
        pos += size
        std = float(np.std(block_vals))
        if std < 1e-9:
            sharpe = 0.0  # empty / all-zero / constant block — never the cap
        else:
            sharpe = float((np.mean(block_vals) / std) * np.sqrt(12))
            sharpe = float(np.clip(sharpe, -OBJECTIVE_SCORE_CAP, OBJECTIVE_SCORE_CAP))
        block_sharpes.append(sharpe)
        block_bounds.append((
            block_periods[0].start_time.strftime("%Y-%m-%d"),
            block_periods[-1].end_time.strftime("%Y-%m-%d"),
        ))

    return {"block_sharpes": block_sharpes, "block_bounds": block_bounds}


def _block_sharpe_score(
    monthly_pnls: pd.Series,
    window_start,
    window_end,
    n_blocks: int,
    metric: str,
    lambda_dispersion: float,
) -> float:
    """Aggregate the per-block Sharpes into a single objective value.

    block_min      -> min(block Sharpes)
    block_median   -> median(block Sharpes)
    block_mean_std -> mean - lambda_dispersion * population std
    Signed values everywhere — no harmonic/geometric means.
    """
    if metric not in BLOCK_OBJECTIVE_METRICS:
        raise ValueError(
            f"Unknown block metric {metric!r}; valid: {sorted(BLOCK_OBJECTIVE_METRICS)}"
        )
    details = _block_sharpe_details(
        monthly_pnls=monthly_pnls,
        window_start=window_start,
        window_end=window_end,
        n_blocks=n_blocks,
    )
    sharpes = np.asarray(details["block_sharpes"], dtype=float)
    if metric == "block_min":
        return float(sharpes.min())
    if metric == "block_median":
        return float(np.median(sharpes))
    return float(sharpes.mean() - lambda_dispersion * sharpes.std())


def _check_block_window(
    objective_metric: str,
    window_start,
    window_end,
    insample_months: int,
    n_blocks: int,
    min_block_months: int,
) -> None:
    """Hard-raise when the post-holdout in-sample window is too small for a
    block metric (no silent fallback to sharpe — house rule)."""
    required_months = n_blocks * min_block_months
    if insample_months >= required_months:
        return
    base, rem = divmod(max(insample_months, 0), n_blocks)
    layout = "/".join(str(base + 1 if i < rem else base) for i in range(n_blocks))
    raise ValueError(
        f"Block objective '{objective_metric}' needs >= {required_months} in-sample "
        f"months (n_blocks={n_blocks} x min_block_months={min_block_months}), but the "
        f"post-holdout window {pd.Timestamp(window_start).date()} -> "
        f"{pd.Timestamp(window_end).date()} has only {insample_months} "
        f"(block layout would be {layout}). No fallback to sharpe — widen the "
        f"window or lower the holdout."
    )


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
    """Annualized Sortino ratio (target downside deviation, target=0)."""
    if len(equity_curve) < 2:
        return 0.0
    returns = np.diff(equity_curve)
    downside_sq = np.minimum(0, returns) ** 2
    downside_dev = float(np.sqrt(np.mean(downside_sq)))
    if downside_dev < 1e-9:
        return float("inf") if np.mean(returns) > 0 else 0.0
    return float(np.mean(returns) / downside_dev * np.sqrt(bars_per_year))


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
            "buy_trades": 0, "sell_trades": 0,
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
        "buy_trades": sum(1 for t in trades if getattr(t, 'side', 0) == 1),
        "sell_trades": sum(1 for t in trades if getattr(t, 'side', 0) == -1),
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


def build_atr_cache(
    ohlcv_df: pd.DataFrame,
    periods: list[int],
) -> dict[int, pd.Series]:
    """Pre-compute ATR series for each period in `periods`.

    The ATR is the simple rolling mean of True Range (matching BacktestEngine).
    Returns a dict {period: atr_series} so Stage 1 (vectorbt prescreener) can
    look up a pre-built series instead of recomputing it per grid combo.

    NOTE: For Stage 2 (Optuna + BacktestEngine), use attach_atr_cache() instead
    — it stamps ATR_{period} columns directly onto the OHLCV DataFrame so
    BacktestEngine.run() can short-circuit its internal recomputation.

    Args:
        ohlcv_df: OHLCV DataFrame with columns High, Low, Close (DateTime index).
        periods:  List of ATR period integers to pre-compute.

    Returns:
        Dict mapping period -> ATR pd.Series (same index as ohlcv_df).
    """
    tr = np.maximum(
        ohlcv_df["High"] - ohlcv_df["Low"],
        np.maximum(
            (ohlcv_df["High"] - ohlcv_df["Close"].shift(1)).abs(),
            (ohlcv_df["Low"] - ohlcv_df["Close"].shift(1)).abs(),
        ),
    )
    return {p: tr.rolling(p).mean() for p in periods}


def attach_atr_cache(
    ohlcv_df: pd.DataFrame,
    periods: list[int] | None = None,
) -> pd.DataFrame:
    """Pre-compute ATR columns and attach them directly to the OHLCV DataFrame.

    Stamps columns named ATR_{period} onto ohlcv_df (in-place) for every period
    in `periods`. BacktestEngine.run() detects these columns and uses them as a
    fast-path, completely eliminating per-trial ATR recomputation from the Optuna
    hot loop. Trials that would have computed ATR_14, ATR_20, etc. via a rolling
    mean now just index into a pre-built numpy array.

    Zero fidelity loss: the ATR formula is identical to BacktestEngine's own
    computation (simple rolling mean of True Range). The values are computed once
    here, and the engine's fallback path remains intact for any period not cached.

    Args:
        ohlcv_df: OHLCV DataFrame with columns High, Low, Close (DateTime index).
                  Modified in-place; also returned for chaining.
        periods:  ATR periods to pre-compute. Defaults to range(10, 52, 2),
                  covering every period the Optuna search space can suggest.

    Returns:
        The same DataFrame with ATR_{period} columns added.
    """
    if periods is None:
        periods = list(range(4, 44, 4))
    tr = np.maximum(
        ohlcv_df["High"] - ohlcv_df["Low"],
        np.maximum(
            (ohlcv_df["High"] - ohlcv_df["Close"].shift(1)).abs(),
            (ohlcv_df["Low"] - ohlcv_df["Close"].shift(1)).abs(),
        ),
    )
    for p in periods:
        col = f"ATR_{p}"
        if col not in ohlcv_df.columns:          # skip if already stamped
            ohlcv_df[col] = tr.rolling(p).mean()
    return ohlcv_df


# ---------------------------------------------------------------------------
# Stage 1 grid constants (used by run_vbt_prescreener)
# ---------------------------------------------------------------------------

_VBT_ENTRY_THRESHOLDS   = [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80]
_VBT_TP_MULTIPLIERS     = [1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0]
_VBT_SL_MULTIPLIERS     = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 4.5]
_VBT_MAX_HOLD_BARS_LIST = [6, 12, 18, 24, 30, 36]


def run_vbt_prescreener(
    predictions_df: pd.DataFrame,
    ohlcv_df: pd.DataFrame,
    optimize_side: str,                # "long" or "short"
    objective_metric: str = "sharpe",  # "sharpe" or "sortino"
    top_n: int = 20,
    atr_period: int = 14,
    contract_multiplier: float = 1000.0,
    commission_per_side: float = 2.50,
    min_trades: int = 10,
) -> list[dict]:
    """Stage 1: fast vectorbt grid sweep over core parameters.

    Sweeps entry_threshold x tp_atr_mult x sl_atr_mult x max_hold_bars on a
    simplified model (no trailing stop, cooldown, or tiered dispatch). Used
    as a coarse pre-screener to warm-start Stage 2 Optuna.

    Returns a list of up to `top_n` param dicts ranked by approximate
    annualized Sharpe/Sortino, ready to pass to study.enqueue_trial().

    Each returned dict uses the suffixed key convention matching
    _suggest_side_params(trial, suffix) so that enqueue_trial works correctly:
        e.g., {"tp_atr_mult_long": 2.5, "sl_atr_mult_long": 1.0, ...}

    Approximate only — Stage 2 (BacktestEngine) provides full fidelity.
    """
    if not _VBT_AVAILABLE:
        raise ImportError(
            "vectorbt is required for Stage 1 prescreening. "
            "Install it with: pip install vectorbt"
        )
    if optimize_side not in ("long", "short"):
        raise ValueError(f"optimize_side must be 'long' or 'short', got {optimize_side!r}")

    suffix = optimize_side  # e.g. "long" or "short"

    # ── Align predictions and OHLCV on a common datetime index ──────────────
    common_idx = predictions_df.index.intersection(ohlcv_df.index)
    preds = predictions_df.loc[common_idx]
    ohlcv = ohlcv_df.loc[common_idx]

    close = ohlcv["Close"]

    # ── Probability column ───────────────────────────────────────────────────
    keyword = "buy" if optimize_side == "long" else "sell"
    prob_col = _resolve_prob_column(preds, keyword)
    if prob_col is None:
        raise ValueError(
            f"Cannot find '{keyword}' probability column in predictions_df. "
            f"Available columns: {list(preds.columns)}"
        )
    prob_series = preds[prob_col]

    # ── ATR series for entry ────────────────────────────────────────────────
    atr_cache = build_atr_cache(ohlcv_df, [atr_period])
    atr_series = atr_cache[atr_period].reindex(common_idx)

    # ── Annualisation factor for 5-min bars ─────────────────────────────────
    bars_per_year = 105_120  # 252 trading days * 6.5 h * 12 bars/h

    results: list[tuple[float, dict]] = []

    for threshold in _VBT_ENTRY_THRESHOLDS:
        # Entry mask is the same for all (tp, sl, hold) at this threshold
        if optimize_side == "long":
            entries_mask = prob_series > threshold
        else:
            entries_mask = prob_series > threshold

        # ── Relative SL / TP fractions (per bar) ────────────────────────────
        # sl_stop and tp_stop in vectorbt are fractions of the entry price
        # We pre-compute them as pd.Series aligned to close
        # For bars without a valid ATR we skip (NaN → no entry)
        atr_frac = atr_series / close  # fraction of close price

        for tp_mult in _VBT_TP_MULTIPLIERS:
            for sl_mult in _VBT_SL_MULTIPLIERS:
                tp_frac = tp_mult * atr_frac
                sl_frac = sl_mult * atr_frac

                for max_hold in _VBT_MAX_HOLD_BARS_LIST:
                    try:
                        if optimize_side == "long":
                            pf = vbt.Portfolio.from_signals(
                                close,
                                entries=entries_mask,
                                exits=pd.Series(False, index=close.index),
                                sl_stop=sl_frac,
                                tp_stop=tp_frac,
                                max_orders=max_hold,
                                fees=0.0,
                                freq=None,
                            )
                        else:  # short
                            pf = vbt.Portfolio.from_signals(
                                close,
                                short_entries=entries_mask,
                                short_exits=pd.Series(False, index=close.index),
                                sl_stop=sl_frac,
                                tp_stop=tp_frac,
                                max_orders=max_hold,
                                fees=0.0,
                                freq=None,
                            )
                    except Exception:
                        # Any vectorbt construction error → skip this combo
                        continue

                    try:
                        stats = pf.stats()
                        trade_count = int(stats.get("Total Trades", 0))
                    except Exception:
                        trade_count = 0

                    if trade_count < min_trades:
                        continue

                    # ── Score computation ────────────────────────────────────
                    try:
                        rets = pf.returns()
                        if hasattr(rets, "values"):
                            ret_vals = rets.values
                        else:
                            ret_vals = np.asarray(rets)

                        ret_vals = ret_vals[np.isfinite(ret_vals)]
                        if len(ret_vals) < 2:
                            continue

                        mean_r = np.mean(ret_vals)
                        std_r = np.std(ret_vals)

                        if objective_metric == "sortino":
                            downside_sq = np.minimum(0, ret_vals) ** 2
                            downside_dev = float(np.sqrt(np.mean(downside_sq)))
                            if downside_dev < 1e-12:
                                score = OBJECTIVE_SCORE_CAP if mean_r > 0 else 0.0
                            else:
                                score = float(mean_r / downside_dev * np.sqrt(bars_per_year))
                        else:  # sharpe
                            if std_r < 1e-12:
                                continue
                            score = float(mean_r / std_r * np.sqrt(bars_per_year))

                        if not np.isfinite(score):
                            continue

                    except Exception:
                        continue

                    param_dict = {
                        f"tp_atr_mult_{suffix}": float(tp_mult),
                        f"sl_atr_mult_{suffix}": float(sl_mult),
                        f"entry_threshold_{suffix}": float(threshold),
                        f"max_hold_bars_{suffix}": int(max_hold),
                    }
                    results.append((score, param_dict))

    # Sort descending by score and return top_n
    results.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in results[:top_n]]


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
# Warm-start + objective score helpers
# ---------------------------------------------------------------------------


# Parameter search ranges — single source of truth for _suggest_side_params
# and _extract_warm_start_params.  Each entry: (low, high, step, type).
#
# ── AGGRESSIVE search-space tier (2026-07-10) ──────────────────────────────
# Shrunk from the 9-dim/side baseline per the dimensionality audit
# (reports/analysis/optimizer_dim_audit_07102026.md): across 112 pooled
# winners every dimension's winning values spanned 80-100% of its allowed
# range (the 200-trial argmax fits selection noise on every dim), so weak /
# inert dims are FROZEN at the winner-consensus medians (_FROZEN_PARAMS) and
# the surviving dims are narrowed/coarsened to resolutions at or above
# execution noise.  Baseline rollback = git revert of this commit.
#   baseline:   9 dims/side, ~1.5e8 configs/side (log10 8.17)
#   aggressive: 5 dims/side, ~3.0e3 configs/side (log10 3.48)
SEARCH_SPACE_TIER = "aggressive"
_PARAM_RANGES = {
    "tp_atr_mult":                    (4.0,   8.0, 1.0,  "float"),
    "sl_atr_mult":                    (1.0,   3.0, 0.5,  "float"),
    "cooldown_bars":                  (1,    13,   4,    "int"),
    # NOTE: entry_threshold's static tuple is the FALLBACK only (used when a
    # side's prob series is empty/missing). The live search bounds are derived
    # per-model / per-side from the prediction distribution — see
    # _entry_threshold_bounds() and FIRING_FRAC_MIN/MAX below.
    "entry_threshold":                (0.30,  0.70, 0.08, "float"),
    "atr_period":                     (4,    36,   8,    "int"),
}

# Frozen (formerly searched) dims — deliberately constant for EVERY trial and
# re-applied to the reconstructed best config, so trial cfg == best cfg.
# Values are the winner-consensus medians from the audit (not config
# fallbacks — the no-silent-default rule does not apply to deliberate
# experiment constants).  max_hold_bars is frozen GLOBALLY at the pooled
# median (audit P11 proposed per-target-family values; deferred — needs
# manifest plumbing).
_FROZEN_PARAMS = {
    "trigger_frac": 0.4,              # trailing trigger at 40% of TP distance
    "distance_frac": 0.5,             # trailing SL offset at 50% of trigger
    "max_hold_bars": 30,              # pooled winner median
    "consecutive_signal_threshold": 2,
}
# conflict_resolution: provably inert in single-side mode (the opposite side
# is disabled, and reconstruction dropped it) and modal-at-"hold" in ensemble
# winners — frozen, no longer suggested (audit P1+P2).
_FROZEN_CONFLICT_RESOLUTION = "hold"

# ── Dynamic entry-threshold search band (firing-fraction based) ────────────
# Different models emit probabilities on completely different scales, so a
# fixed [0.30, 0.70] entry-threshold grid can sit below a model's entire
# probability mass and let the optimizer pick an "always-on" threshold that
# fires on ~100% of bars (throwing away the ranking edge and starving the
# opposite side in the single-position engine). Instead we derive the search
# bounds per-model / per-side from that model's own prediction distribution,
# expressed as a target signal-firing band [f_min, f_max]:
#     f(t) = P(prob >= t) = 1 - CDF(t)          (upper-tail firing fraction)
#     threshold_floor   = quantile(1 - f_max)   (most-permissive, fires f_max)
#     threshold_ceiling = quantile(1 - f_min)   (most-selective,  fires f_min)
# The band is a parameter (threaded from run_optimization/CLI, and — in a
# fast-follow ticket — from the v2 manifest). These module constants are the
# first-run LIBERAL defaults (per user, 2026-07-07).
FIRING_FRAC_MIN = 0.05
FIRING_FRAC_MAX = 0.45

# Guard rails for degenerate/compressed prediction distributions.
_ENTRY_THR_MIN_SPAN = 0.01      # below this span the range is "collapsed"
_ENTRY_THR_HALF_SPAN = 0.005    # widen symmetrically to +/- this around midpoint
_ENTRY_THR_MIN_STEP = 1e-3      # avoid a zero / absurdly-fine Optuna step


def _entry_threshold_bounds(
    prob_series: "pd.Series",
    f_min: float,
    f_max: float,
) -> tuple[float, float, float]:
    """Derive per-side entry-threshold search bounds from a model's own
    prediction distribution, expressed as a firing band ``[f_min, f_max]``.

    Firing fraction at threshold ``t`` is ``f(t) = P(prob >= t) = 1 - CDF(t)``,
    so a LARGER firing fraction inverts to a LOWER threshold:

        low  = quantile(1 - f_max)   # most-permissive threshold, fires ~f_max
        high = quantile(1 - f_min)   # most-selective threshold, fires ~f_min
        step = max((high - low) / 5, 1e-3)   # 6-point grid (aggressive tier; was /10 = 11 points)

    NaNs are dropped before quantiling. Degenerate / compressed distributions
    (``high - low`` below a small epsilon) are widened symmetrically to a
    minimum span around the midpoint so Optuna receives a valid non-empty
    range; both bounds are clamped to ``[0.0, 1.0]`` and ``high > low`` is
    guaranteed.

    Returns ``(low, high, step)``.
    """
    clean = prob_series.dropna()

    low = float(clean.quantile(1.0 - f_max))
    high = float(clean.quantile(1.0 - f_min))

    # Order defensively (a pathological series could invert these).
    if high < low:
        low, high = high, low

    # Widen collapsed/degenerate ranges so Optuna gets a usable search space.
    if (high - low) < _ENTRY_THR_MIN_SPAN:
        mid = (high + low) / 2.0
        low = mid - _ENTRY_THR_HALF_SPAN
        high = mid + _ENTRY_THR_HALF_SPAN

    # Clamp to the valid probability range.
    low = max(0.0, min(1.0, low))
    high = max(0.0, min(1.0, high))

    # If clamping collapsed the range (e.g. midpoint at a boundary), nudge apart
    # while staying inside [0, 1].
    if high <= low:
        high = min(1.0, low + _ENTRY_THR_MIN_SPAN)
        low = max(0.0, high - _ENTRY_THR_MIN_SPAN)

    step = max((high - low) / 5.0, _ENTRY_THR_MIN_STEP)
    return low, high, step


def _compute_entry_thr_bounds(
    predictions_df: "pd.DataFrame",
    f_min: float,
    f_max: float,
) -> dict:
    """Precompute per-side dynamic entry-threshold bounds from a predictions df.

    Returns ``{"long": (low, high, step) | None, "short": (low, high, step) | None}``
    where the long side is derived from ``prob_Buy`` and the short side from
    ``prob_Sell``. A side maps to ``None`` when its prob column is absent or
    empty — callers then fall back to the static _PARAM_RANGES grid for that
    side (single-side configs with no opposite predictions).

    This is computed ONCE per optimization (not per trial), on the
    optimizer-window predictions only (the holdout is sliced off upstream in
    run_optimization — never recompute on the holdout).
    """
    bounds: dict = {"long": None, "short": None}
    for side, col in (("long", "prob_Buy"), ("short", "prob_Sell")):
        if col in predictions_df.columns:
            series = predictions_df[col].dropna()
            if len(series) > 0:
                bounds[side] = _entry_threshold_bounds(series, f_min, f_max)
    return bounds


def _snap_to_grid(value: float, low: float, high: float, step: float,
                  dtype: str = "float") -> float | int:
    """Clamp `value` to [low, high] and snap to the nearest grid point."""
    clamped = max(low, min(high, value))
    steps_from_low = round((clamped - low) / step)
    snapped = low + steps_from_low * step
    snapped = max(low, min(high, snapped))
    if dtype == "int":
        return int(round(snapped))
    # Re-clamp AFTER rounding: dynamic entry_threshold bounds carry many
    # decimals, so round(..., 10) can nudge a boundary snap ~1e-11 outside
    # [low, high] and cause enqueue_trial to reject the warm-start baseline.
    # (The static grid has clean 2-decimal bounds and is unaffected.)
    return max(low, min(high, round(snapped, 10)))  # avoid float precision noise


def _derive_trailing_params(params: dict) -> dict:
    """Derive structural trailing stops from latent variables."""
    out = params.copy()
    if "trigger_frac" in out and "tp_atr_mult" in out:
        out["trailing_atr_mult"] = round(out["tp_atr_mult"] * out.pop("trigger_frac"), 10)
        out["trailing_sl_atr_offset"] = round(out["trailing_atr_mult"] * (1.0 - out.pop("distance_frac")), 10)
    return out


def _reapply_strategy_level_params(best_cfg: dict, trial_params: dict) -> dict:
    """Re-apply strategy-level (non-per-side) trial params onto ``best_cfg``.

    The reconstruction blocks split ``best_trial.params`` into per-side dicts
    using a ``_long`` / ``_short`` suffix filter. Any strategy-level param that
    carries neither suffix — notably ``conflict_resolution`` — is silently
    dropped by that filter, so the re-backtest of ``best_cfg`` diverged from the
    trial's own score (best_obj_score != consistency_score).

    This mirrors exactly what the Optuna objective does when it builds its cfg
    (``cfg["conflict_resolution"] = conflict_mode``): any key without a side
    suffix is written straight onto the top-level config.

    Mutates ``best_cfg`` in place and returns it for chaining.

    ``atr_period_shared`` (the tied cross-side ATR of the aggressive tier) is
    per-side data, not a strategy-level key — it is injected into both side
    dicts by the reconstruction blocks and must NOT be written top-level.
    """
    for k, v in trial_params.items():
        if k == "atr_period_shared":
            continue
        if not (k.endswith("_long") or k.endswith("_short")):
            best_cfg[k] = v
    return best_cfg


def _entry_threshold_grid(
    entry_thr_bounds: dict | None,
    side: str,
) -> tuple[float, float, float, str]:
    """Return the (low, high, step, dtype) grid to snap ``entry_threshold`` to.

    Uses the per-side DYNAMIC bounds (``entry_thr_bounds[side]``) so the
    warm-start baseline is snapped onto the SAME grid the sampler explores.
    Falls back to the static ``_PARAM_RANGES["entry_threshold"]`` only when
    dynamic bounds are unavailable (e.g. a side's prob series was empty).
    """
    if entry_thr_bounds and side in entry_thr_bounds and entry_thr_bounds[side]:
        low, high, step = entry_thr_bounds[side]
        return low, high, step, "float"
    return _PARAM_RANGES["entry_threshold"]


def _extract_warm_start_params(
    base_cfg: dict,
    is_tiered: bool,
    optimize_side: str | None,
    entry_thr_bounds: dict | None = None,
) -> dict | None:
    """Extract baseline config values as an Optuna-compatible param dict.

    Maps per-side config blocks (cfg["long"], cfg["short"]) into the flat
    suffixed key convention used by _suggest_side_params(), then snaps every
    value to the Optuna search grid so enqueue_trial() accepts it.

    ``entry_threshold`` is snapped to the per-side DYNAMIC grid supplied in
    ``entry_thr_bounds`` (``{"long": (low, high, step), "short": (...)}`` for
    tiered, ``{"long": (...)}`` for non-tiered), NOT the static _PARAM_RANGES
    grid — this is the 3-way consistency invariant with _suggest_side_params()
    and the non-tiered objective loop. A mismatch would silently distort or
    reject the enqueued baseline trial. Every OTHER param stays on the static
    grid. When ``entry_thr_bounds`` is None/missing a side, the static
    entry_threshold grid is used as the fallback.

    Returns None if extraction fails (e.g. missing config keys).
    """
    def _extract_side(cfg: dict, side: str, suffix: str, include_atr: bool = True) -> dict:
        """Extract params for one side from its config block.

        Emits ONLY keys the objective will actually suggest for this mode
        (the _PARAM_RANGES survivors) — frozen dims (_FROZEN_PARAMS) and the
        derived trailing latents are deliberately absent, and ``include_atr``
        is False in ensemble mode where the tied ``atr_period_shared`` is
        emitted separately by the caller.  Extra keys in enqueue_trial would
        desynchronize the warm-start trial from the search space.
        """
        side_cfg = cfg.get(side, {})
        params = {}

        # Entry threshold from tiers[0].min_prob (tiered) or models.*.threshold
        if is_tiered:
            tiers = side_cfg.get("tiers", [])
            if tiers:
                raw_thr = tiers[0].get("min_prob", 0.55)
            else:
                raw_thr = cfg.get("models", {}).get(side, {}).get("threshold", 0.55)
        else:
            raw_thr = cfg.get("models", {}).get(side, {}).get(
                "threshold", cfg.get("entry_threshold", 0.55)
            )

        raw_values = {
            "entry_threshold": raw_thr,
            "tp_atr_mult": side_cfg.get("tp_atr_mult", cfg.get("tp_atr_mult", 3.0)),
            "sl_atr_mult": side_cfg.get("sl_atr_mult", cfg.get("sl_atr_mult", 1.5)),
            "cooldown_bars": side_cfg.get("cooldown_bars", cfg.get("cooldown_bars", 7)),
            "atr_period": side_cfg.get("atr_period", cfg.get("atr_period", 14)),
        }
        if not include_atr:
            raw_values.pop("atr_period")

        for key, raw_val in raw_values.items():
            if key == "entry_threshold":
                # Snap onto the per-side DYNAMIC grid (consistency invariant).
                low, high, step, dtype = _entry_threshold_grid(entry_thr_bounds, side)
            else:
                low, high, step, dtype = _PARAM_RANGES[key]
            snapped = _snap_to_grid(raw_val, low, high, step, dtype)
            if snapped != raw_val:
                print(f"  [WARM-START] {key}_{suffix}: {raw_val} -> {snapped} (snapped to grid)")
            params[f"{key}_{suffix}"] = snapped

        return params

    try:
        warm = {}
        if is_tiered and optimize_side:
            # Single-side mode
            warm = _extract_side(base_cfg, optimize_side, optimize_side)
        elif is_tiered:
            # Simultaneous ensemble — both sides. atr_period is TIED across
            # sides (audit P4): one shared key, snapped from the long side.
            # conflict_resolution is frozen — not part of the search space.
            warm.update(_extract_side(base_cfg, "long", "long", include_atr=False))
            warm.update(_extract_side(base_cfg, "short", "short", include_atr=False))
            a_low, a_high, a_step, a_dtype = _PARAM_RANGES["atr_period"]
            raw_atr = base_cfg.get("long", {}).get(
                "atr_period", base_cfg.get("atr_period", 14)
            )
            warm["atr_period_shared"] = _snap_to_grid(raw_atr, a_low, a_high, a_step, a_dtype)
        else:
            # Non-tiered — no suffix. Single-model configs are long-side.
            side_cfg = base_cfg
            for key in _PARAM_RANGES:
                if key == "entry_threshold":
                    low, high, step, dtype = _entry_threshold_grid(entry_thr_bounds, "long")
                    raw_val = base_cfg.get("models", {}).get("long", {}).get(
                        "threshold", base_cfg.get("entry_threshold", 0.55)
                    )
                else:
                    low, high, step, dtype = _PARAM_RANGES[key]
                    raw_val = base_cfg.get(key, low)
                snapped = _snap_to_grid(raw_val, low, high, step, dtype)
                if snapped != raw_val:
                    print(f"  [WARM-START] {key}: {raw_val} -> {snapped} (snapped to grid)")
                warm[key] = snapped

        return warm if warm else None
    except Exception as e:
        print(f"  [WARN] Failed to extract warm-start params: {e}")
        return None


def _compute_objective_score(
    result: BacktestResult,
    objective_metric: str,
    trade_floor: float,
    window_start=None,
    window_end=None,
    n_blocks: int = 3,
    lambda_dispersion: float = 1.0,
) -> float:
    """Compute the same Sharpe/Sortino/block score used by the Optuna objective.

    This mirrors the scoring logic in make_objective() so that baseline and
    optimized results are comparable on the same scale. Block metrics
    additionally need the fixed in-sample window bounds (the SLICED
    predictions window, not the trade span) and the block params.
    """
    if result.trade_count == 0 or not result.trades:
        return -9999.0

    trade_records = []
    for t in result.trades:
        trade_records.append({"exit_dt": t.exit_dt, "pnl": t.net_pnl_dollars})
    trades_df = pd.DataFrame(trade_records)
    trades_df["exit_dt"] = pd.to_datetime(trades_df["exit_dt"])
    trades_df = trades_df.set_index("exit_dt").sort_index()

    monthly_pnls = trades_df["pnl"].resample("M").sum().dropna()
    monthly_pnl_vals = monthly_pnls.values

    if len(monthly_pnl_vals) == 0:
        return -9999.0

    if objective_metric in BLOCK_OBJECTIVE_METRICS:
        if window_start is None or window_end is None:
            raise ValueError(
                f"objective_metric={objective_metric!r} requires window_start/"
                f"window_end (the sliced in-sample window bounds) — no silent fallback."
            )
        agg = _block_sharpe_score(
            monthly_pnls=monthly_pnls,
            window_start=window_start,
            window_end=window_end,
            n_blocks=n_blocks,
            metric=objective_metric,
            lambda_dispersion=lambda_dispersion,
        )
        raw_score = min(agg, OBJECTIVE_SCORE_CAP)
    elif objective_metric == "sortino":
        downside_sq = np.minimum(0, monthly_pnl_vals) ** 2
        downside_dev = float(np.sqrt(np.mean(downside_sq)))
        if downside_dev < 1e-9:
            if len(monthly_pnl_vals) > 0 and float(np.mean(monthly_pnl_vals)) > 0:
                raw_score = OBJECTIVE_SCORE_CAP   # Holy grail: only winning months
            else:
                return -9999.0  # Degenerate: 0 trades or perfectly flat
        else:
            score = float(
                (np.mean(monthly_pnl_vals) / downside_dev) * np.sqrt(12)
            )
            raw_score = min(score, OBJECTIVE_SCORE_CAP)
    else:
        std_pnl = float(np.std(monthly_pnl_vals))
        if std_pnl < 1e-9:
            return -9999.0
        score = float(
            (np.mean(monthly_pnl_vals) / std_pnl) * np.sqrt(12)
        )
        raw_score = min(score, OBJECTIVE_SCORE_CAP)
        
    return _apply_trade_floor_penalty(raw_score, result.trade_count, trade_floor)


# ---------------------------------------------------------------------------
# Optuna objective
# ---------------------------------------------------------------------------


def _resolve_symbol_economics(
    symbol: str | None,
    slippage_per_side: float | None,
) -> tuple[float | None, float | None]:
    """Resolve (contract_multiplier, slippage_per_side) for a symbol.

    symbol=None preserves legacy behavior exactly (engine default 1000 $/pt,
    slippage passed through untouched). With a symbol, dollars-per-point comes
    from the instrument registry, and a missing slippage falls back to the
    instrument's 1-tick default. CL resolves to (1000.0, 0.01) — byte-identical
    to the legacy constants (ledger-parity gate).
    """
    if not symbol:
        return None, slippage_per_side
    from src.core.instrument_master import dollars_per_point, default_slippage_points
    mult = dollars_per_point(symbol)
    if slippage_per_side is None:
        slippage_per_side = default_slippage_points(symbol)
    return mult, slippage_per_side


def make_objective(
    base_cfg: dict,
    predictions_df: pd.DataFrame,
    ohlcv_df: pd.DataFrame,
    ohlcv_exec_df: pd.DataFrame | None = None,
    best_tracker: BestResultTracker | None = None,
    tracker: "TopKTracker | None" = None,
    objective_metric: str = "sharpe",
    optimize_side: str | None = None,
    slippage_per_side: float | None = None,
    contract_multiplier: float | None = None,
    f_min: float = FIRING_FRAC_MIN,
    f_max: float = FIRING_FRAC_MAX,
    entry_thr_bounds: dict | None = None,
    n_blocks: int = 3,
    lambda_dispersion: float = 1.0,
    window_start=None,
    window_end=None,
):
    """Create a closure that Optuna can call with trial params.

    Supports three optimization modes:
      - optimize_side=None: simultaneous ensemble (both long+short params)
      - optimize_side="long": only long-side params, short side disabled
      - optimize_side="short": only short-side params, long side disabled

    Supports two objective functions:
      - objective_metric="sharpe": Annualized Monthly Sharpe Ratio
      - objective_metric="sortino": Annualized Monthly Sortino Ratio

    Args:
        base_cfg: Base strategy config dict.
        predictions_df: OOS predictions DataFrame.
        ohlcv_df: OHLCV DataFrame.
        best_tracker: Optional BestResultTracker — O(1) memory, replaces
            the old unbounded dict that stored all trial results.
        tracker: Optional TopKTracker to collect top configs.
        objective_metric: "sharpe" or "sortino".
        optimize_side: "long", "short", or None (both sides / ensemble).
        f_min, f_max: Signal-firing band used to derive the per-side dynamic
            entry_threshold search bounds (defaults: the module constants
            FIRING_FRAC_MIN / FIRING_FRAC_MAX). Passed as params so a downstream
            ticket can wire them from the manifest without changing the objective.
        entry_thr_bounds: Optional precomputed per-side bounds
            ({"long": (low, high, step) | None, "short": ...}). When None they
            are computed here from ``predictions_df`` (prob_Buy / prob_Sell).
            run_optimization precomputes and passes these so the warm-start
            extraction snaps to the SAME grid the sampler uses.
        n_blocks, lambda_dispersion: Block-metric params (BLOCK_OBJECTIVE_METRICS).
        window_start, window_end: Fixed in-sample window bounds for the block
            partition — identical for every trial. Default to the bounds of the
            (already holdout-sliced) ``predictions_df``, never a trial's trade span.
    """
    # Pre-create a strategy instance for parameter routing
    strategy = create_execution_strategy(base_cfg)
    is_tiered = (
        base_cfg.get("execution_class") == "TieredEnsembleStrategy"
        and base_cfg.get("long", {}).get("tiers")
        and base_cfg.get("short", {}).get("tiers")
    )

    # ── Dynamic per-side entry-threshold bounds (once, not per trial) ─────
    # Derived from THIS model's own prediction distribution on the optimizer
    # window; the holdout is already sliced off upstream. entry_threshold uses
    # these bounds; every other param stays on the static _PARAM_RANGES grid.
    if entry_thr_bounds is None:
        entry_thr_bounds = _compute_entry_thr_bounds(predictions_df, f_min, f_max)

    def _entry_thr_range(suffix: str) -> tuple[float, float, float]:
        """Per-side dynamic (low, high, step) for entry_threshold, with the
        static grid as the fallback when a side's prob series was empty."""
        b = entry_thr_bounds.get(suffix) if entry_thr_bounds else None
        if b is not None:
            return b
        low, high, step, _ = _PARAM_RANGES["entry_threshold"]
        return low, high, step

    # Log the derived per-side bounds + implied firing band (parity with the
    # [WARM-START] prints) so batch logs show exactly what was searched.
    for _side in ("long", "short"):
        _b = entry_thr_bounds.get(_side) if entry_thr_bounds else None
        if _b is not None:
            _lo, _hi, _st = _b
            print(
                f"  [ENTRY-THR] {_side}: range=[{_lo:.4f}, {_hi:.4f}] step={_st:.4f} "
                f"(firing band [{f_min:.2f}, {f_max:.2f}])"
            )
        else:
            print(
                f"  [ENTRY-THR] {_side}: no prob series -> static fallback "
                f"{_PARAM_RANGES['entry_threshold'][:2]}"
            )

    # Compute trade floor from prediction data span (once, not per trial)
    # Use halved floor for single-side optimization (18/year vs 36/year)
    _floor_rate = TRADES_PER_YEAR_FLOOR_SINGLE if optimize_side else TRADES_PER_YEAR_FLOOR
    _backtest_years = (predictions_df.index.max() - predictions_df.index.min()).days / 365.25
    _trade_floor = max(1.0, _floor_rate * _backtest_years)

    # Block-partition window bounds (once, not per trial): the sliced in-sample
    # predictions window — fixed and identical for every trial.
    if window_start is None:
        window_start = predictions_df.index.min()
    if window_end is None:
        window_end = predictions_df.index.max()

    def _suggest_side_params(trial: optuna.Trial, suffix: str,
                             shared_atr: int | None = None) -> dict:
        """Suggest params for one side with the given suffix.

        ``shared_atr`` (ensemble mode) injects the tied cross-side ATR period
        (audit P4) instead of a per-side suggestion.  Frozen dims
        (_FROZEN_PARAMS) are appended verbatim so the derived trailing params
        and the applied config stay fully specified.
        """
        params = {}
        for key, (low, high, step, dtype) in _PARAM_RANGES.items():
            if key == "atr_period" and shared_atr is not None:
                params[key] = shared_atr
            elif key == "entry_threshold":
                # Dynamic per-side bounds instead of the static tuple.
                d_low, d_high, d_step = _entry_thr_range(suffix)
                params[key] = trial.suggest_float(
                    f"{key}_{suffix}", d_low, d_high, step=d_step
                )
            elif dtype == "float":
                params[key] = trial.suggest_float(f"{key}_{suffix}", low, high, step=step)
            elif dtype == "int":
                params[key] = trial.suggest_int(f"{key}_{suffix}", int(low), int(high), step=int(step))
        params.update(_FROZEN_PARAMS)
        return _derive_trailing_params(params)

    def _disable_side(cfg: dict, side_to_disable: str) -> None:
        """Disable a side by setting all tier min_prob to 1.0."""
        if side_to_disable in cfg and "tiers" in cfg[side_to_disable]:
            for tier in cfg[side_to_disable]["tiers"]:
                tier["min_prob"] = 1.0

    def objective(trial: optuna.Trial) -> float:
        cfg = copy.deepcopy(base_cfg)

        if is_tiered:
            # conflict_resolution FROZEN (audit P1+P2): it was provably inert
            # in single-side mode (opposite side disabled + dropped at
            # reconstruction) and modal-at-"hold" in ensemble winners.
            cfg["conflict_resolution"] = _FROZEN_CONFLICT_RESOLUTION

            if optimize_side == "long":
                # Single-side: only suggest long params, disable short
                side_params = _suggest_side_params(trial, "long")
                strategy.apply_trial_params(cfg, side_params, side="long")
                _disable_side(cfg, "short")
            elif optimize_side == "short":
                # Single-side: only suggest short params, disable long
                side_params = _suggest_side_params(trial, "short")
                strategy.apply_trial_params(cfg, side_params, side="short")
                _disable_side(cfg, "long")
            else:
                # Simultaneous: asymmetric params for both sides, but ONE
                # shared ATR clock (audit P4 — long/short winner medians were
                # identical at 22/22 across 112 winners).
                a_low, a_high, a_step, _a_dtype = _PARAM_RANGES["atr_period"]
                shared_atr = trial.suggest_int(
                    "atr_period_shared", int(a_low), int(a_high), step=int(a_step)
                )
                long_params = _suggest_side_params(trial, "long", shared_atr=shared_atr)
                short_params = _suggest_side_params(trial, "short", shared_atr=shared_atr)
                strategy.apply_trial_params(cfg, long_params, side="long")
                strategy.apply_trial_params(cfg, short_params, side="short")
        else:
            # Non-tiered: single set of params. Non-tiered configs are
            # single-model long, so entry_threshold uses the long-side dynamic
            # bounds; every other param stays on the static grid.
            params = {}
            for key, (low, high, step, dtype) in _PARAM_RANGES.items():
                if key == "entry_threshold":
                    d_low, d_high, d_step = _entry_thr_range("long")
                    params[key] = trial.suggest_float(key, d_low, d_high, step=d_step)
                elif dtype == "float":
                    params[key] = trial.suggest_float(key, low, high, step=step)
                elif dtype == "int":
                    params[key] = trial.suggest_int(key, int(low), int(high), step=int(step))
            params.update(_FROZEN_PARAMS)
            params = _derive_trailing_params(params)
            if base_cfg.get("execution_class") == "BreakoutStraddleStrategy":
                params["breakout_window"] = trial.suggest_int("breakout_window", 2, 24, step=2)
            strategy.apply_trial_params(cfg, params)

        overrides = {}
        if slippage_per_side is not None:
            overrides["slippage_per_side"] = slippage_per_side
        if contract_multiplier is not None:
            overrides["contract_multiplier"] = contract_multiplier

        engine = BacktestEngine.from_config(cfg, **overrides)
        result = engine.run(predictions_df, ohlcv_df, ohlcv_exec_df=ohlcv_exec_df)

        # --- Scoring ---
        if result.trade_count == 0 or not result.trades:
            return -9999.0

        trade_records = []
        for t in result.trades:
            trade_records.append({"exit_dt": t.exit_dt, "pnl": t.net_pnl_dollars})
        trades_df = pd.DataFrame(trade_records)
        trades_df["exit_dt"] = pd.to_datetime(trades_df["exit_dt"])
        trades_df = trades_df.set_index("exit_dt").sort_index()

        monthly_pnls = trades_df["pnl"].resample("M").sum().dropna()
        monthly_pnl_vals = monthly_pnls.values

        if len(monthly_pnl_vals) == 0:
            return -9999.0

        if objective_metric in BLOCK_OBJECTIVE_METRICS:
            # --- Block-wise Annualized Monthly Sharpe ---
            agg = _block_sharpe_score(
                monthly_pnls=monthly_pnls,
                window_start=window_start,
                window_end=window_end,
                n_blocks=n_blocks,
                metric=objective_metric,
                lambda_dispersion=lambda_dispersion,
            )
            annualized_score = min(agg, OBJECTIVE_SCORE_CAP)
        elif objective_metric == "sortino":
            # --- Annualized Monthly Sortino (Target Downside Deviation) ---
            downside_sq = np.minimum(0, monthly_pnl_vals) ** 2
            downside_dev = float(np.sqrt(np.mean(downside_sq)))
            if downside_dev < 1e-9:
                if len(monthly_pnl_vals) > 0 and float(np.mean(monthly_pnl_vals)) > 0:
                    annualized_score = OBJECTIVE_SCORE_CAP   # Holy grail: only winning months
                else:
                    annualized_score = -9999.0  # Degenerate
            else:
                annualized_score = float(
                    (np.mean(monthly_pnl_vals) / downside_dev) * np.sqrt(12)
                )
                annualized_score = min(annualized_score, OBJECTIVE_SCORE_CAP)
        else:
            # --- Annualized Monthly Sharpe ---
            std_pnl = float(np.std(monthly_pnl_vals))
            if std_pnl < 1e-9:
                return -9999.0
            annualized_score = float(
                (np.mean(monthly_pnl_vals) / std_pnl) * np.sqrt(12)
            )
            annualized_score = min(annualized_score, OBJECTIVE_SCORE_CAP)

        # --- Trade Floor Penalty ---
        # Negative score returned as-is (multiplying by weight < 1 would
        # *improve* a negative score — the opposite of the intended effect).
        final_score = _apply_trade_floor_penalty(annualized_score, result.trade_count, _trade_floor)

        # Track top configs (penalized score so ranking matches Optuna)
        if tracker is not None:
            metrics = extract_metrics(result)
            tracker.add(final_score, trial.number, cfg, metrics=metrics)

        # Update best result tracker — O(1) memory, only keeps the single best
        if best_tracker is not None:
            best_tracker.update_if_better(final_score, trial.number, result)

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
    objective_metric: str = "sharpe",
    optimize_side: str | None = None,
    exec_ohlcv_path: str | None = None,
    slippage_per_side: float | None = None,
    random_seed: int = 42,
    symbol: str | None = None,
    firing_frac_min: float = FIRING_FRAC_MIN,
    firing_frac_max: float = FIRING_FRAC_MAX,
    n_blocks: int = 3,
    lambda_dispersion: float = 1.0,
    min_block_months: int = 10,
) -> tuple[dict, BacktestResult]:
    """Run strategy parameter optimization.

    Args:
        holdout_months: If set, reserve the last N months of predictions
            as an unseen holdout.  Optuna only sees data before the cutoff.
            Falls back to config key ``holdout_months`` when *None*.
        objective_metric: "sharpe", "sortino", or a block metric
            (BLOCK_OBJECTIVE_METRICS: block_min / block_median / block_mean_std).
        optimize_side: "long", "short", or None (both sides / ensemble).
        symbol: Instrument symbol for economics resolution (contract
            multiplier + default 1-tick slippage). None = legacy CL-econ
            behavior (engine default 1000 $/pt).
        firing_frac_min, firing_frac_max: Signal-firing band that derives the
            per-side dynamic entry_threshold search bounds (defaults: module
            constants FIRING_FRAC_MIN / FIRING_FRAC_MAX). Threaded from the CLI;
            a fast-follow ticket wires them from the v2 manifest.
        n_blocks, lambda_dispersion, min_block_months: Block-metric params.
            A block metric HARD-RAISES ValueError when the post-holdout
            in-sample window has fewer calendar months than
            n_blocks * min_block_months — no silent fallback to sharpe.

    Returns:
        Tuple of (best_config, best_result).  Holdout metrics (if any)
        are stored in ``best_config["optuna_info"]["holdout_metrics"]``.
    """
    start_time = time.perf_counter()

    # Seed numpy for reproducibility (Optuna sampler seeded separately below).
    effective_seed = random_seed + _OBJECTIVE_SEED_OFFSETS.get(objective_metric, 0)
    np.random.seed(effective_seed)

    from src.live_execution.config_loader import load_strategy_config
    base_cfg = load_strategy_config(config_path)

    model_name = base_cfg.get("nickname", Path(config_path).stem)
    if label:
        model_name = f"{label} | {model_name}"
    is_tiered = (
        base_cfg.get("execution_class") == "TieredEnsembleStrategy"
        and base_cfg.get("long", {}).get("tiers")
        and base_cfg.get("short", {}).get("tiers")
    )

    # Determine optimization mode description
    if optimize_side:
        mode_str = f"SINGLE-SIDE ({optimize_side.upper()})"
    elif is_tiered:
        mode_str = "SIMULTANEOUS ENSEMBLE"
    else:
        mode_str = "SINGLE CONFIG"

    if objective_metric == "sortino":
        obj_str = "Annualized Monthly Sortino"
    elif objective_metric in BLOCK_OBJECTIVE_METRICS:
        obj_str = (f"Block-wise Annualized Monthly Sharpe ({objective_metric}, "
                   f"n_blocks={n_blocks}, lambda={lambda_dispersion})")
    else:
        obj_str = "Annualized Monthly Sharpe"

    contract_multiplier, slippage_per_side = _resolve_symbol_economics(symbol, slippage_per_side)

    print("=" * 70)
    print(f"STRATEGY PARAMETER OPTIMIZATION: {model_name}")
    print(f"  Config: {config_path}")
    print(f"  Trials: {n_trials}")
    print(f"  Mode: {mode_str}")
    print(f"  Objective: {obj_str}")
    if symbol:
        print(f"  Economics: symbol={symbol}  $/pt={contract_multiplier}  slippage/side={slippage_per_side}")
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
    if exec_ohlcv_path:
        print(f"  EXEC data: {exec_ohlcv_path}")

    print("\nLoading data...")
    ohlcv_df, ohlcv_exec_df = load_ohlcv_dual(ohlcv_path) if exec_ohlcv_path is None else load_ohlcv_dual(ohlcv_path)
    if exec_ohlcv_path:
        ohlcv_exec_df = load_ohlcv(exec_ohlcv_path)
        
    ohlcv_df = attach_atr_cache(ohlcv_df)   # stamp ATR_{period} cols once; BacktestEngine skips recomputation per trial
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

    # ── In-sample window bounds (post-holdout, fixed for every trial) ──
    _win_start = predictions_df.index.min()
    _win_end = predictions_df.index.max()
    _insample_months = (_win_end.to_period("M") - _win_start.to_period("M")).n + 1
    if objective_metric in BLOCK_OBJECTIVE_METRICS:
        _check_block_window(objective_metric, _win_start, _win_end,
                            _insample_months, n_blocks, min_block_months)

    # Run baseline (with side-appropriate config for fair comparison)
    print("\n--- BASELINE ---")
    baseline_cfg = copy.deepcopy(base_cfg)
    if optimize_side and is_tiered:
        # Disable the other side for a fair baseline comparison
        other_side = "short" if optimize_side == "long" else "long"
        if other_side in baseline_cfg and "tiers" in baseline_cfg[other_side]:
            for tier in baseline_cfg[other_side]["tiers"]:
                tier["min_prob"] = 1.0
                
    overrides = {}
    if slippage_per_side is not None:
        overrides["slippage_per_side"] = slippage_per_side
    if contract_multiplier is not None:
        overrides["contract_multiplier"] = contract_multiplier

    baseline_engine = BacktestEngine.from_config(baseline_cfg, **overrides)
    baseline_result = baseline_engine.run(predictions_df, ohlcv_df, ohlcv_exec_df=ohlcv_exec_df, label="Baseline")
    baseline_metrics = extract_metrics(baseline_result)
    print(f"  PnL: ${baseline_metrics['total_pnl']:,.2f}  "
          f"PF: {baseline_metrics['profit_factor']:.2f}  "
          f"WR: {baseline_metrics['win_rate']:.1%}  "
          f"Trades: {baseline_metrics['trade_count']}  "
          f"DD: ${baseline_metrics['max_drawdown']:,.2f}")

    # ── Optimization ─────────────────────────────────────────────────
    # Precompute per-side dynamic entry-threshold bounds ONCE on the optimizer
    # window (holdout already sliced off above) and share them between the
    # objective and the warm-start extraction, so the baseline snaps to the
    # SAME grid the sampler explores (3-way consistency invariant).
    entry_thr_bounds = _compute_entry_thr_bounds(
        predictions_df, firing_frac_min, firing_frac_max
    )

    best_result_tracker = BestResultTracker()
    tracker = TopKTracker(k=5, save_dir="configs/strategies/candidates")
    objective = make_objective(
        base_cfg, predictions_df, ohlcv_df, ohlcv_exec_df=ohlcv_exec_df, best_tracker=best_result_tracker, tracker=tracker,
        objective_metric=objective_metric, optimize_side=optimize_side, slippage_per_side=slippage_per_side,
        contract_multiplier=contract_multiplier,
        f_min=firing_frac_min, f_max=firing_frac_max, entry_thr_bounds=entry_thr_bounds,
        n_blocks=n_blocks, lambda_dispersion=lambda_dispersion,
        window_start=_win_start, window_end=_win_end,
    )

    # PID in the hash: two batches post-optimizing concurrently on one machine
    # share model_name ("Ensemble_N ... | HourSet_10_Base") — without it they
    # collide on the same study .db (WinError 32, all tasks fail). The study is
    # deleted fresh each run, so uniqueness is the only requirement.
    db_hash = hashlib.md5(f"strategy_opt_{model_name}_{objective_metric}_{os.getpid()}".encode()).hexdigest()[:8]
    db_dir = Path("reports/optuna_db")
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / f"optuna_study_{db_hash}.db"
    db_path.unlink(missing_ok=True)  # fresh study each run — avoids trial number inflation
    study = optuna.create_study(
        direction="maximize",
        study_name=f"strategy_opt_{model_name}_{objective_metric}",
        storage=f"sqlite:///{db_path}",
        sampler=optuna.samplers.TPESampler(seed=effective_seed),
    )

    # ── Warm-start: inject baseline as trial #0 ───────────────────────
    warm_params = _extract_warm_start_params(
        base_cfg, is_tiered, optimize_side, entry_thr_bounds=entry_thr_bounds
    )
    _warm_start_ok = False
    if warm_params:
        try:
            study.enqueue_trial(warm_params)
            _warm_start_ok = True
            print(f"  Enqueued baseline config as warm-start trial #0")
        except Exception as e:
            print(f"  [WARN] Failed to enqueue baseline warm-start: {e}")
    if not _warm_start_ok:
        print("  [WARN] NO WARM-START: Optimizer will cold-start from random sampling!")

    # Compute baseline objective score for regression guard
    _floor_rate = TRADES_PER_YEAR_FLOOR_SINGLE if optimize_side else TRADES_PER_YEAR_FLOOR
    _backtest_years = (predictions_df.index.max() - predictions_df.index.min()).days / 365.25
    _trade_floor = max(1.0, _floor_rate * _backtest_years)

    baseline_obj_score = _compute_objective_score(
        baseline_result, objective_metric, _trade_floor,
        window_start=_win_start, window_end=_win_end,
        n_blocks=n_blocks, lambda_dispersion=lambda_dispersion,
    )
    print(f"  Baseline {objective_metric}: {baseline_obj_score:.4f}")

    score_label = objective_metric.capitalize()

    def trial_callback(study_: optuna.Study, trial: optuna.trial.FrozenTrial) -> None:
        if trial.state != optuna.trial.TrialState.COMPLETE:
            return
        completed = trial.number + 1
        if completed % 50 == 0:
            elapsed = time.perf_counter() - start_time
            print(f"  Trial {completed}/{n_trials}  "
                  f"{score_label}={trial.value:.3f}  [{elapsed:.0f}s]")

    _warm_tag = "baseline-seeded" if _warm_start_ok else "[WARN] COLD-START (no baseline)"
    print(f"\n--- OPTIMIZING ({n_trials} trials, n_jobs={n_jobs}) [{_warm_tag}] ---")
    send_telegram(
        f"[Strategy Optimizer] {model_name}\n"
        f"Started: {n_trials} trials (n_jobs={n_jobs})\n"
        f"Objective: {objective_metric}\n"
        f"Warm-start: {'[OK] baseline injected' if _warm_start_ok else '[WARN] COLD-START -- no baseline config provided!'}"
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
    print(f"Best trial #{best_trial.number}: {score_label}={best_trial.value:.4f}")
    print(f"  Params: {dict(best_trial.params)}")

    # End notification with best performance
    # Use the O(1) best tracker; fall back gracefully if trial mismatch
    if best_result_tracker.result is not None and best_result_tracker.trial_number == best_trial.number:
        best_m = extract_metrics(best_result_tracker.result)
        send_telegram(
            f"[Strategy Optimizer] {model_name}\n"
            f"COMPLETE ({elapsed:.0f}s / {elapsed/60:.1f}m)\n"
            f"Best Trial: #{best_trial.number}/{n_trials}\n"
            f"Best {score_label}: {best_trial.value:.4f}\n"
            f"PnL: ${best_m['total_pnl']:,.2f}  PF: {best_m['profit_factor']:.2f}\n"
            f"Trades: {best_m['trade_count']}  WR: {best_m['win_rate']:.1%}\n"
            f"DD: ${best_m['max_drawdown']:,.2f}"
        )
    else:
        send_telegram(
            f"[Strategy Optimizer] {model_name}\n"
            f"COMPLETE ({elapsed:.0f}s / {elapsed/60:.1f}m)\n"
            f"Best Trial: #{best_trial.number}/{n_trials}\n"
            f"Best {score_label}: {best_trial.value:.4f}"
        )

    # Reconstruct best config from best trial params
    best_cfg = copy.deepcopy(base_cfg)
    strategy = create_execution_strategy(best_cfg)

    if is_tiered and optimize_side:
        # Single-side: only the optimized side's params are in the trial
        side_params = {
            k.replace(f"_{optimize_side}", ""): v
            for k, v in best_trial.params.items()
            if k.endswith(f"_{optimize_side}")
        }
        # Frozen dims: the same constants every trial's cfg was built with.
        side_params.update(_FROZEN_PARAMS)
        side_params = _derive_trailing_params(side_params)
        strategy.apply_trial_params(best_cfg, side_params, side=optimize_side)
        best_cfg["conflict_resolution"] = _FROZEN_CONFLICT_RESOLUTION
        # Disable the other side
        other_side = "short" if optimize_side == "long" else "long"
        if other_side in best_cfg and "tiers" in best_cfg[other_side]:
            for tier in best_cfg[other_side]["tiers"]:
                tier["min_prob"] = 1.0
    elif is_tiered:
        # Split suffixed params back into per-side dicts
        long_params = {k.replace("_long", ""): v for k, v in best_trial.params.items() if k.endswith("_long")}
        short_params = {k.replace("_short", ""): v for k, v in best_trial.params.items() if k.endswith("_short")}
        # Tied ATR (audit P4): one shared suggestion, injected into both sides.
        _shared_atr = best_trial.params.get("atr_period_shared")
        if _shared_atr is not None:
            long_params["atr_period"] = _shared_atr
            short_params["atr_period"] = _shared_atr
        long_params.update(_FROZEN_PARAMS)
        short_params.update(_FROZEN_PARAMS)
        long_params = _derive_trailing_params(long_params)
        short_params = _derive_trailing_params(short_params)
        strategy.apply_trial_params(best_cfg, long_params, side="long")
        strategy.apply_trial_params(best_cfg, short_params, side="short")
        # Re-apply strategy-level params (e.g. conflict_resolution) dropped by
        # the _long/_short suffix filter above, matching the objective's cfg.
        _reapply_strategy_level_params(best_cfg, dict(best_trial.params))
        best_cfg["conflict_resolution"] = _FROZEN_CONFLICT_RESOLUTION
    else:
        params = dict(best_trial.params)
        params.update(_FROZEN_PARAMS)
        params = _derive_trailing_params(params)
        strategy.apply_trial_params(best_cfg, params)

    # Final backtest with best params
    opt_label = f"Optimized_{optimize_side}" if optimize_side else "Optimized_Ensemble"
    best_engine = BacktestEngine.from_config(best_cfg, **overrides)
    best_result = best_engine.run(predictions_df, ohlcv_df, ohlcv_exec_df=ohlcv_exec_df, label=opt_label)
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

    # ── Regression guard ──────────────────────────────────────────────
    best_obj_score = _compute_objective_score(
        best_result, objective_metric, _trade_floor,
        window_start=_win_start, window_end=_win_end,
        n_blocks=n_blocks, lambda_dispersion=lambda_dispersion,
    )
    _regression_triggered = False
    if best_obj_score <= baseline_obj_score:
        _regression_triggered = True
        print(f"\n  [WARN] REGRESSION GUARD: Optuna best ({best_obj_score:.4f}) did not beat "
              f"baseline ({baseline_obj_score:.4f}). Returning baseline config.")
        send_telegram(
            f"[Strategy Optimizer] {model_name}\n"
            f"[WARN] REGRESSION GUARD TRIGGERED\n"
            f"Optuna best {score_label}: {best_obj_score:.4f}\n"
            f"Baseline {score_label}: {baseline_obj_score:.4f}\n"
            f"Returning baseline config."
        )
        best_cfg = copy.deepcopy(base_cfg)
        best_result = baseline_result
        best_metrics = baseline_metrics
    else:
        print(f"\n  [OK] Optimizer improved over baseline: "
              f"{best_obj_score:.4f} > {baseline_obj_score:.4f}")

    # ── Per-block diagnostics (all arms, incl. sharpe) ────────────────
    # Shows WHERE the score comes from: which block binds under block_min,
    # dispersion under block_mean_std; lets block-min filter a sharpe arm.
    _block_details = None
    if _insample_months >= n_blocks:
        _block_details = _block_sharpe_details(
            monthly_pnls=_monthly_pnl_series(best_result),
            window_start=_win_start, window_end=_win_end, n_blocks=n_blocks,
        )
        print(f"  Block Sharpes ({n_blocks} blocks): "
              + "/".join(f"{s:.2f}" for s in _block_details["block_sharpes"]))
    else:
        print(f"  [WARN] in-sample window has {_insample_months} months < "
              f"n_blocks={n_blocks} — block diagnostics skipped.")

    # Build optuna_info
    # Build optuna_info — shared base fields
    _optuna_base = {
        "trial_number": best_trial.number,
        "timestamp": pd.Timestamp.now().isoformat(timespec="seconds"),
        "optimizer": "strategy_optimizer",
        "objective": objective_metric,
        "n_trials": n_trials,
        "consistency_score": best_trial.value,
        "baseline_metrics": baseline_metrics,
        "baseline_obj_score": round(baseline_obj_score, 4),
        "best_obj_score": round(best_obj_score, 4),
        "regression_guard_triggered": _regression_triggered,
        "warm_start_injected": _warm_start_ok,
        "wall_time_seconds": round(elapsed, 1),
        "block_sharpes": _block_details["block_sharpes"] if _block_details else None,
        "block_bounds": _block_details["block_bounds"] if _block_details else None,
    }

    if is_tiered and optimize_side:
        best_cfg["optuna_info"] = {
            **_optuna_base,
            "mode": f"single_side_{optimize_side}",
            "optimize_side": optimize_side,
            "params": side_params if not _regression_triggered else {},
            "all_trial_params": dict(best_trial.params),
            "metrics": best_metrics,
        }
    elif is_tiered:
        best_cfg["optuna_info"] = {
            **_optuna_base,
            "mode": "simultaneous_ensemble",
            "long_params": long_params if not _regression_triggered else {},
            "short_params": short_params if not _regression_triggered else {},
            "params": dict(best_trial.params),
            "ensemble_metrics": best_metrics,
        }
    else:
        best_cfg["optuna_info"] = {
            **_optuna_base,
            "mode": "single_config",
            "params": dict(best_trial.params),
            "metrics": best_metrics,
        }

    # ── Holdout backtest (unseen by Optuna) ────────────────────────────
    if holdout_preds is not None and len(holdout_preds) > 0:
        print(f"\n--- HOLDOUT BACKTEST ({_holdout_months} months, unseen by optimizer) ---")
        holdout_engine = BacktestEngine.from_config(best_cfg, **overrides)
        holdout_result = holdout_engine.run(holdout_preds, ohlcv_df, ohlcv_exec_df=ohlcv_exec_df, label="Holdout")
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

    # Save optimized config — filename includes side and objective for namespacing
    if _holdout_months > 0:
        best_cfg["holdout_months"] = _holdout_months

    suffix_parts = []
    if optimize_side:
        suffix_parts.append(optimize_side)
    suffix_parts.append(objective_metric)
    opt_suffix = "_".join(suffix_parts)

    opt_config_name = Path(config_path).stem + f"_opt_{opt_suffix}.json"
    opt_config_path = os.path.join(os.path.dirname(config_path), opt_config_name)
    with open(opt_config_path, "w") as f:
        json.dump(best_cfg, f, indent=4)
    print(f"\nSaved optimized config: {opt_config_path}")

    return best_cfg, best_result


# ---------------------------------------------------------------------------
# Two-Stage Hybrid Optimizer
# ---------------------------------------------------------------------------


def run_hybrid_optimization(
    config_path: str,
    n_trials: int = 150,
    predictions_path: str | None = None,
    ohlcv_path: str | None = None,
    holdout_months: int | None = None,
    n_jobs: int = 1,
    quiet: bool = False,
    label: str = "",
    objective_metric: str = "sharpe",
    optimize_side: str | None = None,
    exec_ohlcv_path: str | None = None,
    slippage_per_side: float | None = None,
    vbt_top_n: int = 20,
    random_seed: int = 42,
    symbol: str | None = None,
    firing_frac_min: float = FIRING_FRAC_MIN,
    firing_frac_max: float = FIRING_FRAC_MAX,
    n_blocks: int = 3,
    lambda_dispersion: float = 1.0,
    min_block_months: int = 10,
) -> tuple[dict, BacktestResult]:
    """Two-Stage Hybrid optimizer: vectorbt pre-screen → Optuna warm-start.

    Drop-in replacement for run_optimization() with a reduced n_trials budget.

    Stage 1 (vectorbt): Fast grid sweep over entry_threshold x tp_atr_mult x
        sl_atr_mult x max_hold_bars. Simplified model — no trailing stop,
        cooldown, or tiered dispatch.

    Stage 2 (Optuna + BacktestEngine): Full-fidelity optimization warm-started
        from Stage 1 survivors. Uses study.enqueue_trial() to seed TPE with
        the top vbt_top_n configurations before free exploration begins.

    ATR Cache: Pre-computed via build_atr_cache() for Stage 1 only.
        BacktestEngine.run() always recomputes ATR internally — this cache
        does NOT bypass engine internals (zero changes to backtest_engine.py).

    Args:
        n_trials: Stage 2 Optuna trial budget AFTER warm-start injection.
            Total effective trials = vbt_top_n (enqueued) + n_trials (TPE).
        optimize_side: "long", "short", or None (both sides). When None,
            Stage 1 skips prescreening (vbt only supports single-side) and
            falls back to straight warm-start with default mid-range params.
        vbt_top_n: Number of Stage 1 configs to inject via enqueue_trial.

    Returns:
        Tuple of (best_config, best_result) — same format as run_optimization().
    """
    start_time = time.perf_counter()

    # Seed numpy for reproducibility (Optuna sampler seeded separately below).
    np.random.seed(random_seed)

    from src.live_execution.config_loader import load_strategy_config
    base_cfg = load_strategy_config(config_path)

    model_name = base_cfg.get("nickname", Path(config_path).stem)
    if label:
        model_name = f"{label} | {model_name}"
    is_tiered = (
        base_cfg.get("execution_class") == "TieredEnsembleStrategy"
        and base_cfg.get("long", {}).get("tiers")
        and base_cfg.get("short", {}).get("tiers")
    )

    if optimize_side:
        mode_str = f"HYBRID SINGLE-SIDE ({optimize_side.upper()})"
    elif is_tiered:
        mode_str = "HYBRID SIMULTANEOUS ENSEMBLE"
    else:
        mode_str = "HYBRID SINGLE CONFIG"

    if objective_metric == "sortino":
        obj_str = "Annualized Monthly Sortino"
    elif objective_metric in BLOCK_OBJECTIVE_METRICS:
        obj_str = (f"Block-wise Annualized Monthly Sharpe ({objective_metric}, "
                   f"n_blocks={n_blocks}, lambda={lambda_dispersion})")
    else:
        obj_str = "Annualized Monthly Sharpe"

    print("=" * 70)
    print(f"HYBRID STRATEGY OPTIMIZATION: {model_name}")
    print(f"  Config: {config_path}")
    print(f"  Stage 1 top-N: {vbt_top_n} | Stage 2 trials: {n_trials}")
    print(f"  Mode: {mode_str}")
    print(f"  Objective: {obj_str}")
    print("=" * 70)

    # ── Load config, OHLCV, predictions (mirrors run_optimization) ──────────
    training_info = base_cfg.get("training_info", {})
    if ohlcv_path is None:
        ohlcv_path = training_info.get("data", "data/processed/CL_set_06.parquet")

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
    if exec_ohlcv_path:
        print(f"  EXEC data: {exec_ohlcv_path}")

    print("\nLoading data...")
    ohlcv_df, ohlcv_exec_df = load_ohlcv_dual(ohlcv_path) if exec_ohlcv_path is None else (load_ohlcv_dual(ohlcv_path)[0], None)
    if exec_ohlcv_path:
        ohlcv_exec_df = load_ohlcv(exec_ohlcv_path)
    ohlcv_df = attach_atr_cache(ohlcv_df)   # stamp ATR_{period} cols once; BacktestEngine skips recomputation per trial
    print(f"  Predictions: {len(predictions_df):,} rows  cols={list(predictions_df.columns)}")
    print(f"  OHLCV: {len(ohlcv_df):,} rows")
    print(f"  Date range: {predictions_df.index.min()} to {predictions_df.index.max()}")

    # ── Holdout split ────────────────────────────────────────────────────────
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

    # ── In-sample window bounds (post-holdout, fixed for every trial) ────────
    _win_start = predictions_df.index.min()
    _win_end = predictions_df.index.max()
    _insample_months = (_win_end.to_period("M") - _win_start.to_period("M")).n + 1
    if objective_metric in BLOCK_OBJECTIVE_METRICS:
        _check_block_window(objective_metric, _win_start, _win_end,
                            _insample_months, n_blocks, min_block_months)

    # ── Baseline ─────────────────────────────────────────────────────────────
    print("\n--- BASELINE ---")
    baseline_cfg = copy.deepcopy(base_cfg)
    if optimize_side and is_tiered:
        other_side = "short" if optimize_side == "long" else "long"
        if other_side in baseline_cfg and "tiers" in baseline_cfg[other_side]:
            for tier in baseline_cfg[other_side]["tiers"]:
                tier["min_prob"] = 1.0
    contract_multiplier, slippage_per_side = _resolve_symbol_economics(symbol, slippage_per_side)
    if symbol:
        print(f"  Economics: symbol={symbol}  $/pt={contract_multiplier}  slippage/side={slippage_per_side}")

    overrides = {}
    if slippage_per_side is not None:
        overrides["slippage_per_side"] = slippage_per_side
    if contract_multiplier is not None:
        overrides["contract_multiplier"] = contract_multiplier

    baseline_engine = BacktestEngine.from_config(baseline_cfg, **overrides)
    baseline_result = baseline_engine.run(predictions_df, ohlcv_df, ohlcv_exec_df=ohlcv_exec_df, label="Baseline")
    baseline_metrics = extract_metrics(baseline_result)
    print(f"  PnL: ${baseline_metrics['total_pnl']:,.2f}  "
          f"PF: {baseline_metrics['profit_factor']:.2f}  "
          f"WR: {baseline_metrics['win_rate']:.1%}  "
          f"Trades: {baseline_metrics['trade_count']}  "
          f"DD: ${baseline_metrics['max_drawdown']:,.2f}")

    # ── Stage 1: vectorbt coarse grid sweep ──────────────────────────────────
    if optimize_side in ("long", "short") and _VBT_AVAILABLE:
        print("\n--- STAGE 1: vectorbt coarse grid sweep ---")
        # Block metrics prescreen on plain sharpe: seeds are starting points,
        # not selection — the study itself scores block-wise.
        _vbt_metric = "sharpe" if objective_metric in BLOCK_OBJECTIVE_METRICS else objective_metric
        stage1_configs = run_vbt_prescreener(
            predictions_df=predictions_df,
            ohlcv_df=ohlcv_df,
            optimize_side=optimize_side,
            objective_metric=_vbt_metric,
            top_n=vbt_top_n,
            contract_multiplier=contract_multiplier if contract_multiplier is not None else 1000.0,
        )
        print(f"  Stage 1 complete: {len(stage1_configs)} configs to warm-start")
    else:
        stage1_configs = []
        if not _VBT_AVAILABLE and optimize_side in ("long", "short"):
            print("  [WARNING] vectorbt not available — skipping Stage 1 prescreening")
        elif optimize_side is None:
            print("  Stage 1 skipped: optimize_side=None (vbt supports single-side only)")

    # ── Stage 2: Optuna warm-started from Stage 1 ────────────────────────────
    score_label = objective_metric.capitalize()

    # Precompute per-side dynamic entry-threshold bounds ONCE on the optimizer
    # window and share them between the objective and the warm-start extraction
    # (3-way consistency invariant).
    entry_thr_bounds = _compute_entry_thr_bounds(
        predictions_df, firing_frac_min, firing_frac_max
    )

    best_result_tracker = BestResultTracker()
    tracker = TopKTracker(k=5, save_dir="configs/strategies/candidates")
    objective = make_objective(
        base_cfg, predictions_df, ohlcv_df, ohlcv_exec_df=ohlcv_exec_df, best_tracker=best_result_tracker,
        tracker=tracker, objective_metric=objective_metric,
        optimize_side=optimize_side, slippage_per_side=slippage_per_side,
        contract_multiplier=contract_multiplier,
        f_min=firing_frac_min, f_max=firing_frac_max, entry_thr_bounds=entry_thr_bounds,
        n_blocks=n_blocks, lambda_dispersion=lambda_dispersion,
        window_start=_win_start, window_end=_win_end,
    )

    db_hash = hashlib.md5(f"hybrid_opt_{model_name}_{objective_metric}_{os.getpid()}".encode()).hexdigest()[:8]
    db_dir = Path("reports/optuna_db")
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / f"optuna_study_{db_hash}.db"
    db_path.unlink(missing_ok=True)  # fresh study each run — avoids trial number inflation
    study = optuna.create_study(
        direction="maximize",
        study_name=f"hybrid_opt_{model_name}_{objective_metric}",
        storage=f"sqlite:///{db_path}",
        sampler=optuna.samplers.TPESampler(seed=random_seed),
    )

    # ── Warm-start: inject baseline as first trial ────────────────────
    warm_params = _extract_warm_start_params(
        base_cfg, is_tiered, optimize_side, entry_thr_bounds=entry_thr_bounds
    )
    _warm_start_ok = False
    if warm_params:
        try:
            study.enqueue_trial(warm_params)
            _warm_start_ok = True
            print(f"  Enqueued baseline config as warm-start trial #0")
        except Exception as e:
            print(f"  [WARN] Failed to enqueue baseline warm-start: {e}")
    if not _warm_start_ok:
        print("  [WARN] NO WARM-START: Optimizer will cold-start from random sampling!")

    # Inject Stage 1 VBT warm-start configs (after baseline)
    # AGGRESSIVE tier: the vbt grids (_VBT_*) emit params outside the shrunk
    # search space (max_hold_bars frozen, tp/sl coarsened/narrowed), so
    # enqueueing them would desynchronize the study. Injection is disabled
    # until the vbt grids are re-aligned (follow-up); the baseline warm-start
    # above still applies.
    if stage1_configs and SEARCH_SPACE_TIER == "aggressive":
        print(f"  [AGGRESSIVE-TIER] Skipping {len(stage1_configs)} Stage 1 vbt "
              f"warm-starts (vbt grids not aligned to the shrunk space)")
        stage1_configs = []
    for params in stage1_configs:
        try:
            study.enqueue_trial(params)
        except Exception as e:
            print(f"  [WARN] enqueue_trial failed for {params}: {e}")
    if stage1_configs:
        print(f"  Enqueued {len(stage1_configs)} Stage 1 warm-start trials")

    # Compute baseline objective score for regression guard
    _floor_rate = TRADES_PER_YEAR_FLOOR_SINGLE if optimize_side else TRADES_PER_YEAR_FLOOR
    _backtest_years = (predictions_df.index.max() - predictions_df.index.min()).days / 365.25
    _trade_floor = max(1.0, _floor_rate * _backtest_years)

    baseline_obj_score = _compute_objective_score(
        baseline_result, objective_metric, _trade_floor,
        window_start=_win_start, window_end=_win_end,
        n_blocks=n_blocks, lambda_dispersion=lambda_dispersion,
    )
    print(f"  Baseline {objective_metric}: {baseline_obj_score:.4f}")

    def trial_callback(study_: optuna.Study, trial: optuna.trial.FrozenTrial) -> None:
        if trial.state != optuna.trial.TrialState.COMPLETE:
            return
        completed = trial.number + 1
        total_effective = len(stage1_configs) + n_trials
        if completed % 50 == 0:
            elapsed = time.perf_counter() - start_time
            print(f"  Trial {completed}/{total_effective}  "
                  f"{score_label}={trial.value:.3f}  [{elapsed:.0f}s]")

    _warm_tag = "baseline-seeded" if _warm_start_ok else "[WARN] COLD-START (no baseline)"
    print(f"\n--- STAGE 2: OPTIMIZING ({n_trials} TPE trials, n_jobs={n_jobs}) [{_warm_tag}] ---")
    send_telegram(
        f"[Hybrid Optimizer] {model_name}\n"
        f"Stage 1: {len(stage1_configs)} warm-start configs\n"
        f"Stage 2: {n_trials} trials (n_jobs={n_jobs})\n"
        f"Objective: {objective_metric}\n"
        f"Warm-start: {'[OK] baseline injected' if _warm_start_ok else '[WARN] COLD-START -- no baseline config provided!'}"
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
    print(f"Best trial #{best_trial.number}: {score_label}={best_trial.value:.4f}")
    print(f"  Params: {dict(best_trial.params)}")

    if best_result_tracker.result is not None and best_result_tracker.trial_number == best_trial.number:
        best_m = extract_metrics(best_result_tracker.result)
        send_telegram(
            f"[Hybrid Optimizer] {model_name}\n"
            f"COMPLETE ({elapsed:.0f}s / {elapsed/60:.1f}m)\n"
            f"Best Trial: #{best_trial.number}\n"
            f"Best {score_label}: {best_trial.value:.4f}\n"
            f"PnL: ${best_m['total_pnl']:,.2f}  PF: {best_m['profit_factor']:.2f}\n"
            f"Trades: {best_m['trade_count']}  WR: {best_m['win_rate']:.1%}\n"
            f"DD: ${best_m['max_drawdown']:,.2f}"
        )
    else:
        send_telegram(
            f"[Hybrid Optimizer] {model_name}\n"
            f"COMPLETE ({elapsed:.0f}s / {elapsed/60:.1f}m)\n"
            f"Best Trial: #{best_trial.number}\n"
            f"Best {score_label}: {best_trial.value:.4f}"
        )

    # ── Reconstruct best config ──────────────────────────────────────────────
    best_cfg = copy.deepcopy(base_cfg)
    strategy = create_execution_strategy(best_cfg)

    if is_tiered and optimize_side:
        side_params = {
            k.replace(f"_{optimize_side}", ""): v
            for k, v in best_trial.params.items()
            if k.endswith(f"_{optimize_side}")
        }
        side_params.update(_FROZEN_PARAMS)
        side_params = _derive_trailing_params(side_params)
        strategy.apply_trial_params(best_cfg, side_params, side=optimize_side)
        best_cfg["conflict_resolution"] = _FROZEN_CONFLICT_RESOLUTION
        other_side = "short" if optimize_side == "long" else "long"
        if other_side in best_cfg and "tiers" in best_cfg[other_side]:
            for tier in best_cfg[other_side]["tiers"]:
                tier["min_prob"] = 1.0
    elif is_tiered:
        long_params = {k.replace("_long", ""): v for k, v in best_trial.params.items() if k.endswith("_long")}
        short_params = {k.replace("_short", ""): v for k, v in best_trial.params.items() if k.endswith("_short")}
        _shared_atr = best_trial.params.get("atr_period_shared")
        if _shared_atr is not None:
            long_params["atr_period"] = _shared_atr
            short_params["atr_period"] = _shared_atr
        long_params.update(_FROZEN_PARAMS)
        short_params.update(_FROZEN_PARAMS)
        long_params = _derive_trailing_params(long_params)
        short_params = _derive_trailing_params(short_params)
        strategy.apply_trial_params(best_cfg, long_params, side="long")
        strategy.apply_trial_params(best_cfg, short_params, side="short")
        # Re-apply strategy-level params (e.g. conflict_resolution) dropped by
        # the _long/_short suffix filter above, matching the objective's cfg.
        _reapply_strategy_level_params(best_cfg, dict(best_trial.params))
        best_cfg["conflict_resolution"] = _FROZEN_CONFLICT_RESOLUTION
    else:
        params = dict(best_trial.params)
        params.update(_FROZEN_PARAMS)
        params = _derive_trailing_params(params)
        strategy.apply_trial_params(best_cfg, params)

    # ── Final backtest ───────────────────────────────────────────────────────
    opt_label = f"Hybrid_Optimized_{optimize_side}" if optimize_side else "Hybrid_Optimized_Ensemble"
    best_engine = BacktestEngine.from_config(best_cfg, **overrides)
    best_result = best_engine.run(predictions_df, ohlcv_df, ohlcv_exec_df=ohlcv_exec_df, label=opt_label)
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

    # ── Regression guard ──────────────────────────────────────────────
    best_obj_score = _compute_objective_score(
        best_result, objective_metric, _trade_floor,
        window_start=_win_start, window_end=_win_end,
        n_blocks=n_blocks, lambda_dispersion=lambda_dispersion,
    )
    _regression_triggered = False
    if best_obj_score <= baseline_obj_score:
        _regression_triggered = True
        print(f"\n  [WARN] REGRESSION GUARD: Optuna best ({best_obj_score:.4f}) did not beat "
              f"baseline ({baseline_obj_score:.4f}). Returning baseline config.")
        send_telegram(
            f"[Hybrid Optimizer] {model_name}\n"
            f"[WARN] REGRESSION GUARD TRIGGERED\n"
            f"Optuna best {score_label}: {best_obj_score:.4f}\n"
            f"Baseline {score_label}: {baseline_obj_score:.4f}\n"
            f"Returning baseline config."
        )
        best_cfg = copy.deepcopy(base_cfg)
        best_result = baseline_result
        best_metrics = baseline_metrics
    else:
        print(f"\n  [OK] Optimizer improved over baseline: "
              f"{best_obj_score:.4f} > {baseline_obj_score:.4f}")

    # ── Per-block diagnostics (all arms, incl. sharpe) ───────────────────────
    _block_details = None
    if _insample_months >= n_blocks:
        _block_details = _block_sharpe_details(
            monthly_pnls=_monthly_pnl_series(best_result),
            window_start=_win_start, window_end=_win_end, n_blocks=n_blocks,
        )
        print(f"  Block Sharpes ({n_blocks} blocks): "
              + "/".join(f"{s:.2f}" for s in _block_details["block_sharpes"]))
    else:
        print(f"  [WARN] in-sample window has {_insample_months} months < "
              f"n_blocks={n_blocks} — block diagnostics skipped.")

    # ── Build optuna_info (tagged as "hybrid") ───────────────────────────────
    _optuna_info_base = {
        "trial_number": best_trial.number,
        "timestamp": pd.Timestamp.now().isoformat(timespec="seconds"),
        "optimizer": "strategy_optimizer",
        "objective": objective_metric,
        "n_trials": n_trials,
        "vbt_top_n": vbt_top_n,
        "stage1_configs_count": len(stage1_configs),
        "consistency_score": best_trial.value,
        "baseline_metrics": baseline_metrics,
        "baseline_obj_score": round(baseline_obj_score, 4),
        "best_obj_score": round(best_obj_score, 4),
        "regression_guard_triggered": _regression_triggered,
        "warm_start_injected": _warm_start_ok,
        "wall_time_seconds": round(elapsed, 1),
        "block_sharpes": _block_details["block_sharpes"] if _block_details else None,
        "block_bounds": _block_details["block_bounds"] if _block_details else None,
    }
    if is_tiered and optimize_side:
        best_cfg["optuna_info"] = {
            **_optuna_info_base,
            "mode": f"hybrid_single_side_{optimize_side}",
            "optimize_side": optimize_side,
            "params": side_params if not _regression_triggered else {},
            "all_trial_params": dict(best_trial.params),
            "metrics": best_metrics,
        }
    elif is_tiered:
        best_cfg["optuna_info"] = {
            **_optuna_info_base,
            "mode": "hybrid_simultaneous_ensemble",
            "long_params": long_params if not _regression_triggered else {},
            "short_params": short_params if not _regression_triggered else {},
            "params": dict(best_trial.params),
            "ensemble_metrics": best_metrics,
        }
    else:
        best_cfg["optuna_info"] = {
            **_optuna_info_base,
            "mode": "hybrid_single_config",
            "params": dict(best_trial.params),
            "metrics": best_metrics,
        }

    # ── Holdout backtest ─────────────────────────────────────────────────────
    if holdout_preds is not None and len(holdout_preds) > 0:
        print(f"\n--- HOLDOUT BACKTEST ({_holdout_months} months, unseen by optimizer) ---")
        holdout_engine = BacktestEngine.from_config(best_cfg, **overrides)
        holdout_result = holdout_engine.run(holdout_preds, ohlcv_df, ohlcv_exec_df=ohlcv_exec_df, label="Holdout")
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

    # ── Save optimized config ────────────────────────────────────────────────
    if _holdout_months > 0:
        best_cfg["holdout_months"] = _holdout_months

    suffix_parts = []
    if optimize_side:
        suffix_parts.append(optimize_side)
    suffix_parts.append(objective_metric)
    opt_suffix = "_".join(suffix_parts)

    opt_config_name = Path(config_path).stem + f"_hybrid_{opt_suffix}.json"
    opt_config_path = os.path.join(os.path.dirname(config_path), opt_config_name)
    with open(opt_config_path, "w") as f:
        json.dump(best_cfg, f, indent=4)
    print(f"\nSaved hybrid-optimized config: {opt_config_path}")

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
        "--n-trials", type=int, default=500,
        help="Number of Optuna trials (default: 500)"
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
        "--exec-data", default=None,
        help="Optional: path to raw unadjusted execution data"
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
    parser.add_argument(
        "--objective",
        choices=["sharpe", "sortino", "block_min", "block_median", "block_mean_std"],
        default="sharpe",
        help="Objective function: sharpe (default), sortino, or a block-wise "
             "Sharpe aggregate (block_min / block_median / block_mean_std)"
    )
    parser.add_argument(
        "--side", choices=["long", "short", "both"], default="both",
        help="Which side to optimize: long, short, or both (default: both)"
    )
    parser.add_argument(
        "--random-seed", type=int, default=42,
        help="Seed for the Optuna TPE sampler and numpy RNG (default: 42). "
             "Same seed => identical study best_trial.params."
    )
    parser.add_argument(
        "--firing-frac-min", type=float, default=FIRING_FRAC_MIN,
        help=f"Min signal-firing fraction for the dynamic entry_threshold "
             f"ceiling (default: {FIRING_FRAC_MIN}). Smaller => more selective."
    )
    parser.add_argument(
        "--firing-frac-max", type=float, default=FIRING_FRAC_MAX,
        help=f"Max signal-firing fraction for the dynamic entry_threshold "
             f"floor (default: {FIRING_FRAC_MAX}). Larger => more permissive."
    )
    args = parser.parse_args()

    _side = None if args.side == "both" else args.side

    run_optimization(
        config_path=args.config,
        n_trials=args.n_trials,
        predictions_path=args.predictions,
        ohlcv_path=args.data,
        holdout_months=args.holdout_months,
        n_jobs=args.jobs,
        objective_metric=args.objective,
        optimize_side=_side,
        exec_ohlcv_path=args.exec_data,
        random_seed=args.random_seed,
        firing_frac_min=args.firing_frac_min,
        firing_frac_max=args.firing_frac_max,
    )


if __name__ == "__main__":
    logging.getLogger("src.live_execution.execution_guard").setLevel(logging.ERROR)
    main()
