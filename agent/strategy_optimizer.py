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
# Optional vectorbt import — used only by Stage 1 prescreener
# ---------------------------------------------------------------------------
try:
    import vectorbt as vbt
    _VBT_AVAILABLE = True
except ImportError:
    _VBT_AVAILABLE = False


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

TRADES_PER_YEAR_FLOOR = 36  # Minimum ~3 trades/month combined (long+short)
TRADES_PER_YEAR_FLOOR_SINGLE = 18  # Minimum ~1.5 trades/month for single-side optimization


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
        periods = list(range(10, 52, 2))
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
_VBT_MAX_HOLD_BARS_LIST = [24, 48, 96, 144, 192, 240]


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
                            neg = ret_vals[ret_vals < 0]
                            if len(neg) == 0 or np.std(neg) < 1e-12:
                                score = 10.0 if mean_r > 0 else 0.0
                            else:
                                score = float(mean_r / np.std(neg) * np.sqrt(bars_per_year))
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
# Optuna objective
# ---------------------------------------------------------------------------


def make_objective(
    base_cfg: dict,
    predictions_df: pd.DataFrame,
    ohlcv_df: pd.DataFrame,
    best_tracker: BestResultTracker | None = None,
    tracker: "TopKTracker | None" = None,
    objective_metric: str = "sharpe",
    optimize_side: str | None = None,
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
    """
    # Pre-create a strategy instance for parameter routing
    strategy = create_execution_strategy(base_cfg)
    is_tiered = (
        base_cfg.get("execution_class") == "TieredEnsembleStrategy"
        and base_cfg.get("long", {}).get("tiers")
        and base_cfg.get("short", {}).get("tiers")
    )

    # Compute trade floor from prediction data span (once, not per trial)
    # Use halved floor for single-side optimization (18/year vs 36/year)
    _floor_rate = TRADES_PER_YEAR_FLOOR_SINGLE if optimize_side else TRADES_PER_YEAR_FLOOR
    _backtest_years = (predictions_df.index.max() - predictions_df.index.min()).days / 365.25
    _trade_floor = max(1.0, _floor_rate * _backtest_years)

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

    def _disable_side(cfg: dict, side_to_disable: str) -> None:
        """Disable a side by setting all tier min_prob to 1.0."""
        if side_to_disable in cfg and "tiers" in cfg[side_to_disable]:
            for tier in cfg[side_to_disable]["tiers"]:
                tier["min_prob"] = 1.0

    def objective(trial: optuna.Trial) -> float:
        cfg = copy.deepcopy(base_cfg)

        if is_tiered:
            # Conflict resolution: strategy-level param (not per-side)
            conflict_mode = trial.suggest_categorical(
                "conflict_resolution",
                ["hold", "close_existing_position", "reverse_position"],
            )
            cfg["conflict_resolution"] = conflict_mode

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

        if objective_metric == "sortino":
            # --- Annualized Monthly Sortino ---
            neg_pnls = monthly_pnl_vals[monthly_pnl_vals < 0]
            if len(neg_pnls) == 0 or float(np.std(neg_pnls)) < 1e-9:
                annualized_score = 10.0  # cap to avoid inf
            else:
                annualized_score = float(
                    (np.mean(monthly_pnl_vals) / np.std(neg_pnls)) * np.sqrt(12)
                )
        else:
            # --- Annualized Monthly Sharpe ---
            std_pnl = float(np.std(monthly_pnl_vals))
            if std_pnl < 1e-9:
                return -9999.0
            annualized_score = float(
                (np.mean(monthly_pnl_vals) / std_pnl) * np.sqrt(12)
            )

        # --- Trade Floor Penalty ---
        # Negative score returned as-is (multiplying by weight < 1 would
        # *improve* a negative score — the opposite of the intended effect).
        if annualized_score > 0:
            weight = _trade_floor_weight(result.trade_count, _trade_floor)
            final_score = annualized_score * weight
        else:
            final_score = annualized_score

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
) -> tuple[dict, BacktestResult]:
    """Run strategy parameter optimization.

    Args:
        holdout_months: If set, reserve the last N months of predictions
            as an unseen holdout.  Optuna only sees data before the cutoff.
            Falls back to config key ``holdout_months`` when *None*.
        objective_metric: "sharpe" or "sortino".
        optimize_side: "long", "short", or None (both sides / ensemble).

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

    # Determine optimization mode description
    if optimize_side:
        mode_str = f"SINGLE-SIDE ({optimize_side.upper()})"
    elif is_tiered:
        mode_str = "SIMULTANEOUS ENSEMBLE"
    else:
        mode_str = "SINGLE CONFIG"

    obj_str = "Annualized Monthly Sortino" if objective_metric == "sortino" else "Annualized Monthly Sharpe"

    print("=" * 70)
    print(f"STRATEGY PARAMETER OPTIMIZATION: {model_name}")
    print(f"  Config: {config_path}")
    print(f"  Trials: {n_trials}")
    print(f"  Mode: {mode_str}")
    print(f"  Objective: {obj_str}")
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

    # Run baseline (with side-appropriate config for fair comparison)
    print("\n--- BASELINE ---")
    baseline_cfg = copy.deepcopy(base_cfg)
    if optimize_side and is_tiered:
        # Disable the other side for a fair baseline comparison
        other_side = "short" if optimize_side == "long" else "long"
        if other_side in baseline_cfg and "tiers" in baseline_cfg[other_side]:
            for tier in baseline_cfg[other_side]["tiers"]:
                tier["min_prob"] = 1.0
    baseline_engine = BacktestEngine.from_config(baseline_cfg)
    baseline_result = baseline_engine.run(predictions_df, ohlcv_df, label="Baseline")
    baseline_metrics = extract_metrics(baseline_result)
    print(f"  PnL: ${baseline_metrics['total_pnl']:,.2f}  "
          f"PF: {baseline_metrics['profit_factor']:.2f}  "
          f"WR: {baseline_metrics['win_rate']:.1%}  "
          f"Trades: {baseline_metrics['trade_count']}  "
          f"DD: ${baseline_metrics['max_drawdown']:,.2f}")

    # ── Optimization ─────────────────────────────────────────────────
    best_result_tracker = BestResultTracker()
    tracker = TopKTracker(k=5, save_dir="configs/strategies/candidates")
    objective = make_objective(
        base_cfg, predictions_df, ohlcv_df, best_result_tracker, tracker=tracker,
        objective_metric=objective_metric, optimize_side=optimize_side,
    )

    db_hash = hashlib.md5(f"strategy_opt_{model_name}_{objective_metric}".encode()).hexdigest()[:8]
    study = optuna.create_study(
        direction="maximize",
        study_name=f"strategy_opt_{model_name}_{objective_metric}",
        storage=f"sqlite:///optuna_study_{db_hash}.db",
        load_if_exists=True,
    )

    score_label = objective_metric.capitalize()

    def trial_callback(study_: optuna.Study, trial: optuna.trial.FrozenTrial) -> None:
        if trial.state != optuna.trial.TrialState.COMPLETE:
            return
        completed = trial.number + 1
        if completed % 50 == 0:
            elapsed = time.perf_counter() - start_time
            print(f"  Trial {completed}/{n_trials}  "
                  f"{score_label}={trial.value:.3f}  [{elapsed:.0f}s]")

    print(f"\n--- OPTIMIZING ({n_trials} trials, n_jobs={n_jobs}) ---")
    send_telegram(
        f"[Strategy Optimizer] {model_name}\n"
        f"Started: {n_trials} trials (n_jobs={n_jobs})\n"
        f"Objective: {objective_metric}"
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
        strategy.apply_trial_params(best_cfg, side_params, side=optimize_side)
        # Disable the other side
        other_side = "short" if optimize_side == "long" else "long"
        if other_side in best_cfg and "tiers" in best_cfg[other_side]:
            for tier in best_cfg[other_side]["tiers"]:
                tier["min_prob"] = 1.0
    elif is_tiered:
        # Split suffixed params back into per-side dicts
        long_params = {k.replace("_long", ""): v for k, v in best_trial.params.items() if k.endswith("_long")}
        short_params = {k.replace("_short", ""): v for k, v in best_trial.params.items() if k.endswith("_short")}
        strategy.apply_trial_params(best_cfg, long_params, side="long")
        strategy.apply_trial_params(best_cfg, short_params, side="short")
    else:
        strategy.apply_trial_params(best_cfg, dict(best_trial.params))

    # Final backtest with best params
    opt_label = f"Optimized_{optimize_side}" if optimize_side else "Optimized_Ensemble"
    best_engine = BacktestEngine.from_config(best_cfg)
    best_result = best_engine.run(predictions_df, ohlcv_df, label=opt_label)
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

    # Build optuna_info
    if is_tiered and optimize_side:
        best_cfg["optuna_info"] = {
            "trial_number": best_trial.number,
            "timestamp": pd.Timestamp.now().isoformat(timespec="seconds"),
            "optimizer": "strategy_optimizer",
            "mode": f"single_side_{optimize_side}",
            "objective": objective_metric,
            "optimize_side": optimize_side,
            "n_trials": n_trials,
            "params": side_params,
            "all_trial_params": dict(best_trial.params),
            "consistency_score": best_trial.value,
            "metrics": best_metrics,
            "baseline_metrics": baseline_metrics,
            "wall_time_seconds": round(elapsed, 1),
        }
    elif is_tiered:
        best_cfg["optuna_info"] = {
            "trial_number": best_trial.number,
            "timestamp": pd.Timestamp.now().isoformat(timespec="seconds"),
            "optimizer": "strategy_optimizer",
            "mode": "simultaneous_ensemble",
            "objective": objective_metric,
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
            "objective": objective_metric,
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
    vbt_top_n: int = 20,
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

    if optimize_side:
        mode_str = f"HYBRID SINGLE-SIDE ({optimize_side.upper()})"
    elif is_tiered:
        mode_str = "HYBRID SIMULTANEOUS ENSEMBLE"
    else:
        mode_str = "HYBRID SINGLE CONFIG"

    obj_str = "Annualized Monthly Sortino" if objective_metric == "sortino" else "Annualized Monthly Sharpe"

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

    print("\nLoading data...")
    ohlcv_df = load_ohlcv(ohlcv_path)
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

    # ── Baseline ─────────────────────────────────────────────────────────────
    print("\n--- BASELINE ---")
    baseline_cfg = copy.deepcopy(base_cfg)
    if optimize_side and is_tiered:
        other_side = "short" if optimize_side == "long" else "long"
        if other_side in baseline_cfg and "tiers" in baseline_cfg[other_side]:
            for tier in baseline_cfg[other_side]["tiers"]:
                tier["min_prob"] = 1.0
    baseline_engine = BacktestEngine.from_config(baseline_cfg)
    baseline_result = baseline_engine.run(predictions_df, ohlcv_df, label="Baseline")
    baseline_metrics = extract_metrics(baseline_result)
    print(f"  PnL: ${baseline_metrics['total_pnl']:,.2f}  "
          f"PF: {baseline_metrics['profit_factor']:.2f}  "
          f"WR: {baseline_metrics['win_rate']:.1%}  "
          f"Trades: {baseline_metrics['trade_count']}  "
          f"DD: ${baseline_metrics['max_drawdown']:,.2f}")

    # ── Stage 1: vectorbt coarse grid sweep ──────────────────────────────────
    if optimize_side in ("long", "short") and _VBT_AVAILABLE:
        print("\n--- STAGE 1: vectorbt coarse grid sweep ---")
        stage1_configs = run_vbt_prescreener(
            predictions_df=predictions_df,
            ohlcv_df=ohlcv_df,
            optimize_side=optimize_side,
            objective_metric=objective_metric,
            top_n=vbt_top_n,
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

    best_result_tracker = BestResultTracker()
    tracker = TopKTracker(k=5, save_dir="configs/strategies/candidates")
    objective = make_objective(
        base_cfg, predictions_df, ohlcv_df, best_result_tracker,
        tracker=tracker, objective_metric=objective_metric,
        optimize_side=optimize_side,
    )

    db_hash = hashlib.md5(f"hybrid_opt_{model_name}_{objective_metric}".encode()).hexdigest()[:8]
    study = optuna.create_study(
        direction="maximize",
        study_name=f"hybrid_opt_{model_name}_{objective_metric}",
        storage=f"sqlite:///optuna_study_{db_hash}.db",
        load_if_exists=True,
    )

    # Inject Stage 1 warm-start configs
    for params in stage1_configs:
        try:
            study.enqueue_trial(params)
        except Exception as e:
            print(f"  [WARN] enqueue_trial failed for {params}: {e}")
    if stage1_configs:
        print(f"  Enqueued {len(stage1_configs)} Stage 1 warm-start trials")

    def trial_callback(study_: optuna.Study, trial: optuna.trial.FrozenTrial) -> None:
        if trial.state != optuna.trial.TrialState.COMPLETE:
            return
        completed = trial.number + 1
        total_effective = len(stage1_configs) + n_trials
        if completed % 50 == 0:
            elapsed = time.perf_counter() - start_time
            print(f"  Trial {completed}/{total_effective}  "
                  f"{score_label}={trial.value:.3f}  [{elapsed:.0f}s]")

    print(f"\n--- STAGE 2: OPTIMIZING ({n_trials} TPE trials, n_jobs={n_jobs}) ---")
    send_telegram(
        f"[Hybrid Optimizer] {model_name}\n"
        f"Stage 1: {len(stage1_configs)} warm-start configs\n"
        f"Stage 2: {n_trials} trials (n_jobs={n_jobs})\n"
        f"Objective: {objective_metric}"
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
        strategy.apply_trial_params(best_cfg, side_params, side=optimize_side)
        other_side = "short" if optimize_side == "long" else "long"
        if other_side in best_cfg and "tiers" in best_cfg[other_side]:
            for tier in best_cfg[other_side]["tiers"]:
                tier["min_prob"] = 1.0
    elif is_tiered:
        long_params = {k.replace("_long", ""): v for k, v in best_trial.params.items() if k.endswith("_long")}
        short_params = {k.replace("_short", ""): v for k, v in best_trial.params.items() if k.endswith("_short")}
        strategy.apply_trial_params(best_cfg, long_params, side="long")
        strategy.apply_trial_params(best_cfg, short_params, side="short")
    else:
        strategy.apply_trial_params(best_cfg, dict(best_trial.params))

    # ── Final backtest ───────────────────────────────────────────────────────
    opt_label = f"Hybrid_Optimized_{optimize_side}" if optimize_side else "Hybrid_Optimized_Ensemble"
    best_engine = BacktestEngine.from_config(best_cfg)
    best_result = best_engine.run(predictions_df, ohlcv_df, label=opt_label)
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
        "wall_time_seconds": round(elapsed, 1),
    }
    if is_tiered and optimize_side:
        best_cfg["optuna_info"] = {
            **_optuna_info_base,
            "mode": f"hybrid_single_side_{optimize_side}",
            "optimize_side": optimize_side,
            "params": side_params,
            "all_trial_params": dict(best_trial.params),
            "metrics": best_metrics,
        }
    elif is_tiered:
        best_cfg["optuna_info"] = {
            **_optuna_info_base,
            "mode": "hybrid_simultaneous_ensemble",
            "long_params": long_params,
            "short_params": short_params,
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
        "--holdout-months", type=int, default=None,
        help="Reserve last N months of predictions as unseen holdout "
             "(overrides config holdout_months)"
    )
    parser.add_argument(
        "--jobs", type=int, default=1,
        help="Number of parallel Optuna trial evaluations (default: 1)"
    )
    parser.add_argument(
        "--objective", choices=["sharpe", "sortino"], default="sharpe",
        help="Objective function: sharpe (default) or sortino"
    )
    parser.add_argument(
        "--side", choices=["long", "short", "both"], default="both",
        help="Which side to optimize: long, short, or both (default: both)"
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
    )


if __name__ == "__main__":
    main()

