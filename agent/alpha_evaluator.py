"""
Alpha Evaluator (Workflow C Foundation)

Evaluates the raw predictive quality of an ensemble (long model + short model)
without any trade execution parameters, across a full term structure of
forward-return horizons.

Key metrics:
- Signal-to-Noise Ratio (SNR): dimensionless mean/std of frictionless PnL.
  NO annualization — overlapping returns on hourly bars create autocorrelation
  that makes annualized Sharpe explode into artifacts.
- Information Coefficient (IC): Spearman rank correlation between a continuous
  signal (prob_Buy - prob_Sell) and the realised forward return.
"""

import os
import logging

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from agent.backtest_engine import load_ohlcv, load_predictions, _resolve_prob_column
from agent.forward_returns import compute_forward_returns

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_models_from_dir(directory: str, prefix: str = "") -> list[str]:
    """Walk *directory* and return paths to model dirs containing
    ``oos_predictions.csv``.

    Reuses the pattern from ``agent/sweep_ensembles.py``.
    """
    models: list[str] = []
    if os.path.exists(directory):
        for root, _dirs, files in os.walk(directory):
            if "oos_predictions.csv" in files:
                basename = os.path.basename(root)
                if prefix == "" or basename.startswith(prefix) or prefix in root:
                    models.append(root.replace("\\", "/"))
    return sorted(models)


def _unique_model_name(model_path: str) -> str:
    """Build a unique model name from a model directory path.

    Combines the experiment directory with the model basename.
    """
    parts = model_path.replace("\\", "/").split("/")
    basename = parts[-1]
    skip_dirs = {"registry", "canary_output", "reports", "batch_runs", ".", ""}
    experiment_dir = ""
    for part in reversed(parts[:-1]):
        if part.lower() not in skip_dirs:
            experiment_dir = part
            break
    if experiment_dir:
        return f"{experiment_dir}_{basename}"
    return basename


# ---------------------------------------------------------------------------
# Core evaluation
# ---------------------------------------------------------------------------

def evaluate_ensemble(
    long_probs: pd.Series,
    short_probs: pd.Series,
    forward_returns: pd.DataFrame,
    threshold: float = 0.5,
    holdout_start: pd.Timestamp = None,
) -> dict:
    """Compute frictionless metrics for a single long+short ensemble across
    all horizons present in *forward_returns*.

    Signal Construction — two modes for different metrics:

    1. **Binary signal** (for SNR and Frictionless PnL)::

           signal[t] = +1  if prob_Buy[t]  > threshold
                     = -1  if prob_Sell[t] > threshold
                     =  0  otherwise

    2. **Continuous signal** (for IC only)::

           signal[t] = prob_Buy[t] - prob_Sell[t]

    Per-horizon metrics
    ~~~~~~~~~~~~~~~~~~~
    - **Frictionless PnL**: ``binary_signal * fwd_ret_H``
    - **SNR**: ``mean(frictionless_pnl) / std(frictionless_pnl)``
      — dimensionless, NO annualization.
    - **IC**: ``spearmanr(continuous_signal, fwd_ret_H)`` on non-NaN rows.

    Aggregate metrics
    ~~~~~~~~~~~~~~~~~
    - ``peak_snr``: max SNR across all horizons.
    - ``peak_horizon``: horizon at which peak_snr occurs.
    - ``signal_count``: count of bars where ``|binary_signal| > 0``.
    - ``hit_rate``: % of non-zero binary signals where
      ``sign(signal) == sign(fwd_ret at peak_horizon)``.

    Holdout split
    ~~~~~~~~~~~~~
    If *holdout_start* is provided, metrics are computed separately for the
    evaluation period (< holdout_start) and holdout period (>= holdout_start).

    Monthly breakdown
    ~~~~~~~~~~~~~~~~~
    For the peak horizon, resample frictionless PnL to monthly sums and
    return as ``{year_month_str: monthly_pnl}``.
    """
    # Align all Series/DataFrames to a common index
    idx = long_probs.index.intersection(short_probs.index).intersection(
        forward_returns.index
    )
    long_probs = long_probs.reindex(idx).fillna(0.0)
    short_probs = short_probs.reindex(idx).fillna(0.0)
    fwd = forward_returns.reindex(idx)

    # --- Build signals -------------------------------------------------------
    binary_signal = np.where(
        long_probs.values > threshold,
        1,
        np.where(short_probs.values > threshold, -1, 0),
    )
    continuous_signal = long_probs.values - short_probs.values

    # Detect horizons from column names
    horizons = sorted(
        int(c.replace("fwd_ret_", "")) for c in fwd.columns if c.startswith("fwd_ret_")
    )

    result: dict = {}
    best_snr = -np.inf
    best_horizon = horizons[0] if horizons else 0

    def _compute_metrics(bin_sig, cont_sig, fwd_df, prefix=""):
        """Compute per-horizon metrics and aggregate stats.

        Returns a dict with SNR/IC per horizon, peak_snr, peak_horizon,
        signal_count, hit_rate, and monthly breakdown.
        """
        nonlocal best_snr, best_horizon

        metrics: dict = {}
        local_best_snr = -np.inf
        local_best_horizon = horizons[0] if horizons else 0

        for h in horizons:
            col = f"fwd_ret_{h}"
            ret_h = fwd_df[col].values

            # Mask: valid where both signal and return are finite
            valid = np.isfinite(ret_h)
            bs_valid = bin_sig[valid]
            cs_valid = cont_sig[valid]
            ret_valid = ret_h[valid]

            # Frictionless PnL
            fpnl = bs_valid * ret_valid

            # SNR — dimensionless, no annualization
            std_fpnl = np.std(fpnl)
            snr = np.mean(fpnl) / std_fpnl if std_fpnl > 0 else 0.0

            # IC — Spearman rank correlation (continuous signal)
            # Need at least 3 observations for a meaningful correlation
            if len(cs_valid) >= 3:
                ic_val, _ = spearmanr(cs_valid, ret_valid)
                if np.isnan(ic_val):
                    ic_val = 0.0
            else:
                ic_val = 0.0

            metrics[f"{prefix}snr_{h}"] = snr
            metrics[f"{prefix}ic_{h}"] = ic_val

            if snr > local_best_snr:
                local_best_snr = snr
                local_best_horizon = h

        # Aggregate metrics
        signal_mask = np.abs(bin_sig) > 0
        metrics[f"{prefix}peak_snr"] = local_best_snr
        metrics[f"{prefix}peak_horizon"] = local_best_horizon
        metrics[f"{prefix}signal_count"] = int(np.sum(signal_mask))

        # Hit rate at peak horizon
        peak_col = f"fwd_ret_{local_best_horizon}"
        peak_ret = fwd_df[peak_col].values
        valid_peak = np.isfinite(peak_ret) & signal_mask
        if np.sum(valid_peak) > 0:
            hits = np.sign(bin_sig[valid_peak]) == np.sign(peak_ret[valid_peak])
            metrics[f"{prefix}hit_rate"] = float(np.mean(hits))
        else:
            metrics[f"{prefix}hit_rate"] = 0.0

        # Monthly breakdown at peak horizon
        if not prefix:  # only for the full / eval period
            fpnl_series = pd.Series(
                bin_sig * fwd_df[peak_col].values,
                index=fwd_df.index,
            )
            try:
                monthly = fpnl_series.resample("ME").sum().dropna()
            except ValueError:
                monthly = fpnl_series.resample("M").sum().dropna()
            metrics["monthly_breakdown"] = {
                dt.strftime("%Y-%m"): round(float(v), 6)
                for dt, v in monthly.items()
            }

        return metrics, local_best_snr, local_best_horizon

    # --- Full-period or eval/holdout split -----------------------------------
    if holdout_start is not None:
        eval_mask = idx < holdout_start
        holdout_mask = idx >= holdout_start

        # Evaluation period
        eval_metrics, _, _ = _compute_metrics(
            binary_signal[eval_mask],
            continuous_signal[eval_mask],
            fwd.loc[eval_mask],
            prefix="",
        )
        result.update(eval_metrics)

        # Holdout period
        holdout_metrics, _, _ = _compute_metrics(
            binary_signal[holdout_mask],
            continuous_signal[holdout_mask],
            fwd.loc[holdout_mask],
            prefix="holdout_",
        )
        result.update(holdout_metrics)
    else:
        full_metrics, _, _ = _compute_metrics(
            binary_signal, continuous_signal, fwd, prefix=""
        )
        result.update(full_metrics)

    return result


# ---------------------------------------------------------------------------
# Batch evaluation
# ---------------------------------------------------------------------------

def batch_evaluate_ensembles(
    long_models: list[str],
    short_models: list[str],
    ohlcv_path: str,
    horizons: list[int] = [6, 12, 24, 48, 72],
    threshold: float = 0.5,
    holdout_months: int = 6,
    min_signals: int = 360,
) -> pd.DataFrame:
    """Cartesian-product evaluation of all long × short pairs.

    1. Load OHLCV once, compute forward returns for all horizons once.
    2. Load all prediction CSVs once into memory.
    3. Evaluate all N×M pairs via vectorized operations.
    4. Filter: drop ensembles with signal_count < min_signals.
    5. Sort by peak_snr descending.

    Parameters
    ----------
    long_models : list[str]
        Paths to long model directories (must contain ``oos_predictions.csv``).
    short_models : list[str]
        Paths to short model directories.
    ohlcv_path : str
        Path to OHLCV parquet file.
    horizons : list[int]
        Forward-return horizons in bars.
    threshold : float
        Binary signal probability threshold.
    holdout_months : int
        Number of months at the tail of the dataset to reserve as holdout.
    min_signals : int
        Minimum number of non-zero binary signals to keep an ensemble.

    Returns
    -------
    pd.DataFrame
        Sorted by ``peak_snr`` descending with per-horizon and aggregate
        metrics for every (long, short) pair.
    """
    # 1. Load OHLCV and compute forward returns
    logger.info("Loading OHLCV from %s", ohlcv_path)
    ohlcv = load_ohlcv(ohlcv_path)
    fwd_ret = compute_forward_returns(ohlcv, horizons=horizons)

    # 2. Load all prediction CSVs into memory
    long_data: dict[str, pd.Series] = {}
    for model_dir in long_models:
        csv_path = os.path.join(model_dir, "oos_predictions.csv")
        preds = load_predictions(csv_path)
        col = _resolve_prob_column(preds, "buy")
        if col is None:
            logger.warning("No prob_Buy column found in %s — skipping", csv_path)
            continue
        long_data[model_dir] = preds[col]

    short_data: dict[str, pd.Series] = {}
    for model_dir in short_models:
        csv_path = os.path.join(model_dir, "oos_predictions.csv")
        preds = load_predictions(csv_path)
        col = _resolve_prob_column(preds, "sell")
        if col is None:
            logger.warning("No prob_Sell column found in %s — skipping", csv_path)
            continue
        short_data[model_dir] = preds[col]

    # 3. Compute holdout start dynamically
    holdout_start = ohlcv.index.max() - pd.DateOffset(months=holdout_months)

    # 4. Cartesian product evaluation
    rows: list[dict] = []
    counter = 1
    for lpath, lprobs in long_data.items():
        lname = _unique_model_name(lpath)
        for spath, sprobs in short_data.items():
            sname = _unique_model_name(spath)
            ensemble_id = f"ensemble_{counter:03d}"
            counter += 1

            try:
                metrics = evaluate_ensemble(
                    long_probs=lprobs,
                    short_probs=sprobs,
                    forward_returns=fwd_ret,
                    threshold=threshold,
                    holdout_start=holdout_start,
                )
            except Exception:
                logger.exception(
                    "Failed to evaluate %s × %s", lname, sname
                )
                continue

            row = {
                "ensemble_id": ensemble_id,
                "long_model": lname,
                "short_model": sname,
                "long_path": lpath,
                "short_path": spath,
            }
            row.update(metrics)
            rows.append(row)

    if not rows:
        logger.warning("No valid ensembles found.")
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    # 5. Filter by minimum signals
    if "signal_count" in df.columns:
        df = df[df["signal_count"] >= min_signals]

    # 6. Sort by peak_snr descending
    if "peak_snr" in df.columns:
        df = df.sort_values("peak_snr", ascending=False).reset_index(drop=True)

    return df
