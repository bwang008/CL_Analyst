"""Data Access Layer for CL Analyst Dashboard.

All heavy I/O is isolated here behind @st.cache_data decorators.
"""
from __future__ import annotations
import json, re
from pathlib import Path
from typing import Any
import pandas as pd
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_BATCH_ROOT = str(PROJECT_ROOT / "reports" / "batch_runs")
DEFAULT_REGISTRY_ROOT = str(PROJECT_ROOT / "models" / "registry")


def _safe_json(path: Path) -> dict:
    """Read JSON, return {} on any error. Handles BOM from PowerShell."""
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


@st.cache_data(show_spinner="Scanning batch runs…")
def scan_batch_runs(root: str) -> list[str]:
    """Return reverse-chronologically sorted batch folder names (lightweight)."""
    p = Path(root)
    if not p.is_dir():
        return []
    return sorted(
        [d.name for d in p.iterdir() if d.is_dir() and d.name.startswith("batch_")],
        reverse=True,
    )


@st.cache_data(show_spinner="Loading optimization results…")
def load_optimization_results(batch_dir: str, objective: str) -> pd.DataFrame:
    """Parse optimization_results_{objective}.json into a flat DataFrame.

    Keys are 'experiment|side|metric'. Returns one row per key.
    """
    fp = Path(batch_dir) / f"optimization_results_{objective}.json"
    raw = _safe_json(fp) if fp.is_file() else {}
    if not raw:
        return pd.DataFrame()

    rows: list[dict[str, Any]] = []
    for key, val in raw.items():
        parts = key.split("|")
        if len(parts) != 3:
            continue
        experiment, side, ml_metric = [p.strip() for p in parts]
        m = val.get("metrics", {})
        oi = val.get("optuna_info", {})
        bl = oi.get("baseline_metrics", {})
        ho = oi.get("holdout_metrics", {})
        rows.append({
            "key": key,
            "experiment": experiment,
            "side": side,
            "ml_metric": ml_metric,
            "status": val.get("status", "UNKNOWN"),
            # Baseline (pre-optimization)
            "pre_trades": bl.get("trade_count", 0),
            "pre_pf": bl.get("profit_factor", 0.0),
            "pre_pnl": bl.get("total_pnl", 0.0),
            "pre_sharpe": bl.get("sharpe_ratio", 0.0),
            "pre_max_dd": bl.get("max_drawdown", 0.0),
            # Optimized
            "opt_trades": m.get("trade_count", 0),
            "opt_pf": m.get("profit_factor", 0.0),
            "opt_pnl": m.get("total_pnl", 0.0),
            "opt_sharpe": m.get("sharpe_ratio", 0.0),
            "opt_sortino": m.get("sortino_ratio", 0.0),
            "opt_calmar": m.get("calmar_ratio", 0.0),
            "opt_win_rate": m.get("win_rate", 0.0),
            "opt_max_dd": m.get("max_drawdown", 0.0),
            "opt_avg_trade": m.get("avg_trade_pnl", 0.0),
            "opt_avg_win": m.get("avg_win", 0.0),
            "opt_avg_loss": m.get("avg_loss", 0.0),
            "opt_largest_win": m.get("largest_win", 0.0),
            "opt_largest_loss": m.get("largest_loss", 0.0),
            "opt_avg_duration": m.get("avg_duration_bars", 0.0),
            "opt_pct_tp": m.get("pct_tp", 0.0),
            "opt_pct_sl": m.get("pct_sl", 0.0),
            "opt_pct_time": m.get("pct_time_barrier", 0.0),
            "opt_pct_trailing": m.get("pct_trailing_be", 0.0),
            "opt_profit_per_dd": m.get("profit_per_drawdown", 0.0),
            # Holdout
            "holdout_pnl": ho.get("total_pnl", 0.0),
            "holdout_pf": ho.get("profit_factor", 0.0),
            "holdout_trades": ho.get("trade_count", 0),
            "holdout_sharpe": ho.get("sharpe_ratio", 0.0),
            "holdout_win_rate": ho.get("win_rate", 0.0),
            "holdout_max_dd": ho.get("max_drawdown", 0.0),
            # Meta
            "consistency": oi.get("consistency_score", 0.0),
            "trial_number": oi.get("trial_number", -1),
            "n_trials": oi.get("n_trials", 0),
            "wall_time_s": oi.get("wall_time_seconds", 0.0),
            "holdout_months": oi.get("holdout_months", 0),
            "holdout_cutoff": oi.get("holdout_cutoff", ""),
            "params": oi.get("params", {}),
        })
    return pd.DataFrame(rows) if rows else pd.DataFrame()


@st.cache_data(show_spinner=False)
def load_batch_progress(batch_dir: str) -> dict[str, Any]:
    """Load batch_progress.json, normalising mixed-case keys."""
    fp = Path(batch_dir) / "batch_progress.json"
    raw = _safe_json(fp) if fp.is_file() else {}
    if not raw:
        return {}
    normed = []
    for entry in raw.get("experiments", []):
        n: dict[str, Any] = {}
        for k, v in entry.items():
            n[re.sub(r"(?<!^)(?=[A-Z])", "_", k).lower()] = v
        normed.append(n)
    raw["experiments"] = normed
    return raw


@st.cache_data(show_spinner=False)
def load_batch_manifest(batch_dir: str) -> dict[str, Any]:
    fp = Path(batch_dir) / "manifest.json"
    return _safe_json(fp) if fp.is_file() else {}


@st.cache_data(show_spinner="Parsing equity curve…")
def parse_ensemble_backtest(filepath: str) -> pd.DataFrame:
    """Parse monthly PnL breakdown from ensemble_backtest_*.txt.

    Resilient: returns empty DataFrame on any parsing failure.
    """
    try:
        text = Path(filepath).read_text(encoding="utf-8")
        rows = []
        for line in text.splitlines():
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 9:
                continue
            if not re.match(r"^\d{4}-\d{2}$", parts[0]):
                continue
            try:
                pnl_s = parts[7].replace("$", "").replace(",", "").strip()
                rows.append({
                    "month": parts[0],
                    "trades": int(parts[1]),
                    "buys": int(parts[2]),
                    "sells": int(parts[3]),
                    "win_rate": float(parts[4].replace("%", "")),
                    "net_pnl": float(pnl_s),
                    "pf": float(parts[8]),
                })
            except (ValueError, IndexError):
                continue
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        df["month"] = pd.to_datetime(df["month"], format="%Y-%m")
        df.sort_values("month", inplace=True)
        df.reset_index(drop=True, inplace=True)
        df["cumulative_pnl"] = df["net_pnl"].cumsum()
        df["running_max"] = df["cumulative_pnl"].cummax()
        df["drawdown"] = df["cumulative_pnl"] - df["running_max"]
        return df
    except Exception:
        return pd.DataFrame()


def _find_experiment_dir(progress: dict, label: str) -> str | None:
    """Look up the local_dir for an experiment label in batch_progress."""
    for exp in progress.get("experiments", []):
        if exp.get("label") == label and exp.get("local_dir"):
            return exp["local_dir"]
    return None


@st.cache_data(show_spinner="Scanning model registry…")
def scan_model_registry(registry_dir: str) -> pd.DataFrame:
    """Scan models/registry/ and parse experiment_config.json files."""
    root = Path(registry_dir)
    if not root.is_dir():
        return pd.DataFrame()
    rows = []
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        cfg = _safe_json(d / "experiment_config.json") if (d / "experiment_config.json").is_file() else {}
        if not cfg:
            continue
        features = cfg.get("features")
        feat_count = str(len(features)) if features else "All (201)"
        mp = cfg.get("model_params", {})
        groups = [k.replace("use_", "") for k, v in mp.items() if k.startswith("use_") and v is True]
        rows.append({
            "model_id": d.name,
            "experiment_id": cfg.get("experiment_id", d.name),
            "strategy": cfg.get("strategy", ""),
            "target": cfg.get("target_name", ""),
            "data_path": cfg.get("data_path", ""),
            "feature_count": feat_count,
            "feature_groups": ", ".join(groups) if groups else "N/A",
            "boosting": mp.get("boosting_type", ""),
            "num_leaves": mp.get("num_leaves", ""),
            "learning_rate": mp.get("learning_rate", ""),
            "max_depth": mp.get("max_depth", ""),
            "n_estimators": mp.get("n_estimators", ""),
            "has_model_pkl": (d / "final_model.pkl").exists(),
            "has_predictions": any(d.glob("oos_predictions*.csv")),
            "_raw_config": cfg,
        })
    return pd.DataFrame(rows) if rows else pd.DataFrame()


# ═══════════════════════════════════════════════════════════════
#  SIGNAL ANALYSIS DATA LOADERS
# ═══════════════════════════════════════════════════════════════

def _load_single_predictions(path: str) -> pd.DataFrame:
    """Load a single prediction CSV with flexible column names."""
    df = pd.read_csv(path)
    # Normalise datetime index
    dt_col = next((c for c in df.columns if c.lower() in ("datetime", "date")), None)
    if dt_col:
        df[dt_col] = pd.to_datetime(df[dt_col])
        df = df.set_index(dt_col)
    elif not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index)
    return df


@st.cache_data(show_spinner="Loading prediction pair…")
def load_prediction_pair(long_path: str, short_path: str) -> pd.DataFrame:
    """Merge long prob_Buy + short prob_Sell into a single aligned DataFrame.

    Returns DataFrame indexed by DateTime with columns:
        prob_Buy, prob_Sell
    Fills missing bars with 0.0 (no prediction on that bar).
    """
    long_df = _load_single_predictions(long_path)
    short_df = _load_single_predictions(short_path)

    # Resolve probability columns
    buy_col = next((c for c in long_df.columns if "buy" in c.lower()), None)
    sell_col = next((c for c in short_df.columns if "sell" in c.lower()), None)
    if not buy_col or not sell_col:
        return pd.DataFrame()

    long_probs = long_df[[buy_col]].rename(columns={buy_col: "prob_Buy"})
    short_probs = short_df[[sell_col]].rename(columns={sell_col: "prob_Sell"})
    merged = long_probs.join(short_probs, how="outer").fillna(0.0)
    merged.index.name = "DateTime"
    return merged.sort_index()


@st.cache_data(show_spinner=False)
def compute_conflict_matrix(
    df: pd.DataFrame, long_thr: float, short_thr: float
) -> dict:
    """Return 4-cell conflict matrix with counts and percentages.

    Args:
        df: DataFrame with prob_Buy and prob_Sell columns.
        long_thr: Long entry threshold.
        short_thr: Short entry threshold.

    Returns:
        Dict with keys: neither, long_only, short_only, conflict,
        each containing 'count' and 'pct' values.
    """
    n = len(df)
    if n == 0:
        return {}
    long_active = df["prob_Buy"] >= long_thr
    short_active = df["prob_Sell"] >= short_thr

    neither = (~long_active & ~short_active).sum()
    long_only = (long_active & ~short_active).sum()
    short_only = (~long_active & short_active).sum()
    conflict = (long_active & short_active).sum()

    return {
        "neither":    {"count": int(neither),    "pct": neither / n * 100},
        "long_only":  {"count": int(long_only),  "pct": long_only / n * 100},
        "short_only": {"count": int(short_only), "pct": short_only / n * 100},
        "conflict":   {"count": int(conflict),   "pct": conflict / n * 100},
        "total_bars":  n,
    }


@st.cache_data(show_spinner=False)
def compute_autocorrelation(
    series: pd.Series, max_lag: int = 24
) -> pd.Series:
    """Binary ACF of signal_active at lags 1..max_lag."""
    import numpy as np
    binary = series.astype(float).values
    mean = binary.mean()
    var = binary.var()
    if var == 0:
        return pd.Series(0.0, index=range(1, max_lag + 1))
    n = len(binary)
    acf_vals = []
    for lag in range(1, max_lag + 1):
        if lag >= n:
            acf_vals.append(0.0)
        else:
            cov = ((binary[lag:] - mean) * (binary[:-lag] - mean)).mean()
            acf_vals.append(cov / var)
    return pd.Series(acf_vals, index=range(1, max_lag + 1))


@st.cache_data(show_spinner=False)
def compute_run_length_stats(series: pd.Series) -> dict:
    """Compute average/max consecutive run length for a binary signal series."""
    runs = []
    count = 0
    for v in series:
        if v:
            count += 1
        else:
            if count > 0:
                runs.append(count)
            count = 0
    if count > 0:
        runs.append(count)
    if not runs:
        return {"mean_run": 0.0, "max_run": 0, "total_runs": 0, "run_distribution": {}}

    import numpy as np
    dist = {}
    for r in runs:
        dist[r] = dist.get(r, 0) + 1

    return {
        "mean_run": float(np.mean(runs)),
        "max_run": int(max(runs)),
        "total_runs": len(runs),
        "run_distribution": dict(sorted(dist.items())),
    }


@st.cache_data(show_spinner=False)
def scan_prediction_files() -> list[str]:
    """Scan data/predictions/ for prediction CSVs."""
    preds_dir = PROJECT_ROOT / "data" / "predictions"
    if not preds_dir.is_dir():
        return []
    # Sort files by modification time, most recent first
    files = list(preds_dir.glob("oos_predictions*.csv"))
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return [
        str(f.relative_to(PROJECT_ROOT))
        for f in files
    ]

