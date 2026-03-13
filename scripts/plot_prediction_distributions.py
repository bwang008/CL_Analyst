"""
Prediction Probability Distribution Visualizer.

Auto-discovers OOS prediction CSVs in models/registry/*/oos_predictions.csv,
generates histogram + KDE plots showing probability distributions with
threshold lines and distribution statistics.

Usage:
    python scripts/plot_prediction_distributions.py          # default run
    python scripts/plot_prediction_distributions.py --force  # regenerate all
    python scripts/plot_prediction_distributions.py --threshold 0.55

Author: CL Analyst
"""

from __future__ import annotations

import argparse
import os
import sys
from glob import glob
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import stats
from scipy.signal import find_peaks

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_DIR = PROJECT_ROOT / "models" / "registry"
OUTPUT_DIR = PROJECT_ROOT / "reports" / "prediction_distributions"

DEFAULT_THRESHOLD = 0.60
SECONDARY_THRESHOLD = 0.45


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def find_prob_column(columns: list[str]) -> str | None:
    """Return the first column whose name contains 'buy' or 'sell' (case-insensitive)."""
    for col in columns:
        if "buy" in col.lower() or "sell" in col.lower():
            return col
    return None


def infer_direction(col_name: str) -> str:
    """Infer LONG or SHORT from the probability column name."""
    lower = col_name.lower()
    if "buy" in lower:
        return "LONG"
    elif "sell" in lower:
        return "SHORT"
    return "UNKNOWN"


def classify_distribution(values: np.ndarray) -> str:
    """
    Classify the distribution shape as unimodal, bimodal, or skewed.

    Uses KDE peak detection + skewness.
    """
    if len(values) < 10:
        return "insufficient data"

    # Build KDE
    try:
        kde = stats.gaussian_kde(values)
    except np.linalg.LinAlgError:
        return "degenerate"

    x_grid = np.linspace(values.min(), values.max(), 500)
    density = kde(x_grid)

    # Find peaks (require prominence > 5% of max density)
    peaks, properties = find_peaks(density, prominence=density.max() * 0.05)
    n_peaks = len(peaks)

    skewness = stats.skew(values)

    if n_peaks >= 2:
        return "bimodal"
    elif abs(skewness) > 0.5:
        direction = "right" if skewness > 0 else "left"
        return f"skewed-{direction} (skew={skewness:.2f})"
    else:
        return "unimodal"


# ---------------------------------------------------------------------------
# Plotting
# ---------------------------------------------------------------------------

def plot_single_model(
    model_name: str,
    probs: np.ndarray,
    prob_col: str,
    output_path: Path,
    threshold: float = DEFAULT_THRESHOLD,
) -> plt.Figure:
    """Generate a histogram + KDE plot for a single model's predictions."""
    direction = infer_direction(prob_col)

    fig, ax = plt.subplots(figsize=(10, 6))

    # Color-coded histogram: green above threshold, red below
    above = probs[probs >= threshold]
    below = probs[probs < threshold]

    bins = np.linspace(probs.min(), probs.max(), 51)

    ax.hist(below, bins=bins, color="#e74c3c", alpha=0.65, label=f"< {threshold:.2f}", edgecolor="white", linewidth=0.3)
    ax.hist(above, bins=bins, color="#2ecc71", alpha=0.65, label=f"≥ {threshold:.2f}", edgecolor="white", linewidth=0.3)

    # KDE overlay
    try:
        kde = stats.gaussian_kde(probs)
        x_grid = np.linspace(probs.min(), probs.max(), 300)
        kde_vals = kde(x_grid)
        # Scale KDE to match histogram height
        bin_width = (probs.max() - probs.min()) / 50
        ax.plot(x_grid, kde_vals * len(probs) * bin_width, color="#2c3e50", linewidth=2, label="KDE")
    except Exception:
        pass  # KDE can fail on degenerate data

    # Threshold lines
    ax.axvline(threshold, color="black", linestyle="--", linewidth=1.5, label=f"Threshold {threshold:.2f}")
    ax.axvline(SECONDARY_THRESHOLD, color="gray", linestyle=":", linewidth=1.2, label=f"Secondary {SECONDARY_THRESHOLD:.2f}")

    # Stats
    pct_above = (probs >= threshold).sum() / len(probs) * 100
    pct_below_sec = (probs <= SECONDARY_THRESHOLD).sum() / len(probs) * 100
    shape = classify_distribution(probs)

    stats_text = (
        f"Model: {model_name}\n"
        f"Direction: {direction}  |  Column: {prob_col}\n"
        f"N = {len(probs):,}\n"
        f"Min: {probs.min():.4f}  Max: {probs.max():.4f}\n"
        f"Mean: {probs.mean():.4f}  Median: {np.median(probs):.4f}\n"
        f"≥ {threshold:.2f}: {pct_above:.1f}%  |  ≤ {SECONDARY_THRESHOLD:.2f}: {pct_below_sec:.1f}%\n"
        f"Shape: {shape}"
    )

    ax.text(
        0.98, 0.97, stats_text,
        transform=ax.transAxes, fontsize=9, verticalalignment="top",
        horizontalalignment="right",
        bbox=dict(boxstyle="round,pad=0.5", facecolor="white", alpha=0.85, edgecolor="gray"),
        family="monospace",
    )

    ax.set_title(f"Prediction Distribution — {model_name}", fontsize=13, fontweight="bold")
    ax.set_xlabel("Predicted Probability", fontsize=11)
    ax.set_ylabel("Frequency", fontsize=11)
    ax.legend(loc="upper left", fontsize=9)
    ax.grid(True, alpha=0.25)

    fig.tight_layout()
    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return fig


def plot_comparison_grid(
    model_data: list[tuple[str, np.ndarray, str]],
    output_path: Path,
    threshold: float = DEFAULT_THRESHOLD,
) -> plt.Figure:
    """Generate a combined grid comparing all models side-by-side."""
    n = len(model_data)
    if n == 0:
        return None

    ncols = min(3, n)
    nrows = (n + ncols - 1) // ncols

    fig, axes = plt.subplots(nrows, ncols, figsize=(7 * ncols, 5 * nrows))
    if n == 1:
        axes = np.array([axes])
    axes = np.atleast_2d(axes)

    for idx, (model_name, probs, prob_col) in enumerate(model_data):
        row, col = divmod(idx, ncols)
        ax = axes[row, col]
        direction = infer_direction(prob_col)

        # Color-coded histogram
        above = probs[probs >= threshold]
        below = probs[probs < threshold]
        bins = np.linspace(probs.min(), probs.max(), 41)

        ax.hist(below, bins=bins, color="#e74c3c", alpha=0.65, edgecolor="white", linewidth=0.3)
        ax.hist(above, bins=bins, color="#2ecc71", alpha=0.65, edgecolor="white", linewidth=0.3)

        try:
            kde = stats.gaussian_kde(probs)
            x_grid = np.linspace(probs.min(), probs.max(), 200)
            kde_vals = kde(x_grid)
            bin_width = (probs.max() - probs.min()) / 40
            ax.plot(x_grid, kde_vals * len(probs) * bin_width, color="#2c3e50", linewidth=1.5)
        except Exception:
            pass

        ax.axvline(threshold, color="black", linestyle="--", linewidth=1.2)
        ax.axvline(SECONDARY_THRESHOLD, color="gray", linestyle=":", linewidth=1.0)

        pct_above = (probs >= threshold).sum() / len(probs) * 100
        shape = classify_distribution(probs)

        # Compact annotation
        short_name = model_name.replace("_", "\n", 1) if len(model_name) > 30 else model_name
        ax.set_title(f"{model_name}\n({direction})", fontsize=9, fontweight="bold")
        ax.text(
            0.97, 0.95,
            f"N={len(probs):,}\n≥{threshold}: {pct_above:.1f}%\nmax={probs.max():.3f}\n{shape}",
            transform=ax.transAxes, fontsize=8, va="top", ha="right",
            bbox=dict(boxstyle="round", facecolor="white", alpha=0.8, edgecolor="gray"),
            family="monospace",
        )
        ax.set_xlabel("Prob", fontsize=8)
        ax.set_ylabel("Freq", fontsize=8)
        ax.tick_params(labelsize=7)
        ax.grid(True, alpha=0.2)

    # Hide unused axes
    for idx in range(n, nrows * ncols):
        row, col = divmod(idx, ncols)
        axes[row, col].set_visible(False)

    fig.suptitle("Prediction Distributions — All Models", fontsize=15, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return fig


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def discover_models(registry_dir: Path) -> list[tuple[str, Path]]:
    """Return list of (model_name, csv_path) for all models with an OOS CSV."""
    results = []
    if not registry_dir.exists():
        return results

    for entry in sorted(registry_dir.iterdir()):
        if not entry.is_dir():
            continue
        csv_path = entry / "oos_predictions.csv"
        if csv_path.exists():
            results.append((entry.name, csv_path))
    return results


def main():
    parser = argparse.ArgumentParser(
        description="Plot prediction probability distributions for OOS predictions."
    )
    parser.add_argument("--force", action="store_true", help="Regenerate all plots even if they exist")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD, help="Primary threshold (default: 0.60)")
    args = parser.parse_args()

    print(f"Registry: {REGISTRY_DIR}")
    print(f"Output:   {OUTPUT_DIR}")
    print(f"Threshold: {args.threshold}")
    print(f"Force:    {args.force}")
    print()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    models = discover_models(REGISTRY_DIR)
    if not models:
        print("No models with oos_predictions.csv found in registry.")
        sys.exit(1)

    print(f"Found {len(models)} model(s) with OOS predictions:\n")

    model_data_for_grid: list[tuple[str, np.ndarray, str]] = []
    generated = 0
    skipped = 0

    for model_name, csv_path in models:
        output_path = OUTPUT_DIR / f"{model_name}.png"

        # Skip if exists and not forcing
        if output_path.exists() and not args.force:
            print(f"  SKIP  {model_name}  (PNG exists, use --force to regenerate)")
            # Still load for comparison grid
            df = pd.read_csv(csv_path)
            prob_col = find_prob_column(df.columns.tolist())
            if prob_col:
                model_data_for_grid.append((model_name, df[prob_col].dropna().values, prob_col))
            skipped += 1
            continue

        # Load CSV
        df = pd.read_csv(csv_path)
        prob_col = find_prob_column(df.columns.tolist())

        if prob_col is None:
            print(f"  SKIP  {model_name}  (no prob column found in: {list(df.columns)})")
            skipped += 1
            continue

        probs = df[prob_col].dropna().values

        if len(probs) == 0:
            print(f"  SKIP  {model_name}  (empty probability column)")
            skipped += 1
            continue

        print(f"  GEN   {model_name}  ({len(probs):,} predictions, col={prob_col})")
        plot_single_model(model_name, probs, prob_col, output_path, threshold=args.threshold)
        model_data_for_grid.append((model_name, probs, prob_col))
        generated += 1

    # Combined comparison grid
    grid_path = OUTPUT_DIR / "all_models_comparison.png"
    if model_data_for_grid:
        if grid_path.exists() and not args.force and generated == 0:
            print(f"\n  SKIP  all_models_comparison.png  (exists, use --force)")
        else:
            print(f"\n  GEN   all_models_comparison.png  ({len(model_data_for_grid)} models)")
            plot_comparison_grid(model_data_for_grid, grid_path, threshold=args.threshold)

    print(f"\nDone. Generated: {generated}, Skipped: {skipped}")
    print(f"Output directory: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
