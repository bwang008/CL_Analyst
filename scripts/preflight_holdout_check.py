#!/usr/bin/env python3
"""
preflight_holdout_check.py — data-aware dry-run guard against OOS/holdout collapse.

There are TWO holdout mechanisms at two stages, and they can silently conflict:

  • train_cutoff_date        — end of TRAINING. Data < train_cutoff trains the model.
  • holdout_cutoff_date      — (3-way only) end of the VALIDATION window / start of the
                               final VAULT. In 3-way, the post-optimizer backtests the
                               VAULT = [holdout_cutoff_date, data_end].
                               If null (2-way), the post-optimizer backtests the full
                               OOS = [train_cutoff_date, data_end].
  • post_optimizer_holdout_months — a SEPARATE carve at the post-opt stage: the LAST N
                               months of whatever backtest window it receives are held
                               out as the post-opt holdout; everything before is "pre".

COLLAPSE: if the backtest window (vault in 3-way, OOS in 2-way) is <= post_optimizer_holdout_months,
the post-opt holdout carve swallows the ENTIRE window -> "pre" = 0 trades, and Optuna
"optimizes" on an empty backtest. This is what produced the HS14A 0/0/0 pre-trade run.

This check loads the dataset's actual date range and FAILS the dry run when the windows
collapse (or when a cutoff falls outside the data).

Exit codes: 0 = OK, 2 = FAIL (collapse / invalid windows), 0 with WARN text = data not found (skipped).
"""
import argparse
import json
import os
import sys

MONTH_DAYS = 30.44


def _resolve_dataset(symbol, dataset_version):
    if dataset_version.upper().startswith(symbol.upper()):
        name = f"{dataset_version}.parquet"
    else:
        name = f"{symbol}_{dataset_version}.parquet"
    for base in ("data/processed", r"C:\CL_Analyst_Data\data\processed"):
        p = os.path.join(base, name)
        if os.path.isfile(p):
            return p
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args()

    import pandas as pd

    m = json.load(open(args.manifest))
    b = m.get("baseline") or {}
    symbol = (b.get("symbol") or "").strip()
    tw = b.get("training_workflow") or {}
    dv = (b.get("data_workflow") or {}).get("dataset_version", "")
    train_cut = tw.get("train_cutoff_date")
    holdout_cut = tw.get("holdout_cutoff_date")
    holdout_months = (tw.get("optuna") or {}).get("post_optimizer_holdout_months")

    path = _resolve_dataset(symbol, dv)
    if not path:
        print(f"    [WARN] dataset {symbol}/{dv} not found locally — holdout-collapse check SKIPPED.")
        sys.exit(0)

    # Cheap: read index only (no feature columns). Skip gracefully if no parquet engine.
    try:
        idx = pd.read_parquet(path, columns=[]).index
    except ImportError as e:
        print(f"    [WARN] cannot read parquet ({e}); holdout-collapse check SKIPPED. "
              f"Run with an interpreter that has pyarrow to enable it.")
        sys.exit(0)
    data_start, data_end = idx.min(), idx.max()
    train_ts = pd.Timestamp(train_cut)

    if not (data_start < train_ts < data_end):
        print(f"    [FAIL] train_cutoff_date {train_ts.date()} is outside data range "
              f"[{data_start.date()} .. {data_end.date()}].")
        sys.exit(2)

    if holdout_cut:  # 3-way: post-opt backtests the VAULT [holdout_cutoff, data_end]
        hc = pd.Timestamp(holdout_cut)
        if not (train_ts < hc < data_end):
            print(f"    [FAIL] holdout_cutoff_date {hc.date()} must be within "
                  f"(train_cutoff {train_ts.date()}, data_end {data_end.date()}).")
            sys.exit(2)
        window_start, mode, wname = hc, "3-way", "vault"
    else:  # 2-way: post-opt backtests full OOS [train_cutoff, data_end]
        window_start, mode, wname = train_ts, "2-way", "OOS"

    backtest_months = (data_end - window_start).days / MONTH_DAYS

    if backtest_months <= holdout_months:
        print(f"    [FAIL] period collapse: {mode} {wname} window = {backtest_months:.1f}mo "
              f"({window_start.date()} .. {data_end.date()}), but post_optimizer_holdout_months "
              f"= {holdout_months}. The post-opt holdout carve swallows the entire {wname} -> "
              f"'pre' = 0 trades. Lower holdout_months, move holdout_cutoff_date earlier, or use "
              f"2-way (holdout_cutoff_date=null).")
        sys.exit(2)

    if backtest_months < 2 * holdout_months:
        print(f"    [WARN] thin pre window: {mode} {wname} = {backtest_months:.1f}mo vs "
              f"holdout {holdout_months}mo — 'pre' will be small.")

    print(f"    [OK] {mode} split: {wname} backtest window = {backtest_months:.1f}mo "
          f"({window_start.date()} .. {data_end.date()}) > holdout {holdout_months}mo — no collapse.")
    sys.exit(0)


if __name__ == "__main__":
    main()
