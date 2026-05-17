#!/usr/bin/env python
"""Trade Reconciler — Post-Mortem Execution Parity Audit.

Programmatically diffs backtest trade ledgers against live trading ledgers
to surface mathematical deltas in pricing, stop-loss activation, and fill
logic.

Usage:
    # Compare a backtest CSV against the live telemetry DB
    python scripts/trade_reconciler.py \\
        --backtest-csv reports/backtest_trades.csv \\
        --live-db C:/CL_Analyst_Data/data/live_telemetry.db

    # Run backtest fresh from a strategy config + OHLCV, then compare
    python scripts/trade_reconciler.py \\
        --config configs/strategies/my_strategy.json \\
        --ohlcv data/raw/CL.csv \\
        --live-db C:/CL_Analyst_Data/data/live_telemetry.db

    # Export live ledger only (for manual inspection)
    python scripts/trade_reconciler.py \\
        --live-db C:/CL_Analyst_Data/data/live_telemetry.db \\
        --export-live-csv reports/live_trades.csv

Author: CL Analyst
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

import pandas as pd

# ---------------------------------------------------------------------------
# Add project root to sys.path for imports
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EXIT_REASON_MAP = {
    # Live -> Backtest canonical names
    "TP_HIT": "TP",
    "SL_HIT": "SL",
    "TRAILING_SL": "TRAILING_BE",
    "TIME_BARRIER": "TIME_BARRIER",
    "CLOSED_OOB": "CLOSED_OOB",
}

_BACKTEST_EXIT_MAP = {
    # BacktestEngine ExitReason enum values
    "TP": "TP",
    "SL": "SL",
    "TRAILING_BE": "TRAILING_BE",
    "TIME_BARRIER": "TIME_BARRIER",
}


def _normalize_exit_reason(reason: str | None) -> str:
    """Normalize exit reason strings to a common format."""
    if reason is None:
        return "UNKNOWN"
    reason = str(reason).strip().upper()
    if reason in _EXIT_REASON_MAP:
        return _EXIT_REASON_MAP[reason]
    if reason in _BACKTEST_EXIT_MAP:
        return _BACKTEST_EXIT_MAP[reason]
    return reason


def _floor_to_bar(ts: pd.Timestamp, bar_size: str = "1h") -> pd.Timestamp:
    """Floor a timestamp to the top of the bar boundary.

    For 1-hour strategies, floors to the hour.
    """
    if bar_size == "1h":
        return ts.floor("h")
    elif bar_size == "5min":
        return ts.floor("5min")
    else:
        return ts.floor("h")


def _load_live_ledger(db_path: str) -> pd.DataFrame:
    """Load the live trade ledger from the telemetry SQLite DB."""
    from src.live_execution.telemetry import TelemetryDB

    tdb = TelemetryDB(db_path=db_path)
    df = tdb.export_trade_ledger()
    tdb.close()

    if df.empty:
        print("WARNING: No closed positions found in live telemetry DB.")
        return df

    # Normalize exit reasons
    df["exit_reason"] = df["exit_reason"].apply(_normalize_exit_reason)

    # Convert side strings
    df["signal_side"] = df["signal_side"].str.upper()

    print(f"Loaded {len(df)} live trades from {db_path}")
    return df


def _load_backtest_csv(csv_path: str) -> pd.DataFrame:
    """Load a backtest trade ledger from CSV."""
    df = pd.read_csv(csv_path, parse_dates=["entry_time", "exit_time"])

    # Normalize exit reasons
    df["exit_reason"] = df["exit_reason"].apply(_normalize_exit_reason)

    print(f"Loaded {len(df)} backtest trades from {csv_path}")
    return df


# ---------------------------------------------------------------------------
# Core reconciliation
# ---------------------------------------------------------------------------

def reconcile(
    backtest_df: pd.DataFrame,
    live_df: pd.DataFrame,
    *,
    bar_size: str = "1h",
    price_tolerance: float = 0.05,
) -> tuple[pd.DataFrame, dict]:
    """Reconcile backtest and live trade ledgers.

    Time alignment: floors live timestamps to the bar boundary to match
    the backtest's bar-level timestamps.

    Matching: joins on (floored entry_time, signal_side).

    Returns:
        matched_df: DataFrame with per-trade comparison columns
        summary: dict with aggregate stats
    """
    if backtest_df.empty or live_df.empty:
        return pd.DataFrame(), {"error": "One or both ledgers are empty"}

    # --- Prepare backtest ---
    bt = backtest_df.copy()
    bt["entry_bar"] = bt["entry_time"].apply(lambda t: _floor_to_bar(t, bar_size))
    bt = bt.sort_values("entry_bar").reset_index(drop=True)

    # --- Prepare live ---
    lv = live_df.copy()
    lv["entry_bar"] = lv["entry_time"].apply(lambda t: _floor_to_bar(t, bar_size))
    lv = lv.sort_values("entry_bar").reset_index(drop=True)

    # Only compare trades within the live trading window
    live_start = lv["entry_bar"].min()
    live_end = lv["entry_bar"].max()
    bt_in_window = bt[
        (bt["entry_bar"] >= live_start) & (bt["entry_bar"] <= live_end)
    ].copy()

    print(f"\nReconciliation window: {live_start} to {live_end}")
    print(f"  Backtest trades in window: {len(bt_in_window)}")
    print(f"  Live trades in window:     {len(lv)}")

    # --- Match by (entry_bar, signal_side) ---
    # Use merge to find matching pairs
    matched = pd.merge(
        bt_in_window,
        lv,
        on=["entry_bar", "signal_side"],
        how="outer",
        suffixes=("_bt", "_live"),
        indicator=True,
    )

    # Classify matches
    both = matched[matched["_merge"] == "both"].copy()
    bt_only = matched[matched["_merge"] == "left_only"].copy()
    live_only = matched[matched["_merge"] == "right_only"].copy()

    # --- Compute deltas for matched trades ---
    comparison_rows = []
    for _, row in both.iterrows():
        entry_delta = _safe_delta(row.get("entry_price_live"), row.get("entry_price_bt"))
        exit_delta = _safe_delta(row.get("exit_price_live"), row.get("exit_price_bt"))
        tp_delta = _safe_delta(
            row.get("initial_tp_price_live"), row.get("initial_tp_price_bt")
        )
        sl_delta = _safe_delta(
            row.get("initial_sl_price_live"), row.get("initial_sl_price_bt")
        )
        pnl_delta = _safe_delta(
            row.get("net_pnl_dollars_live"), row.get("net_pnl_dollars_bt")
        )

        exit_match = (
            _normalize_exit_reason(str(row.get("exit_reason_bt", "")))
            == _normalize_exit_reason(str(row.get("exit_reason_live", "")))
        )

        comparison_rows.append({
            "entry_bar": row["entry_bar"],
            "signal_side": row["signal_side"],
            "entry_price_bt": row.get("entry_price_bt"),
            "entry_price_live": row.get("entry_price_live"),
            "Δ_entry_price": entry_delta,
            "exit_price_bt": row.get("exit_price_bt"),
            "exit_price_live": row.get("exit_price_live"),
            "Δ_exit_price": exit_delta,
            "initial_tp_bt": row.get("initial_tp_price_bt"),
            "initial_tp_live": row.get("initial_tp_price_live"),
            "Δ_tp": tp_delta,
            "initial_sl_bt": row.get("initial_sl_price_bt"),
            "initial_sl_live": row.get("initial_sl_price_live"),
            "Δ_sl": sl_delta,
            "exit_reason_bt": row.get("exit_reason_bt"),
            "exit_reason_live": row.get("exit_reason_live"),
            "exit_reason_match": exit_match,
            "pnl_bt": row.get("net_pnl_dollars_bt"),
            "pnl_live": row.get("net_pnl_dollars_live"),
            "Δ_pnl": pnl_delta,
            "duration_bt": row.get("duration_bars_bt"),
            "duration_live": row.get("duration_bars_live"),
        })

    comp_df = pd.DataFrame(comparison_rows)

    # --- Build summary ---
    summary = {
        "reconciliation_window": f"{live_start} → {live_end}",
        "backtest_trades_in_window": len(bt_in_window),
        "live_trades": len(lv),
        "matched_pairs": len(both),
        "backtest_only_orphans": len(bt_only),
        "live_only_orphans": len(live_only),
    }

    if not comp_df.empty:
        summary["exit_reason_match_rate"] = (
            f"{comp_df['exit_reason_match'].sum()}/{len(comp_df)} "
            f"({comp_df['exit_reason_match'].mean() * 100:.0f}%)"
        )
        for col in ["Δ_entry_price", "Δ_exit_price", "Δ_tp", "Δ_sl", "Δ_pnl"]:
            vals = comp_df[col].dropna()
            if len(vals) > 0:
                summary[f"{col}_mean"] = round(vals.mean(), 4)
                summary[f"{col}_max_abs"] = round(vals.abs().max(), 4)

    # Add orphan details
    if len(bt_only) > 0:
        summary["backtest_orphan_bars"] = bt_only["entry_bar"].tolist()
    if len(live_only) > 0:
        summary["live_orphan_bars"] = live_only["entry_bar"].tolist()

    return comp_df, summary


def _safe_delta(a, b) -> Optional[float]:
    """Compute a - b, returning None if either is None/NaN."""
    try:
        if a is None or b is None:
            return None
        a_f, b_f = float(a), float(b)
        if pd.isna(a_f) or pd.isna(b_f):
            return None
        return round(a_f - b_f, 4)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------

def format_report(
    comp_df: pd.DataFrame,
    summary: dict,
) -> str:
    """Format the reconciliation results as a structured text report."""
    w = 90
    lines = []

    lines.append("=" * w)
    lines.append("TRADE RECONCILIATION REPORT".center(w))
    lines.append("=" * w)

    # Summary section
    lines.append("")
    lines.append("SUMMARY")
    lines.append("-" * 40)
    for key, val in summary.items():
        if isinstance(val, list):
            lines.append(f"  {key}:")
            for item in val:
                lines.append(f"    - {item}")
        else:
            lines.append(f"  {key}: {val}")

    # Per-trade comparison
    if not comp_df.empty:
        lines.append("")
        lines.append("PER-TRADE COMPARISON")
        lines.append("-" * 40)

        for _, row in comp_df.iterrows():
            lines.append("")
            lines.append(
                f"  [{row['signal_side']}] {row['entry_bar']}"
            )
            lines.append(
                f"    Entry Price:  BT={_fmt(row.get('entry_price_bt'))}  "
                f"LIVE={_fmt(row.get('entry_price_live'))}  "
                f"Δ={_fmt(row.get('Δ_entry_price'))}"
            )
            lines.append(
                f"    Exit Price:   BT={_fmt(row.get('exit_price_bt'))}  "
                f"LIVE={_fmt(row.get('exit_price_live'))}  "
                f"Δ={_fmt(row.get('Δ_exit_price'))}"
            )
            lines.append(
                f"    Initial TP:   BT={_fmt(row.get('initial_tp_bt'))}  "
                f"LIVE={_fmt(row.get('initial_tp_live'))}  "
                f"Δ={_fmt(row.get('Δ_tp'))}"
            )
            lines.append(
                f"    Initial SL:   BT={_fmt(row.get('initial_sl_bt'))}  "
                f"LIVE={_fmt(row.get('initial_sl_live'))}  "
                f"Δ={_fmt(row.get('Δ_sl'))}"
            )
            exit_match_icon = "✅" if row.get("exit_reason_match") else "❌"
            lines.append(
                f"    Exit Reason:  BT={row.get('exit_reason_bt', '?')}  "
                f"LIVE={row.get('exit_reason_live', '?')}  "
                f"{exit_match_icon}"
            )
            lines.append(
                f"    Duration:     BT={row.get('duration_bt', '?')} bars  "
                f"LIVE={row.get('duration_live', '?')} bars"
            )
            if row.get("pnl_bt") is not None or row.get("pnl_live") is not None:
                lines.append(
                    f"    PnL:          BT=${_fmt(row.get('pnl_bt'))}  "
                    f"LIVE=${_fmt(row.get('pnl_live'))}  "
                    f"Δ=${_fmt(row.get('Δ_pnl'))}"
                )

    # Structural divergence flags
    flags = []
    if summary.get("backtest_only_orphans", 0) > 0:
        flags.append(
            f"⚠️  {summary['backtest_only_orphans']} trade(s) exist in backtest "
            f"but NOT in live ledger (missed signals?)"
        )
    if summary.get("live_only_orphans", 0) > 0:
        flags.append(
            f"⚠️  {summary['live_only_orphans']} trade(s) exist in live ledger "
            f"but NOT in backtest (prediction/data mismatch?)"
        )
    if not comp_df.empty:
        mismatches = comp_df[~comp_df["exit_reason_match"]]
        if len(mismatches) > 0:
            flags.append(
                f"❌ {len(mismatches)} trade(s) have DIFFERENT exit reasons "
                f"between backtest and live"
            )
        big_entry_deltas = comp_df[
            comp_df["Δ_entry_price"].abs() > 0.05
        ] if "Δ_entry_price" in comp_df.columns else pd.DataFrame()
        if len(big_entry_deltas) > 0:
            flags.append(
                f"⚠️  {len(big_entry_deltas)} trade(s) have entry price "
                f"divergence > $0.05"
            )

    if flags:
        lines.append("")
        lines.append("STRUCTURAL DIVERGENCES")
        lines.append("-" * 40)
        for flag in flags:
            lines.append(f"  {flag}")

    lines.append("")
    lines.append("=" * w)

    return "\n".join(lines)


def _fmt(val) -> str:
    """Format a numeric value for display."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return "N/A"
    try:
        return f"{float(val):.2f}"
    except (ValueError, TypeError):
        return str(val)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Trade Reconciler — Post-Mortem Execution Parity Audit",
    )
    parser.add_argument(
        "--backtest-csv",
        help="Path to backtest trade ledger CSV (from BacktestResult.to_csv())",
    )
    parser.add_argument(
        "--live-db",
        help="Path to live telemetry SQLite DB",
    )
    parser.add_argument(
        "--bar-size",
        default="1h",
        help="Bar size for time alignment (default: 1h)",
    )
    parser.add_argument(
        "--export-live-csv",
        help="Export the live trade ledger to this CSV path (then exit)",
    )
    parser.add_argument(
        "--export-backtest-csv",
        help="Path to save the backtest ledger CSV (for archival)",
    )
    parser.add_argument(
        "--report-file",
        help="Path to save the reconciliation report text file",
    )
    args = parser.parse_args()

    # Default live DB path
    if args.live_db is None:
        try:
            from src.data_paths import get_data_path
            args.live_db = str(get_data_path("live_telemetry.db"))
        except Exception:
            args.live_db = "data/live_telemetry.db"

    # --- Export-only mode ---
    if args.export_live_csv:
        live_df = _load_live_ledger(args.live_db)
        if not live_df.empty:
            live_df.to_csv(args.export_live_csv, index=False)
            print(f"Exported {len(live_df)} live trades to {args.export_live_csv}")
        return

    # --- Full reconciliation ---
    if args.backtest_csv is None:
        print("ERROR: --backtest-csv is required for reconciliation.")
        print("       Generate it via: BacktestResult.to_csv('path.csv')")
        sys.exit(1)

    backtest_df = _load_backtest_csv(args.backtest_csv)
    live_df = _load_live_ledger(args.live_db)

    if args.export_backtest_csv:
        backtest_df.to_csv(args.export_backtest_csv, index=False)
        print(f"Archived backtest ledger to {args.export_backtest_csv}")

    comp_df, summary = reconcile(
        backtest_df,
        live_df,
        bar_size=args.bar_size,
    )

    report = format_report(comp_df, summary)
    print()
    print(report)

    if args.report_file:
        Path(args.report_file).parent.mkdir(parents=True, exist_ok=True)
        with open(args.report_file, "w", encoding="utf-8") as f:
            f.write(report + "\n")
        print(f"\nReport saved to {args.report_file}")


if __name__ == "__main__":
    main()
