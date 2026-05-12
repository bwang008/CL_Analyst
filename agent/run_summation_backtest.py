"""
Option B — Mathematical Summation Backtest.

Runs independent Long and Short SingleModelStrategy backtests using their
individually-optimized Optuna parameters, then sums the results to produce
a true-parity combined metric that mirrors the asynchronous live trader.

This avoids the TieredEnsembleStrategy FSM bug (sequential state machine
cannot model concurrent Long+Short positions) and the config key hierarchy
mismatch (cfg["models"]["long"] vs cfg["long"]["tiers"]).

Usage:
    conda activate trader
    python agent/run_summation_backtest.py

    # Override scout directory:
    python agent/run_summation_backtest.py --scout-dir reports/scout_hs08_3x1_6h_20260511_2116

    # Override everything:
    python agent/run_summation_backtest.py \
        --long-config  path/to/_opt_long_logloss_opt.json \
        --short-config path/to/_opt_short_logloss_opt.json \
        --long-preds   path/to/oos_predictions_long_logloss.csv \
        --short-preds  path/to/oos_predictions_short_logloss.csv \
        --ohlcv        data/processed/CL_HourSet_08.parquet
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

from agent.backtest_engine import (
    BacktestEngine,
    BacktestResult,
    TradeRecord,
    format_report,
    load_ohlcv,
    load_predictions,
)
from agent.strategy_optimizer import extract_metrics


# ---------------------------------------------------------------------------
# Combined result builder
# ---------------------------------------------------------------------------


def combine_results(
    long_result: BacktestResult,
    short_result: BacktestResult,
) -> BacktestResult:
    """Merge two independent BacktestResults into a combined result.

    Trades are interleaved by entry_dt so the equity curve reflects
    the true chronological PnL path of running both strategies
    simultaneously.
    """
    all_trades = sorted(
        long_result.trades + short_result.trades,
        key=lambda t: t.entry_dt,
    )

    # Build a combined equity curve from the time-sorted trade stream
    combined_equity: list[float] = []
    cumulative_pnl = 0.0
    for t in all_trades:
        cumulative_pnl += t.net_pnl_dollars
        combined_equity.append(cumulative_pnl)

    # Determine overall date range
    start_dt = min(
        filter(None, [long_result.start_dt, short_result.start_dt]),
        default=None,
    )
    end_dt = max(
        filter(None, [long_result.end_dt, short_result.end_dt]),
        default=None,
    )

    return BacktestResult(
        trades=all_trades,
        equity_curve=combined_equity,
        label="Combined (Long + Short, Independent)",
        start_dt=start_dt,
        end_dt=end_dt,
    )


# ---------------------------------------------------------------------------
# Pretty printer
# ---------------------------------------------------------------------------


def print_comparison_table(
    long_m: dict,
    short_m: dict,
    combined_m: dict,
) -> str:
    """Print a side-by-side comparison table of Long, Short, and Combined."""

    header = (
        "\n"
        "=" * 80 + "\n"
        "  OPTION B — MATHEMATICAL SUMMATION BACKTEST (TRUE LIVE PARITY)\n"
        "=" * 80 + "\n"
        "  Architecture: Independent SingleModelStrategy engines, summed results\n"
        "  Rationale:    Mirrors the async, decoupled live trader (no FSM conflict)\n"
        "=" * 80 + "\n"
    )

    row_fmt = "  {label:<22s} | {long:>14s} | {short:>14s} | {combined:>14s}"
    sep = "  " + "-" * 72

    def fmt_dollar(v):
        return f"${v:>12,.2f}"

    def fmt_pct(v):
        return f"{v:>13.1%}"

    def fmt_float(v):
        return f"{v:>14.2f}"

    def fmt_int(v):
        return f"{v:>14,d}"

    lines = [header]
    lines.append(row_fmt.format(
        label="Metric", long="LONG", short="SHORT", combined="COMBINED"
    ))
    lines.append(sep)
    lines.append(row_fmt.format(
        label="Trade Count",
        long=fmt_int(long_m["trade_count"]),
        short=fmt_int(short_m["trade_count"]),
        combined=fmt_int(combined_m["trade_count"]),
    ))
    lines.append(row_fmt.format(
        label="Total Net PnL",
        long=fmt_dollar(long_m["total_pnl"]),
        short=fmt_dollar(short_m["total_pnl"]),
        combined=fmt_dollar(combined_m["total_pnl"]),
    ))
    lines.append(row_fmt.format(
        label="Win Rate",
        long=fmt_pct(long_m["win_rate"]),
        short=fmt_pct(short_m["win_rate"]),
        combined=fmt_pct(combined_m["win_rate"]),
    ))
    lines.append(row_fmt.format(
        label="Profit Factor",
        long=fmt_float(long_m["profit_factor"]),
        short=fmt_float(short_m["profit_factor"]),
        combined=fmt_float(combined_m["profit_factor"]),
    ))
    lines.append(row_fmt.format(
        label="Max Drawdown",
        long=fmt_dollar(long_m["max_drawdown"]),
        short=fmt_dollar(short_m["max_drawdown"]),
        combined=fmt_dollar(combined_m["max_drawdown"]),
    ))
    lines.append(row_fmt.format(
        label="Sharpe Ratio",
        long=fmt_float(long_m["sharpe_ratio"]),
        short=fmt_float(short_m["sharpe_ratio"]),
        combined=fmt_float(combined_m["sharpe_ratio"]),
    ))
    lines.append(row_fmt.format(
        label="Avg Trade PnL",
        long=fmt_dollar(long_m["avg_trade_pnl"]),
        short=fmt_dollar(short_m["avg_trade_pnl"]),
        combined=fmt_dollar(combined_m["avg_trade_pnl"]),
    ))
    lines.append(row_fmt.format(
        label="Expectancy",
        long=fmt_dollar(long_m["expectancy"]),
        short=fmt_dollar(short_m["expectancy"]),
        combined=fmt_dollar(combined_m["expectancy"]),
    ))
    lines.append(row_fmt.format(
        label="PnL / DD Ratio",
        long=fmt_float(long_m["profit_per_drawdown"]),
        short=fmt_float(short_m["profit_per_drawdown"]),
        combined=fmt_float(combined_m["profit_per_drawdown"]),
    ))
    lines.append(sep)

    # Exit distribution
    lines.append("\n  EXIT DISTRIBUTION")
    lines.append(sep)
    for reason in ["pct_tp", "pct_sl", "pct_trailing_be", "pct_time_barrier"]:
        label = reason.replace("pct_", "").upper().replace("_", " ")
        lines.append(row_fmt.format(
            label=f"  {label}",
            long=f"{long_m[reason]:>13.1f}%",
            short=f"{short_m[reason]:>13.1f}%",
            combined=f"{combined_m[reason]:>13.1f}%",
        ))
    lines.append(sep)
    lines.append("")

    text = "\n".join(lines)
    print(text)
    return text


# ---------------------------------------------------------------------------
# Config builder: extract SingleModelStrategy config from optimizer output
# ---------------------------------------------------------------------------


def build_single_model_config(opt_cfg: dict, direction: str) -> dict:
    """Convert an optimizer-output config into a clean SingleModelStrategy config.

    CRITICAL: The optimizer output may be a TieredEnsembleStrategy config where
    the ACTUAL effective parameters come from ``cfg["long"]["tiers"]`` or
    ``cfg["short"]["tiers"]``, NOT from the top-level or optuna_info.params.

    Tier-level values override top-level values in TieredEnsembleStrategy:
        - tier.min_prob  → the real threshold (overrides entry_threshold)
        - tier.tp_atr_mult → the real TP (overrides top-level tp_atr_mult)
        - tier.sl_atr_mult → the real SL (overrides top-level sl_atr_mult)
        - tier.trailing_atr_mult → the real trailing (if not None)
        - tier.max_hold_bars → the real max hold (if not None)

    This function detects TieredEnsembleStrategy configs and extracts the
    true effective parameters from the tier, falling back to top-level/params
    only when the tier value is None.

    Args:
        opt_cfg: Raw optimizer output JSON (e.g., _opt_long_logloss_opt.json).
        direction: "LONG" or "SHORT".

    Returns:
        A clean config dict for BacktestEngine.from_config().
    """
    params = opt_cfg.get("optuna_info", {}).get("params", {})
    dir_key = direction.lower()  # "long" or "short"

    # --- Detect TieredEnsembleStrategy tier overrides ---
    # If the original config was TieredEnsembleStrategy, the tier values
    # were the REAL effective parameters (they override the top-level).
    tier_block = opt_cfg.get(dir_key, {})
    tiers = tier_block.get("tiers", [])
    tier = tiers[0] if tiers else {}

    was_tiered = opt_cfg.get("execution_class") == "TieredEnsembleStrategy" and tier
    if was_tiered:
        # Tier values override top-level — extract the TRUE effective params
        eff_threshold = tier.get("min_prob", 0.55)
        eff_tp = tier.get("tp_atr_mult")  # may be None → falls back to top-level
        eff_sl = tier.get("sl_atr_mult")  # may be None → falls back to top-level
        eff_trail = tier.get("trailing_atr_mult")  # may be None → falls back to top-level
        eff_mhb = tier.get("max_hold_bars")  # may be None → falls back to top-level

        print(f"  [!] Detected TieredEnsembleStrategy config for {direction}.")
        print(f"      Extracting TRUE effective params from tier (overrides top-level):")
        print(f"      Tier threshold={eff_threshold}  tp={eff_tp}  sl={eff_sl}  "
              f"trail={eff_trail}  mhb={eff_mhb}")
    else:
        eff_threshold = None
        eff_tp = None
        eff_sl = None
        eff_trail = None
        eff_mhb = None

    # Resolve each parameter: tier override → optuna params → top-level config
    def _resolve(tier_val, param_key, cfg_key, default):
        if tier_val is not None:
            return tier_val
        if param_key in params:
            return params[param_key]
        return opt_cfg.get(cfg_key, default)

    threshold = _resolve(eff_threshold, "entry_threshold", "entry_threshold", 0.55)
    tp = _resolve(eff_tp, "tp_atr_mult", "tp_atr_mult", 2.0)
    sl = _resolve(eff_sl, "sl_atr_mult", "sl_atr_mult", 1.0)
    trail = _resolve(eff_trail, "trailing_atr_mult", "trailing_atr_mult", 1.0)
    mhb = _resolve(eff_mhb, "max_hold_bars", "max_hold_bars", 288)

    cfg = {
        "nickname": f"{opt_cfg.get('nickname', 'unknown')}_{direction}",
        "execution_class": "SingleModelStrategy",
        "direction": direction,
        "tp_atr_mult": tp,
        "sl_atr_mult": sl,
        "trailing_atr_mult": trail,
        "trailing_sl_atr_offset": opt_cfg.get("trailing_sl_atr_offset", 0.25),
        "cooldown_bars": params.get("cooldown_bars", opt_cfg.get("cooldown_bars", 5)),
        "tp_cooldown_bars": opt_cfg.get("tp_cooldown_bars", 0),
        "sl_cooldown_bars": opt_cfg.get("sl_cooldown_bars", 4),
        "max_hold_bars": int(mhb),
        "allow_concurrent": False,
        "max_concurrent": 1,
        "entry_threshold": threshold,
        "consecutive_signal_threshold": params.get(
            "consecutive_signal_threshold",
            opt_cfg.get("consecutive_signal_threshold", 0),
        ),
        "models": {
            dir_key: {
                "threshold": threshold,
            }
        },
    }
    return cfg


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Option B — Independent Long+Short summation backtest"
    )
    parser.add_argument(
        "--scout-dir",
        default="reports/scout_hs08_3x1_6h_20260511_2116/registry/canary_output",
        help="Path to the scout canary_output directory containing optimized configs and predictions",
    )
    parser.add_argument("--long-config", default=None, help="Override: path to optimized Long config JSON")
    parser.add_argument("--short-config", default=None, help="Override: path to optimized Short config JSON")
    parser.add_argument("--long-preds", default=None, help="Override: path to Long OOS predictions CSV")
    parser.add_argument("--short-preds", default=None, help="Override: path to Short OOS predictions CSV")
    parser.add_argument("--ohlcv", default="data/processed/CL_HourSet_08.parquet", help="Path to OHLCV parquet")
    parser.add_argument("--report-file", default=None, help="Save combined report to this file")
    args = parser.parse_args()

    # Resolve paths from scout directory if not explicitly overridden
    scout_dir = args.scout_dir
    long_config_path = args.long_config or os.path.join(scout_dir, "_opt_long_logloss_opt.json")
    short_config_path = args.short_config or os.path.join(scout_dir, "_opt_short_logloss_opt.json")
    long_preds_path = args.long_preds or os.path.join(scout_dir, "oos_predictions_long_logloss.csv")
    short_preds_path = args.short_preds or os.path.join(scout_dir, "oos_predictions_short_logloss.csv")
    ohlcv_path = args.ohlcv

    print("=" * 80)
    print("  OPTION B: INDEPENDENT SUMMATION BACKTEST")
    print("=" * 80)
    print(f"  Long Config:  {long_config_path}")
    print(f"  Short Config: {short_config_path}")
    print(f"  Long Preds:   {long_preds_path}")
    print(f"  Short Preds:  {short_preds_path}")
    print(f"  OHLCV:        {ohlcv_path}")
    print("=" * 80)

    # Validate all files exist
    for label, path in [
        ("Long config", long_config_path),
        ("Short config", short_config_path),
        ("Long predictions", long_preds_path),
        ("Short predictions", short_preds_path),
        ("OHLCV data", ohlcv_path),
    ]:
        if not os.path.exists(path):
            print(f"\n  ERROR: {label} not found at: {path}")
            sys.exit(1)

    # Load configs
    with open(long_config_path) as f:
        long_opt_raw = json.load(f)
    with open(short_config_path) as f:
        short_opt_raw = json.load(f)

    # Build clean SingleModelStrategy configs
    long_cfg = build_single_model_config(long_opt_raw, "LONG")
    short_cfg = build_single_model_config(short_opt_raw, "SHORT")

    print(f"\n  LONG strategy params:")
    print(f"    Threshold: {long_cfg['entry_threshold']}")
    print(f"    TP: {long_cfg['tp_atr_mult']}x  SL: {long_cfg['sl_atr_mult']}x  Trail: {long_cfg['trailing_atr_mult']}x")
    print(f"    Cooldown: {long_cfg['cooldown_bars']}  Max Hold: {long_cfg['max_hold_bars']}")
    print(f"    Consecutive Signals: {long_cfg['consecutive_signal_threshold']}")

    print(f"\n  SHORT strategy params:")
    print(f"    Threshold: {short_cfg['entry_threshold']}")
    print(f"    TP: {short_cfg['tp_atr_mult']}x  SL: {short_cfg['sl_atr_mult']}x  Trail: {short_cfg['trailing_atr_mult']}x")
    print(f"    Cooldown: {short_cfg['cooldown_bars']}  Max Hold: {short_cfg['max_hold_bars']}")
    print(f"    Consecutive Signals: {short_cfg['consecutive_signal_threshold']}")

    # Load data
    print("\n  Loading data...")
    long_preds = load_predictions(long_preds_path)
    short_preds = load_predictions(short_preds_path)
    ohlcv = load_ohlcv(ohlcv_path)
    print(f"    Long predictions:  {len(long_preds):,} rows  ({long_preds.index.min()} to {long_preds.index.max()})")
    print(f"    Short predictions: {len(short_preds):,} rows ({short_preds.index.min()} to {short_preds.index.max()})")
    print(f"    OHLCV:             {len(ohlcv):,} rows")

    # -----------------------------------------------------------------------
    # Run independent backtests
    # -----------------------------------------------------------------------

    print("\n  Running LONG backtest (SingleModelStrategy)...")
    long_engine = BacktestEngine.from_config(long_cfg)
    long_result = long_engine.run(long_preds, ohlcv, label="Long (Independent)")

    print("  Running SHORT backtest (SingleModelStrategy)...")
    short_engine = BacktestEngine.from_config(short_cfg)
    short_result = short_engine.run(short_preds, ohlcv, label="Short (Independent)")

    # -----------------------------------------------------------------------
    # Combine results
    # -----------------------------------------------------------------------

    combined_result = combine_results(long_result, short_result)

    # -----------------------------------------------------------------------
    # Extract metrics and display
    # -----------------------------------------------------------------------

    long_metrics = extract_metrics(long_result)
    short_metrics = extract_metrics(short_result)
    combined_metrics = extract_metrics(combined_result)

    report_text = print_comparison_table(long_metrics, short_metrics, combined_metrics)

    # Print the full combined report using the standard format_report
    combined_report = format_report(combined_result)
    print(combined_report)

    # -----------------------------------------------------------------------
    # Verify against known optimizer values
    # -----------------------------------------------------------------------

    long_expected = long_opt_raw.get("optuna_info", {}).get("metrics", {})
    short_expected = short_opt_raw.get("optuna_info", {}).get("metrics", {})

    if long_expected:
        delta = abs(long_metrics["total_pnl"] - long_expected.get("total_pnl", 0))
        status = "MATCH" if delta < 1.0 else f"DELTA=${delta:,.2f}"
        print(f"\n  PARITY CHECK - LONG:  Optimizer=${long_expected.get('total_pnl', 0):,.2f}  "
              f"Reproduced=${long_metrics['total_pnl']:,.2f}  [{status}]")

    if short_expected:
        delta = abs(short_metrics["total_pnl"] - short_expected.get("total_pnl", 0))
        status = "MATCH" if delta < 1.0 else f"DELTA=${delta:,.2f}"
        print(f"  PARITY CHECK - SHORT: Optimizer=${short_expected.get('total_pnl', 0):,.2f}  "
              f"Reproduced=${short_metrics['total_pnl']:,.2f}  [{status}]")

    print(f"\n  COMBINED TRUE PARITY PnL: ${combined_metrics['total_pnl']:,.2f}")
    print(f"  (vs broken ensemble PnL: $-24,684.87  —  swing of ${combined_metrics['total_pnl'] - (-24684.87):+,.2f})")

    # Save report if requested
    report_path = args.report_file or "reports/HourSet_08_OptionB_Summation.md"
    os.makedirs(os.path.dirname(report_path) or ".", exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("# Option B — Mathematical Summation Backtest (True Live Parity)\n\n")
        f.write(f"**Architecture:** Independent SingleModelStrategy engines, summed results\n")
        f.write(f"**Scout Source:** `{scout_dir}`\n\n")
        f.write("```\n")
        f.write(report_text)
        f.write("\n")
        f.write(combined_report)
        f.write("\n```\n")
    print(f"\n  Report saved: {report_path}")
    print("=" * 80)


if __name__ == "__main__":
    main()
