"""
merge_predictions.py — Merge long and short model prediction CSVs.

Takes two CSV files (one from a Buy model, one from a Sell model),
aligns them by DateTime index (outer join), and produces a single
CSV with both prob_Buy and prob_Sell columns for ensemble backtesting.

Usage:
    python scripts/merge_predictions.py \
        --long-preds reports/vault_predictions_exp017.csv \
        --short-preds reports/vault_predictions_exp020.csv \
        --output reports/ensemble_predictions.csv
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("MergePredictions")


def load_predictions(path: str) -> pd.DataFrame:
    """Load a predictions CSV with DateTime index."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"Predictions file not found: {path}")
    df = pd.read_csv(path, index_col=0, parse_dates=True, on_bad_lines="warn")
    df.index.name = "DateTime"
    return df


def merge(
    long_df: pd.DataFrame,
    short_df: pd.DataFrame,
) -> pd.DataFrame:
    """Outer-join long and short predictions on DateTime index.

    Returns a DataFrame with prob_Buy and prob_Sell columns.
    NaN values (from misaligned date ranges) are filled with 0.0.
    """
    # Identify the probability column in each file
    long_col = _find_prob_col(long_df, "long")
    short_col = _find_prob_col(short_df, "short")

    # Extract just the probability columns
    long_probs = long_df[[long_col]].rename(columns={long_col: "prob_Buy"})
    short_probs = short_df[[short_col]].rename(columns={short_col: "prob_Sell"})

    # Outer join on DateTime index
    merged = long_probs.join(short_probs, how="outer")

    # Report NaN stats before filling
    nan_buy = merged["prob_Buy"].isna().sum()
    nan_sell = merged["prob_Sell"].isna().sum()
    if nan_buy > 0:
        log.warning(
            "prob_Buy: %d rows filled with 0.0 (missing from long predictions)",
            nan_buy,
        )
    if nan_sell > 0:
        log.warning(
            "prob_Sell: %d rows filled with 0.0 (missing from short predictions)",
            nan_sell,
        )

    # Fill NaN with 0.0
    merged = merged.fillna(0.0)

    return merged


def _find_prob_col(df: pd.DataFrame, label: str) -> str:
    """Find the probability column in a predictions DataFrame."""
    candidates = ["prob_Buy", "prob_Sell", "Predicted"]
    for col in candidates:
        if col in df.columns:
            return col
    raise ValueError(
        f"No probability column found in {label} predictions. "
        f"Expected one of: {candidates}. Got: {list(df.columns)}"
    )


def validate_and_report(
    long_df: pd.DataFrame,
    short_df: pd.DataFrame,
    merged_df: pd.DataFrame,
) -> None:
    """Print alignment statistics and warnings."""
    long_start, long_end = long_df.index.min(), long_df.index.max()
    short_start, short_end = short_df.index.min(), short_df.index.max()

    log.info("Long  predictions: %d rows, %s → %s", len(long_df), long_start, long_end)
    log.info("Short predictions: %d rows, %s → %s", len(short_df), short_start, short_end)
    log.info("Merged output:     %d rows", len(merged_df))

    # Check overlap
    overlap_start = max(long_start, short_start)
    overlap_end = min(long_end, short_end)

    if overlap_start > overlap_end:
        log.warning(
            "⚠ NO DATE OVERLAP between long and short predictions! "
            "Long ends at %s, Short starts at %s",
            long_end,
            short_start,
        )
    else:
        overlap_bars = merged_df.loc[overlap_start:overlap_end]
        log.info(
            "Overlap: %d bars from %s → %s",
            len(overlap_bars),
            overlap_start,
            overlap_end,
        )

    # Summary stats
    log.info(
        "prob_Buy  — mean=%.4f, std=%.4f, min=%.4f, max=%.4f",
        merged_df["prob_Buy"].mean(),
        merged_df["prob_Buy"].std(),
        merged_df["prob_Buy"].min(),
        merged_df["prob_Buy"].max(),
    )
    log.info(
        "prob_Sell — mean=%.4f, std=%.4f, min=%.4f, max=%.4f",
        merged_df["prob_Sell"].mean(),
        merged_df["prob_Sell"].std(),
        merged_df["prob_Sell"].min(),
        merged_df["prob_Sell"].max(),
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Merge long and short model predictions for ensemble backtesting."
    )
    parser.add_argument(
        "--long-preds",
        required=True,
        help="Path to long-model predictions CSV (prob_Buy column).",
    )
    parser.add_argument(
        "--short-preds",
        required=True,
        help="Path to short-model predictions CSV (prob_Sell column).",
    )
    parser.add_argument(
        "--output",
        default="reports/ensemble_predictions.csv",
        help="Output path for merged CSV (default: reports/ensemble_predictions.csv).",
    )
    args = parser.parse_args()

    # Load
    log.info("Loading long predictions from %s ...", args.long_preds)
    long_df = load_predictions(args.long_preds)

    log.info("Loading short predictions from %s ...", args.short_preds)
    short_df = load_predictions(args.short_preds)

    # Merge
    merged = merge(long_df, short_df)

    # Validate
    validate_and_report(long_df, short_df, merged)

    # Save
    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    merged.to_csv(args.output, index=True)
    log.info("Saved merged predictions to %s (%d rows)", args.output, len(merged))


if __name__ == "__main__":
    main()
