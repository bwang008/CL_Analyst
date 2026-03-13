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
# Temporal Breakdown Plotting
# ---------------------------------------------------------------------------

# CL futures trading hours (CME Globex) — map hour to readable session labels
_SESSION_LABELS = {
    18: "Open", 19: "", 20: "", 21: "", 22: "", 23: "",
    0: "", 1: "", 2: "", 3: "", 4: "", 5: "", 6: "", 7: "",
    8: "NY Open", 9: "", 10: "", 11: "", 12: "", 13: "", 14: "",
    15: "", 16: "Close", 17: "Settle",
}

DAY_ORDER = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def plot_temporal_breakdown(
    model_name: str,
    df: pd.DataFrame,
    prob_col: str,
    output_path: Path,
    threshold: float = DEFAULT_THRESHOLD,
) -> plt.Figure:
    """
    Generate a 2x2 grid showing temporal patterns of signal bars (prob >= threshold).

    Panels:
        1. Signal frequency by hour of day
        2. Signal frequency by day of week
        3. Signal count by calendar month (time series)
        4. Year × Month heatmap of signal density
    """
    direction = infer_direction(prob_col)

    # Parse datetime and derive signal mask
    df = df.copy()
    df["DateTime"] = pd.to_datetime(df["DateTime"])
    df["hour"] = df["DateTime"].dt.hour
    df["day_name"] = df["DateTime"].dt.day_name()
    df["month"] = df["DateTime"].dt.month
    df["year"] = df["DateTime"].dt.year
    df["year_month"] = df["DateTime"].dt.to_period("M")

    signals = df[df[prob_col] >= threshold]
    total_bars = len(df)
    n_signals = len(signals)

    if n_signals == 0:
        # Still generate a plot but with a "no signals" message
        fig, ax = plt.subplots(figsize=(10, 6))
        ax.text(0.5, 0.5, f"No signals above {threshold:.2f}\n(max prob = {df[prob_col].max():.4f})",
                ha="center", va="center", fontsize=16, color="#e74c3c",
                transform=ax.transAxes)
        ax.set_title(f"Temporal Breakdown — {model_name} ({direction})", fontweight="bold")
        ax.set_axis_off()
        fig.tight_layout()
        fig.savefig(output_path, dpi=150, bbox_inches="tight")
        plt.close(fig)
        return fig

    fig, axes = plt.subplots(2, 2, figsize=(14, 10))

    # --- Panel 1: Hourly breakdown ---
    ax1 = axes[0, 0]
    hourly_all = df.groupby("hour").size()
    hourly_sig = signals.groupby("hour").size()
    hours = range(24)
    all_counts = [hourly_all.get(h, 0) for h in hours]
    sig_counts = [hourly_sig.get(h, 0) for h in hours]
    # Signal rate per hour
    sig_rates = [sig_counts[h] / all_counts[h] * 100 if all_counts[h] > 0 else 0 for h in hours]

    bars = ax1.bar(hours, sig_counts, color="#3498db", alpha=0.75, edgecolor="white", linewidth=0.3)
    ax1.set_xlabel("Hour (UTC)", fontsize=10)
    ax1.set_ylabel("Signal Count", fontsize=10, color="#3498db")
    ax1.set_title("Signals by Hour of Day", fontsize=11, fontweight="bold")
    ax1.set_xticks(range(0, 24, 2))
    ax1.grid(True, alpha=0.2, axis="y")

    # Overlay signal rate on secondary axis
    ax1b = ax1.twinx()
    ax1b.plot(hours, sig_rates, color="#e74c3c", linewidth=1.5, marker=".", markersize=4, alpha=0.8)
    ax1b.set_ylabel("Signal Rate (%)", fontsize=10, color="#e74c3c")
    ax1b.tick_params(axis="y", labelcolor="#e74c3c")

    # Highlight peak hour
    peak_hour = max(hours, key=lambda h: sig_counts[h])
    ax1.annotate(
        f"Peak: {peak_hour}:00\n({sig_counts[peak_hour]:,})",
        xy=(peak_hour, sig_counts[peak_hour]),
        xytext=(peak_hour + 2, max(sig_counts) * 0.9),
        fontsize=8, ha="center",
        arrowprops=dict(arrowstyle="->", color="gray", lw=0.8),
        bbox=dict(boxstyle="round,pad=0.3", facecolor="lightyellow", alpha=0.9),
    )

    # --- Panel 2: Day of week ---
    ax2 = axes[0, 1]
    dow_all = df.groupby("day_name").size()
    dow_sig = signals.groupby("day_name").size()
    dow_counts = [dow_sig.get(d, 0) for d in DAY_ORDER]
    dow_totals = [dow_all.get(d, 0) for d in DAY_ORDER]
    dow_rates = [dow_counts[i] / dow_totals[i] * 100 if dow_totals[i] > 0 else 0 for i in range(len(DAY_ORDER))]

    colors = ["#2ecc71" if c > np.mean(dow_counts) else "#3498db" for c in dow_counts]
    ax2.bar(range(len(DAY_ORDER)), dow_counts, color=colors, alpha=0.75, edgecolor="white", linewidth=0.3)
    ax2.set_xticks(range(len(DAY_ORDER)))
    ax2.set_xticklabels([d[:3] for d in DAY_ORDER], fontsize=9)
    ax2.set_ylabel("Signal Count", fontsize=10)
    ax2.set_title("Signals by Day of Week", fontsize=11, fontweight="bold")
    ax2.grid(True, alpha=0.2, axis="y")

    # Add rate labels on top of bars
    for i, (c, r) in enumerate(zip(dow_counts, dow_rates)):
        ax2.text(i, c + max(dow_counts) * 0.02, f"{r:.1f}%", ha="center", fontsize=8, color="#555")

    # --- Panel 3: Monthly signal count (time series) ---
    ax3 = axes[1, 0]
    monthly_sig = signals.groupby("year_month").size()
    monthly_all = df.groupby("year_month").size()

    x_labels = [str(p) for p in monthly_sig.index]
    ax3.bar(range(len(x_labels)), monthly_sig.values, color="#9b59b6", alpha=0.7, edgecolor="white", linewidth=0.3)
    ax3.set_xlabel("Month", fontsize=10)
    ax3.set_ylabel("Signal Count", fontsize=10)
    ax3.set_title("Signal Count by Month", fontsize=11, fontweight="bold")

    # Show only every Nth label to avoid crowding
    n_months = len(x_labels)
    step = max(1, n_months // 12)
    ax3.set_xticks(range(0, n_months, step))
    ax3.set_xticklabels([x_labels[i] for i in range(0, n_months, step)], rotation=45, ha="right", fontsize=7)
    ax3.grid(True, alpha=0.2, axis="y")

    # Mean line
    mean_monthly = monthly_sig.mean()
    ax3.axhline(mean_monthly, color="#e74c3c", linestyle="--", linewidth=1, alpha=0.7,
                label=f"Mean: {mean_monthly:.0f}/mo")
    ax3.legend(fontsize=8, loc="upper right")

    # --- Panel 4: Year × Month heatmap ---
    ax4 = axes[1, 1]
    # Build pivot table: signal rate (signals / total bars) by year × month
    df["_sig"] = (df[prob_col] >= threshold).astype(int)
    pivot = df.pivot_table(values="_sig", index="year", columns="month", aggfunc="mean") * 100

    years = sorted(pivot.index)
    months = range(1, 13)
    heatmap_data = np.full((len(years), 12), np.nan)
    for i, y in enumerate(years):
        for m in months:
            if m in pivot.columns and y in pivot.index:
                heatmap_data[i, m - 1] = pivot.loc[y, m]

    im = ax4.imshow(heatmap_data, cmap="YlOrRd", aspect="auto", interpolation="nearest")
    ax4.set_xticks(range(12))
    ax4.set_xticklabels(MONTH_NAMES, fontsize=8)
    ax4.set_yticks(range(len(years)))
    ax4.set_yticklabels(years, fontsize=8)
    ax4.set_title("Signal Rate (%) — Year × Month", fontsize=11, fontweight="bold")

    # Annotate cells
    for i in range(len(years)):
        for j in range(12):
            val = heatmap_data[i, j]
            if not np.isnan(val):
                color = "white" if val > np.nanmax(heatmap_data) * 0.6 else "black"
                ax4.text(j, i, f"{val:.1f}", ha="center", va="center", fontsize=7, color=color)

    fig.colorbar(im, ax=ax4, fraction=0.046, pad=0.04, label="Signal Rate %")

    # Suptitle
    fig.suptitle(
        f"Temporal Signal Breakdown — {model_name} ({direction})\n"
        f"Threshold ≥ {threshold:.2f}  |  {n_signals:,} signals / {total_bars:,} bars ({n_signals/total_bars*100:.1f}%)",
        fontsize=13, fontweight="bold",
    )
    fig.tight_layout(rect=[0, 0, 1, 0.93])
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
        dist_path = OUTPUT_DIR / f"{model_name}.png"
        temporal_path = OUTPUT_DIR / f"{model_name}_temporal.png"

        # Load CSV (needed for both plots and grid)
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

        model_data_for_grid.append((model_name, probs, prob_col))

        # --- Distribution plot ---
        if dist_path.exists() and not args.force:
            print(f"  SKIP  {model_name}  (distribution PNG exists)")
        else:
            print(f"  GEN   {model_name}  ({len(probs):,} predictions, col={prob_col})")
            plot_single_model(model_name, probs, prob_col, dist_path, threshold=args.threshold)
            generated += 1

        # --- Temporal breakdown plot ---
        if temporal_path.exists() and not args.force:
            print(f"  SKIP  {model_name}_temporal  (temporal PNG exists)")
        else:
            print(f"  GEN   {model_name}_temporal")
            plot_temporal_breakdown(model_name, df, prob_col, temporal_path, threshold=args.threshold)
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

