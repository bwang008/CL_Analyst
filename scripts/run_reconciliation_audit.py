#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Run Reconciliation Audit -- Post-Mortem Execution Parity Check.

Executes the full 4-step reconciliation pipeline:
  1. Resample 5-min market_bars -> 1h OHLCV
  2. Build prediction probabilities from shadow_log
  3. Run BacktestEngine with the live strategy config
  4. Run trade_reconciler for the final diff report

Usage:
    python scripts/run_reconciliation_audit.py

Author: CL Analyst
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

# Force UTF-8 output on Windows to avoid cp1252 encoding errors
if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

TELEMETRY_DB = Path(r"C:\CL_Analyst_Data\data\live_telemetry.db")
STRATEGY_CONFIG = _PROJECT_ROOT / "configs" / "strategies" / "hs08_scout_3x1_12h_logloss_opt.json"
BACKTEST_CSV = _PROJECT_ROOT / "reports" / "backtest_trades.csv"
REPORT_FILE = _PROJECT_ROOT / "reports" / "reconciliation_report.txt"


# =====================================================================
# Step 1
# =====================================================================

def step1_resample_ohlcv(conn: sqlite3.Connection) -> pd.DataFrame:
    """Step 1: Query 5-min market_bars and resample to 1-hour OHLCV.

    Challenge: The telemetry DB has two contiguous blocks of 5-min bars:
      - Feb 25 - Mar 2 (417 bars, ~6 days)
      - May 13 - May 15 (519 bars, ~3 days)
    with a 72-day gap between them.  The BacktestEngine computes ATR(14)
    on the hourly bars, which will be wildly inflated due to the gap.
    """
    print("=" * 70)
    print("STEP 1: Building 1-Hour OHLCV from Telemetry DB")
    print("=" * 70)

    df_5m = pd.read_sql(
        "SELECT timestamp, open, high, low, close, volume "
        "FROM market_bars ORDER BY timestamp ASC",
        conn,
    )
    df_5m["timestamp"] = pd.to_datetime(df_5m["timestamp"])
    df_5m = df_5m.set_index("timestamp")
    print(f"  Loaded {len(df_5m)} five-minute bars")
    print(f"  Range: {df_5m.index.min()} -> {df_5m.index.max()}")

    # Resample ALL data to 1-hour bars
    df_1h = df_5m.resample("1h").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    }).dropna()
    df_1h.columns = ["Open", "High", "Low", "Close", "Volume"]
    print(f"  Resampled to {len(df_1h)} one-hour bars")
    print(f"  Range: {df_1h.index.min()} -> {df_1h.index.max()}")

    # Identify gaps
    time_diffs = df_1h.index.to_series().diff()
    big_gaps = time_diffs[time_diffs > pd.Timedelta("2h")]
    if not big_gaps.empty:
        print(f"  OHLCV gaps > 2h:")
        for ts, gap in big_gaps.items():
            print(f"    {ts}: {gap}")

    # ATR diagnostic: compare engine ATR vs live bot ATR
    tr = np.maximum(
        df_1h["High"] - df_1h["Low"],
        np.maximum(
            (df_1h["High"] - df_1h["Close"].shift(1)).abs(),
            (df_1h["Low"] - df_1h["Close"].shift(1)).abs(),
        ),
    )
    atr_14 = tr.rolling(14).mean()

    trade_bars = [
        ("Trade #1", pd.Timestamp("2026-05-13 10:00:00"), 0.5101),
        ("Trade #2", pd.Timestamp("2026-05-13 17:00:00"), 0.5391),
        ("Trade #3", pd.Timestamp("2026-05-14 19:00:00"), 0.4044),
    ]
    print(f"\n  ATR comparison (engine 1h ATR vs live 5m ATR):")
    for name, ts, live_atr in trade_bars:
        engine_atr = atr_14.get(ts, float("nan"))
        ratio = engine_atr / live_atr if not pd.isna(engine_atr) and live_atr > 0 else float("nan")
        print(f"    {name} ({ts}): engine={engine_atr:.4f}  live={live_atr:.4f}  ratio={ratio:.2f}x")

    bars_before = len(df_1h[df_1h.index < pd.Timestamp("2026-05-13 10:00:00")])
    print(f"\n  Bars before first trade: {bars_before}")
    if bars_before < 14:
        print("  [WARN] Fewer than 14 bars -- ATR may be unreliable!")
    else:
        print("  [OK] Sufficient lookback for ATR computation")

    return df_1h


# =====================================================================
# Step 2
# =====================================================================

def step2_build_predictions(conn: sqlite3.Connection) -> pd.DataFrame:
    """Step 2: Build prediction probabilities from shadow_log."""
    print()
    print("=" * 70)
    print("STEP 2: Building Prediction Probabilities from Shadow Log")
    print("=" * 70)

    shadow_df = pd.read_sql(
        "SELECT timestamp, prob_buy, prob_sell, strategy_name "
        "FROM shadow_log ORDER BY timestamp ASC",
        conn,
    )
    shadow_df["timestamp"] = pd.to_datetime(shadow_df["timestamp"])
    shadow_df = shadow_df.set_index("timestamp")
    print(f"  Loaded {len(shadow_df)} shadow_log entries")
    print(f"  Range: {shadow_df.index.min()} -> {shadow_df.index.max()}")

    may_entries = shadow_df[shadow_df.index >= "2026-05-13"]
    print(f"  May 13+ entries: {len(may_entries)}")

    if len(may_entries) == 0:
        print("  [WARN] No shadow_log in May -- falling back to trade_ledger")
        return _fallback_predictions_from_ledger(conn)

    preds = pd.DataFrame(index=shadow_df.index)
    preds["prob_Buy"] = shadow_df["prob_buy"].fillna(0.0)
    preds["prob_Sell"] = shadow_df["prob_sell"].fillna(0.0)

    print(f"  prob_Buy range: {preds['prob_Buy'].min():.4f} -> {preds['prob_Buy'].max():.4f}")
    print(f"  prob_Sell range: {preds['prob_Sell'].min():.4f} -> {preds['prob_Sell'].max():.4f}")
    buy_signals = (preds["prob_Buy"] >= 0.5).sum()
    sell_signals = (preds["prob_Sell"] >= 0.5).sum()
    print(f"  Bars with prob_Buy >= 0.5: {buy_signals}")
    print(f"  Bars with prob_Sell >= 0.5: {sell_signals}")
    return preds


def _fallback_predictions_from_ledger(conn: sqlite3.Connection) -> pd.DataFrame:
    """Fallback: Build sparse predictions from trade_ledger confidence_pct."""
    ledger_df = pd.read_sql(
        "SELECT timestamp, signal, confidence_pct, direction "
        "FROM trade_ledger "
        "WHERE action_taken IN ('EXECUTE', 'EXECUTE_EXIT') "
        "  AND timestamp >= '2026-05-13' "
        "ORDER BY timestamp ASC",
        conn,
    )
    ledger_df["timestamp"] = pd.to_datetime(ledger_df["timestamp"])
    ledger_df = ledger_df.set_index("timestamp")
    preds = pd.DataFrame(index=ledger_df.index)
    preds["prob_Buy"] = ledger_df.apply(
        lambda r: r["confidence_pct"] / 100.0 if r["signal"] == "Buy" else 0.0,
        axis=1,
    )
    preds["prob_Sell"] = 0.0
    print(f"  Fallback: {len(preds)} sparse prediction entries from trade_ledger")
    return preds


# =====================================================================
# Step 3
# =====================================================================

def step3_run_backtest(preds: pd.DataFrame, df_1h: pd.DataFrame) -> int:
    """Step 3: Run BacktestEngine and export trades to CSV.

    Returns the number of backtest trades produced.
    """
    print()
    print("=" * 70)
    print("STEP 3: Running BacktestEngine with Live Strategy Config")
    print("=" * 70)

    from agent.backtest_engine import BacktestEngine

    with open(STRATEGY_CONFIG) as f:
        config = json.load(f)

    print(f"  Strategy: {config.get('nickname', 'unknown')}")
    print(f"  Execution class: {config.get('execution_class', 'unknown')}")
    print(f"  Bar size: {config.get('bar_size', 'unknown')}")

    long_cfg = config.get("long", {})
    short_cfg = config.get("short", {})
    print(f"  Long:  TP={long_cfg.get('tp_atr_mult')}x  SL={long_cfg.get('sl_atr_mult')}x  "
          f"Trail={long_cfg.get('trailing_atr_mult')}x  MaxHold={long_cfg.get('max_hold_bars')}")
    print(f"  Short: TP={short_cfg.get('tp_atr_mult')}x  SL={short_cfg.get('sl_atr_mult')}x  "
          f"Trail={short_cfg.get('trailing_atr_mult')}x  MaxHold={short_cfg.get('max_hold_bars')}")

    bt = BacktestEngine.from_config(config)
    result = bt.run(preds, df_1h, label="Reconciliation: May 13-15")

    print(f"\n  Backtest complete:")
    print(f"    Total trades: {result.trade_count}")
    print(f"    Total PnL:    ${result.total_pnl:.2f}")
    if result.trades:
        print(f"    Win rate:     {result.win_rate:.1%}")
        print(f"\n  Trade details:")
        for i, t in enumerate(result.trades, 1):
            side_str = "LONG" if t.side == 1 else "SHORT"
            print(f"    #{i}: [{side_str}] {t.entry_dt} -> {t.exit_dt}  "
                  f"Entry=${t.entry_price:.2f}  TP=${t.initial_tp_price:.2f}  "
                  f"SL=${t.initial_sl_price:.2f}  Exit={t.exit_reason.value}  "
                  f"Bars={t.duration_bars}  PnL=${t.net_pnl_dollars:.2f}")
    else:
        print("\n  [NOTE] Backtest produced 0 completed trades.")
        print("  Root cause: ATR(14) on 1h bars = ~3.19 (6.25x the live bot's 0.51).")
        print("  Brackets are too wide to trigger within the available OHLCV window.")

    BACKTEST_CSV.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(str(BACKTEST_CSV))
    return result.trade_count


# =====================================================================
# Step 4
# =====================================================================

def step4_run_reconciler(bt_count: int) -> None:
    """Step 4: Run the trade reconciler.

    Calls the reconciler API directly (not via subprocess) to handle
    the empty backtest DataFrame gracefully.
    """
    print()
    print("=" * 70)
    print("STEP 4: Running Trade Reconciler")
    print("=" * 70)

    from scripts.trade_reconciler import (
        _load_live_ledger,
        reconcile,
        format_report,
    )

    live_df = _load_live_ledger(str(TELEMETRY_DB))

    if bt_count > 0:
        from scripts.trade_reconciler import _load_backtest_csv
        backtest_df = _load_backtest_csv(str(BACKTEST_CSV))
    else:
        backtest_df = pd.DataFrame(columns=[
            "entry_time", "signal_side", "entry_price", "entry_fill",
            "initial_tp_price", "initial_sl_price", "exit_time", "exit_price",
            "exit_fill", "exit_reason", "atr_at_entry", "duration_bars",
            "lots", "gross_pnl_dollars", "commission_dollars", "net_pnl_dollars",
        ])
        print(f"Using empty backtest ledger (engine produced 0 closed trades)")

    comp_df, summary = reconcile(backtest_df, live_df, bar_size="1h")
    report = format_report(comp_df, summary)

    # Append ATR mismatch analysis
    if bt_count == 0:
        report += _build_atr_mismatch_section()

    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write(report + "\n")
    print(f"\nReport saved to {REPORT_FILE}")

    print()
    print("=" * 70)
    print("FINAL REPORT")
    print("=" * 70)
    print()
    print(report)


def _build_atr_mismatch_section() -> str:
    """Build the ATR mismatch analysis section for the report."""
    return """

CRITICAL FINDING: ATR COMPUTATION MISMATCH
----------------------------------------
  The BacktestEngine produced 0 completed trades due to an ATR
  computation mismatch between the live bot and the backtester.

  The live bot computes ATR(14) on 5-minute bars from IBKR real-time data.
  The BacktestEngine computes ATR(14) on 1-hour bars resampled from the
  telemetry DB's 5-minute bars.

  Telemetry DB OHLCV Coverage:
    Block 1: Feb 25 - Mar 2, 2026 (417 five-minute bars, ~6 days)
    Block 2: May 13 - May 15, 2026 (519 five-minute bars, ~3 days)
    Gap:     72 days (Mar 2 -> May 13) with ZERO bars

  ATR at Trade Entry Bars:
    Trade #1 (May 13 10:00): Engine=3.1864  Live=0.5101  Ratio=6.25x
    Trade #2 (May 13 17:00): Engine=2.8171  Live=0.5391  Ratio=5.23x
    Trade #3 (May 14 19:00): Engine=0.9050  Live=0.4044  Ratio=2.24x

  Impact: TP/SL brackets computed by the engine are 2-6x wider than live.
  With TP = entry + $23.90 (vs live $3.83), no trade can close within the
  available OHLCV window. The engine enters a position and holds it
  indefinitely without hitting TP, SL, or TIME_BARRIER.

  Root Causes:
    1. OHLCV gap: The 72-day gap between Feb/March and May data causes
       a massive True Range spike on the first May bar (price jumped
       from ~68 to ~101 between blocks).
    2. ATR timescale mismatch: Engine uses 14 x 1-hour bars for ATR.
       Live bot uses 14 x 5-minute bars. Even with contiguous data,
       hourly ATR is inherently larger than 5-minute ATR because
       hourly bars capture more intra-hour price movement.

  Recommendation:
    - Harmonize ATR computation: configure the BacktestEngine to use
      the same bar size (5-min) as the live bot for bracket ATR, or
    - Add a bracket_atr_override_period to the strategy config, or
    - For reconciliation purposes, replay the backtest using 5-minute
      bars directly (not resampled hourly bars).


LIVE TRADE DETAILS (from active_positions)
----------------------------------------
  Trade #1: LONG entry=101.33 at 2026-05-13 10:00
    TP=105.16 SL=102.82(*) ATR=0.5101  Exit=SL_HIT bars=2
    (*) SL=102.82 is ABOVE entry price. This is the final SL after
        trailing stop moved it from the initial level. The reconciler
        reads sl_price from the DB, which stores the CURRENT (not
        initial) stop-loss. Initial SL = 101.33 - 5.0*0.51 = 98.78.
        The trade was stopped out at the trailing breakeven+ level.

  Trade #2: LONG entry=102.07 at 2026-05-13 17:00
    TP=106.11 SL=99.37 ATR=0.5391  Exit=TIME_BARRIER bars=295
    POTENTIAL TIME DILATION BUG: Config specifies max_hold_bars=240
    (designed for 1-hour bars = 240 hours = 10 days). However, the
    live bot held for 295 bars. Possible explanations:
      a) Time Dilation: If bars_held increments on 5-min ticks,
         TIME_BARRIER fires at 240*5min = 20 hours (not 240 hours).
         But this trade held for ~25 hours, suggesting the counter
         is NOT purely 5-min based.
      b) Reboot Reset: User reboots may have reset the bar counter,
         effectively extending the hold period.
      c) Bar-counting window: The 295 value may reflect the total
         bars_held across restarts, with the TIME_BARRIER check
         using a different (reset) counter internally.

  Trade #3: LONG entry=101.43 at 2026-05-14 19:00
    TP=104.46 SL=99.41 ATR=0.4044  Exit=CLOSED_OOB (manual close)
    Out-of-band close via manual TWS intervention. The BacktestEngine
    has no CLOSED_OOB exit mechanism, so this trade would always appear
    as a live-only orphan in any reconciliation -- expected behavior.
"""


# =====================================================================
# Main
# =====================================================================

def main() -> None:
    """Execute the full 4-step reconciliation pipeline."""
    print()
    print("=" * 70)
    print("  LIVE-TO-BACKTEST TRADE RECONCILIATION AUDIT")
    print("  Target: 3 Closed Trades from May 13-15, 2026")
    print("=" * 70)
    print()

    conn = sqlite3.connect(str(TELEMETRY_DB))
    try:
        df_1h = step1_resample_ohlcv(conn)
        preds = step2_build_predictions(conn)
        bt_count = step3_run_backtest(preds, df_1h)
        step4_run_reconciler(bt_count)
    finally:
        conn.close()

    print("\n[DONE] Reconciliation audit complete.")


if __name__ == "__main__":
    main()
