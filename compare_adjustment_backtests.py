"""
compare_adjustment_backtests.py — Compare backtests across data adjustment methods.

Converts the raw Databento CSV into three formats (raw, ratio, panama),
loads the same prediction signals, runs the backtest engine on each
OHLCV variant, and produces a comparison report.

This script does NOT modify any existing data files — it creates
temporary copies and reports the results.

Usage:
    python compare_adjustment_backtests.py \
        --config configs/strategies/hourly_ensemble_010.json \
        --databento-csv "C:/CL_Analyst_Data/data/raw/DataBentoSample/glbx-mdp3-20100606-20260613.ohlcv-1h.csv" \
        --predictions "reports/sweep_hs11_3x1_12h_20260614_0348/registry/canary_output/oos_predictions_sweep_hs11_3x1_12h_20260614_0348_long_logloss.csv"

    # Or use defaults (auto-discovers most recent sweep predictions):
    python compare_adjustment_backtests.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

from src.data.databento_data_builder import convert_databento_csv
from agent.backtest_engine import (
    BacktestEngine,
    BacktestResult,
    load_predictions,
    compare_runs,
    format_report,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DEFAULT_DATABENTO_CSV = (
    r"C:\CL_Analyst_Data\data\raw\DataBentoSample"
    r"\glbx-mdp3-20100606-20260613.ohlcv-1h.csv"
)
DEFAULT_CONFIG = os.path.join(
    PROJECT_ROOT, "configs", "strategies", "hourly_ensemble_010.json"
)


def _find_latest_predictions() -> str | None:
    """Auto-discover the most recent sweep prediction CSV."""
    reports_dir = os.path.join(PROJECT_ROOT, "reports")
    sweep_dirs = sorted(
        [d for d in os.listdir(reports_dir) if d.startswith("sweep_")],
        reverse=True,
    )
    for sweep in sweep_dirs:
        canary = os.path.join(reports_dir, sweep, "registry", "canary_output")
        if os.path.isdir(canary):
            csvs = [f for f in os.listdir(canary) if f.startswith("oos_predictions_") and f.endswith(".csv")]
            if csvs:
                # prefer long_logloss if available
                for c in csvs:
                    if "long_logloss" in c:
                        return os.path.join(canary, c)
                return os.path.join(canary, csvs[0])
    return None


def _load_ohlcv_from_semicolon_csv(path: str) -> pd.DataFrame:
    """Load a semicolon-separated, headerless OHLCV CSV into a DataFrame.

    This mirrors the exact parsing logic in DataProcessor.load_data()
    so the backtest engine receives identical column names and index.
    """
    df = pd.read_csv(
        path,
        sep=";",
        header=None,
        names=["Date", "Time", "Open", "High", "Low", "Close", "Volume"],
    )
    df["DateTime"] = pd.to_datetime(
        df["Date"] + " " + df["Time"], dayfirst=True
    )
    df = df.set_index("DateTime").drop(columns=["Date", "Time"])
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _compute_sharpe(result: BacktestResult) -> float:
    """Annualized Monthly Sharpe from trade PnL."""
    if not result.trades:
        return 0.0
    records = [{"exit_dt": t.exit_dt, "pnl": t.net_pnl_dollars} for t in result.trades]
    tdf = pd.DataFrame(records)
    tdf["exit_dt"] = pd.to_datetime(tdf["exit_dt"])
    tdf = tdf.set_index("exit_dt").sort_index()
    monthly = tdf["pnl"].resample("M").sum().dropna().values
    if len(monthly) < 2:
        return 0.0
    std = float(np.std(monthly))
    if std < 1e-9:
        return 0.0
    return float((np.mean(monthly) / std) * np.sqrt(12))


def _compute_sortino(result: BacktestResult) -> float:
    """Annualized Monthly Sortino from trade PnL."""
    if not result.trades:
        return 0.0
    records = [{"exit_dt": t.exit_dt, "pnl": t.net_pnl_dollars} for t in result.trades]
    tdf = pd.DataFrame(records)
    tdf["exit_dt"] = pd.to_datetime(tdf["exit_dt"])
    tdf = tdf.set_index("exit_dt").sort_index()
    monthly = tdf["pnl"].resample("M").sum().dropna().values
    if len(monthly) < 2:
        return 0.0
    neg = monthly[monthly < 0]
    if len(neg) == 0 or float(np.std(neg)) < 1e-9:
        return 10.0
    return float((np.mean(monthly) / np.std(neg)) * np.sqrt(12))


def _compute_avg_trade(result: BacktestResult) -> float:
    """Average net PnL per trade."""
    if not result.trades:
        return 0.0
    return result.total_pnl / result.trade_count


def _yearly_pnl_breakdown(result: BacktestResult) -> dict[int, float]:
    """Sum PnL by year."""
    yearly: dict[int, float] = {}
    for t in result.trades:
        yr = t.exit_dt.year
        yearly[yr] = yearly.get(yr, 0.0) + t.net_pnl_dollars
    return dict(sorted(yearly.items()))


# ---------------------------------------------------------------------------
# Main Comparison
# ---------------------------------------------------------------------------

def run_comparison(
    databento_csv: str,
    config_path: str,
    predictions_path: str,
    output_report: str | None = None,
) -> str:
    """Run backtests on raw-unadjusted and panama-adjusted data and compare.

    Returns the comparison report as a string.
    """
    print("=" * 80)
    print("  DATA ADJUSTMENT BACKTEST COMPARISON")
    print(f"  Started: {datetime.now().isoformat(timespec='seconds')}")
    print("=" * 80)

    # ── Step 1: Convert raw Databento CSV to both formats ─────────────
    with tempfile.TemporaryDirectory(prefix="cl_adj_compare_") as tmpdir:
        print(f"\n[1/5] Converting Databento CSV to adjusted formats...")
        outputs = convert_databento_csv(
            databento_csv,
            tmpdir,
            modes=["raw", "panama", "ratio"],
            fmt="semicolon",
        )

        # ── Step 2: Load all three OHLCV variants ────────────────────
        print(f"\n[2/5] Loading OHLCV data...")
        ohlcv_variants: dict[str, pd.DataFrame] = {}
        for mode, path in outputs.items():
            ohlcv_variants[mode] = _load_ohlcv_from_semicolon_csv(path)
            print(f"  {mode:>8s}: {len(ohlcv_variants[mode]):,} bars  "
                  f"({ohlcv_variants[mode].index.min().date()} -> "
                  f"{ohlcv_variants[mode].index.max().date()})")

        # Spot-check prices
        for mode, df in ohlcv_variants.items():
            first_close = df["Close"].iloc[0]
            last_close = df["Close"].iloc[-1]
            print(f"  {mode:>8s}  first Close=${first_close:.4f}  "
                  f"last Close=${last_close:.4f}")

        # ── Step 3: Load predictions & config ─────────────────────────
        print(f"\n[3/5] Loading predictions and strategy config...")
        predictions_df = load_predictions(predictions_path)
        print(f"  Predictions: {len(predictions_df):,} rows "
              f"({predictions_df.index.min().date()} -> "
              f"{predictions_df.index.max().date()})")

        with open(config_path, "r") as f:
            cfg = json.load(f)
        print(f"  Strategy: {cfg.get('nickname', 'unknown')}")

        # ── Step 4: Run backtests on each variant ─────────────────────
        print(f"\n[4/5] Running backtests...")
        results: dict[str, BacktestResult] = {}

        # Only compare raw vs panama (ratio is what we're moving away from)
        test_modes = ["raw", "panama", "ratio"]

        for mode in test_modes:
            ohlcv = ohlcv_variants[mode]
            engine = BacktestEngine.from_config(cfg)
            label = {
                "raw": "Raw Unadjusted",
                "panama": "Panama Canal (Additive)",
                "ratio": "Ratio Adjusted (Current)",
            }[mode]

            result = engine.run(predictions_df, ohlcv, label=label)
            results[mode] = result
            print(f"  {label}: {result.trade_count} trades, "
                  f"PnL=${result.total_pnl:,.2f}, WR={result.win_rate:.1%}")

        # ── Step 5: Build comparison report ───────────────────────────
        print(f"\n[5/5] Building comparison report...")
        report = _build_report(
            results,
            cfg,
            predictions_path,
            databento_csv,
        )

    # Save report
    if output_report is None:
        output_report = os.path.join(
            PROJECT_ROOT, "reports",
            f"adjustment_comparison_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        )
    os.makedirs(os.path.dirname(output_report), exist_ok=True)
    with open(output_report, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\nReport saved to: {output_report}")

    return report


def _build_report(
    results: dict[str, BacktestResult],
    cfg: dict,
    predictions_path: str,
    databento_csv: str,
) -> str:
    """Build a comprehensive markdown comparison report."""
    lines: list[str] = []

    lines.append("# Data Adjustment Backtest Comparison Report")
    lines.append("")
    lines.append(f"**Generated:** {datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"**Strategy:** {cfg.get('nickname', 'unknown')}")
    lines.append(f"**Predictions:** `{os.path.basename(predictions_path)}`")
    lines.append(f"**Source Data:** `{os.path.basename(databento_csv)}`")
    lines.append("")

    # Explanation section
    lines.append("## Background")
    lines.append("")
    lines.append("This report compares the same trading signals evaluated against three")
    lines.append("different OHLCV price adjustment methods to quantify the impact of")
    lines.append("data adjustment on backtest results:")
    lines.append("")
    lines.append("| Method | Description | Dollar PnL Preserved? | Price Ratios Preserved? |")
    lines.append("|--------|-------------|-----------------------|-------------------------|")
    lines.append("| **Raw Unadjusted** | Front-month prices with rollover gaps | Yes (per-contract) | Yes |")
    lines.append("| **Panama Canal** | Additive back-adjustment (gap subtracted backward) | Yes (series-wide) | No |")
    lines.append("| **Ratio Adjusted** | Multiplicative back-adjustment (current = anchor) | No | Yes |")
    lines.append("")

    # Aggregate comparison table
    lines.append("## Aggregate Results")
    lines.append("")
    modes = ["raw", "panama", "ratio"]
    labels = {
        "raw": "Raw Unadjusted",
        "panama": "Panama Canal",
        "ratio": "Ratio Adjusted",
    }

    header = "| Metric | " + " | ".join(labels[m] for m in modes) + " |"
    sep = "|--------|" + "|".join(["--------"] * len(modes)) + "|"
    lines.append(header)
    lines.append(sep)

    def _row(metric: str, values: list[str]) -> str:
        return f"| {metric} | " + " | ".join(values) + " |"

    lines.append(_row("Trades", [
        str(results[m].trade_count) for m in modes
    ]))
    lines.append(_row("Win Rate", [
        f"{results[m].win_rate:.1%}" for m in modes
    ]))
    lines.append(_row("Profit Factor", [
        f"{results[m].profit_factor:.3f}" for m in modes
    ]))
    lines.append(_row("Total Net PnL", [
        f"${results[m].total_pnl:,.2f}" for m in modes
    ]))
    lines.append(_row("Max Drawdown", [
        f"${results[m].max_drawdown:,.2f}" for m in modes
    ]))
    lines.append(_row("Avg Trade PnL", [
        f"${_compute_avg_trade(results[m]):,.2f}" for m in modes
    ]))
    lines.append(_row("Monthly Sharpe", [
        f"{_compute_sharpe(results[m]):.3f}" for m in modes
    ]))
    lines.append(_row("Monthly Sortino", [
        f"{_compute_sortino(results[m]):.3f}" for m in modes
    ]))

    # Exit distribution
    lines.append("")
    lines.append("## Exit Distribution")
    lines.append("")
    exit_header = "| Exit Reason | " + " | ".join(labels[m] for m in modes) + " |"
    lines.append(exit_header)
    lines.append(sep)
    for reason in ["TP", "SL", "TRAILING_BE", "TIME_BARRIER", "SIGNAL_EXIT"]:
        vals = []
        for m in modes:
            dist = results[m].exit_distribution
            d = dist.get(reason, {"count": 0, "pct": 0.0})
            vals.append(f"{int(d['count'])} ({d['pct']:.1f}%)")
        lines.append(_row(reason, vals))

    # Yearly PnL breakdown
    lines.append("")
    lines.append("## Yearly PnL Breakdown")
    lines.append("")

    yearly_data = {m: _yearly_pnl_breakdown(results[m]) for m in modes}
    all_years = sorted(set().union(*(yd.keys() for yd in yearly_data.values())))

    yr_header = "| Year | " + " | ".join(labels[m] for m in modes) + " |"
    lines.append(yr_header)
    lines.append(sep)
    for yr in all_years:
        vals = [f"${yearly_data[m].get(yr, 0.0):,.2f}" for m in modes]
        lines.append(_row(str(yr), vals))

    # Key findings
    lines.append("")
    lines.append("## Key Observations")
    lines.append("")

    raw_pnl = results["raw"].total_pnl
    panama_pnl = results["panama"].total_pnl
    ratio_pnl = results["ratio"].total_pnl

    if abs(ratio_pnl - raw_pnl) > 100:
        pct_diff = ((ratio_pnl - raw_pnl) / abs(raw_pnl) * 100) if raw_pnl != 0 else float("inf")
        lines.append(f"- **Ratio vs Raw PnL gap:** ${ratio_pnl - raw_pnl:,.2f} "
                     f"({pct_diff:+.1f}%) -- ratio adjustment inflates historical "
                     f"price moves, distorting dollar PnL.")
    else:
        lines.append("- Ratio vs Raw PnL are very close -- adjustment has minimal impact.")

    if abs(panama_pnl - raw_pnl) < abs(ratio_pnl - raw_pnl):
        lines.append("- **Panama Canal is closer to Raw** than Ratio, confirming it better "
                     "preserves dollar PnL across the series.")

    if results["raw"].trade_count != results["ratio"].trade_count:
        lines.append(f"- **Trade count differs:** Raw={results['raw'].trade_count}, "
                     f"Ratio={results['ratio'].trade_count} -- the adjustment method "
                     f"affects ATR calculations and therefore entry/exit levels.")
    else:
        lines.append("- Trade count is identical across all adjustment methods.")

    # Recommendation
    lines.append("")
    lines.append("## Recommendation")
    lines.append("")
    lines.append("> [!IMPORTANT]")
    lines.append("> For futures backtesting where the objective is accurate dollar PnL,")
    lines.append("> **Panama Canal (additive) back-adjustment** is the standard choice.")
    lines.append("> It preserves the absolute dollar magnitude of price moves across")
    lines.append("> contract rollovers while maintaining a gap-free continuous series")
    lines.append("> suitable for indicator calculations (ATR, moving averages, etc.).")
    lines.append(">")
    lines.append("> **Raw unadjusted** data gives the most truthful per-contract PnL")
    lines.append("> but introduces rollover gaps that corrupt indicator calculations.")
    lines.append(">")
    lines.append("> **Ratio-adjusted** data preserves price ratios but distorts")
    lines.append("> absolute dollar PnL -- a $1 ATR in 2012 might represent $2.17")
    lines.append("> in ratio-adjusted space, inflating both TP/SL distances and PnL.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Compare backtests across data adjustment methods"
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG,
        help="Strategy config JSON (default: hourly_ensemble_010.json)"
    )
    parser.add_argument(
        "--databento-csv",
        default=DEFAULT_DATABENTO_CSV,
        help="Raw Databento ohlcv-1h CSV"
    )
    parser.add_argument(
        "--predictions",
        default=None,
        help="Predictions CSV (default: auto-discover latest sweep)"
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output report path (default: reports/adjustment_comparison_*.md)"
    )

    args = parser.parse_args()

    # Auto-discover predictions if not provided
    predictions_path = args.predictions
    if predictions_path is None:
        predictions_path = _find_latest_predictions()
        if predictions_path is None:
            print("ERROR: Could not auto-discover predictions CSV. "
                  "Use --predictions to specify one.")
            sys.exit(1)
        print(f"Auto-discovered predictions: {predictions_path}")

    # Validate inputs
    for label, path in [("Databento CSV", args.databento_csv),
                         ("Config", args.config),
                         ("Predictions", predictions_path)]:
        if not os.path.exists(path):
            print(f"ERROR: {label} not found: {path}")
            sys.exit(1)

    report = run_comparison(
        databento_csv=args.databento_csv,
        config_path=args.config,
        predictions_path=predictions_path,
        output_report=args.output,
    )

    # Print the report to console too
    print("\n" + "=" * 80)
    print(report)


if __name__ == "__main__":
    main()
