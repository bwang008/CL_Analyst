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

BLOCK_OBJECTIVE_METRICS = {"block_min", "block_median", "block_mean_std"}


def check_block_layout(window_start, window_end, holdout_months, objectives,
                       n_blocks, min_block_months):
    """Block-size gate for block-wise Sharpe objectives — pure function the
    CLI wraps.

    Passes (inert) when no block metric is present. When one is, the
    post-holdout in-sample window [window_start, window_end - holdout_months)
    must span at least n_blocks * min_block_months calendar months; the
    message reports the computed calendar block layout (months per block).

    Returns (ok: bool, message: str).
    """
    import pandas as pd

    block_arms = [o for o in objectives if o in BLOCK_OBJECTIVE_METRICS]
    if not block_arms:
        return True, "[OK] no block objective requested — block-layout gate inert."

    ws = pd.Timestamp(window_start)
    we = pd.Timestamp(window_end)
    # Mirror the post-optimizer carve: holdout = last N months, in-sample is
    # strictly before the cutoff (the -1s keeps an exact-boundary cutoff from
    # counting its own month).
    cutoff = we - pd.DateOffset(months=holdout_months)
    start_p = ws.to_period("M")
    end_p = (cutoff - pd.Timedelta(seconds=1)).to_period("M")
    insample_months = (end_p - start_p).n + 1
    required = n_blocks * min_block_months

    if insample_months < required:
        return False, (
            f"[FAIL] block objective(s) {block_arms}: in-sample window "
            f"{start_p} .. {end_p} = {insample_months} months after the "
            f"{holdout_months}-month holdout carve, but n_blocks={n_blocks} x "
            f"min_block_months={min_block_months} requires >= {required} months. "
            f"Widen the data window, lower the holdout, or drop the block arm."
        )

    periods = pd.period_range(start_p, end_p, freq="M")
    base, rem = divmod(insample_months, n_blocks)
    sizes = [base + 1 if i < rem else base for i in range(n_blocks)]
    bounds = []
    pos = 0
    for size in sizes:
        blk = periods[pos:pos + size]
        pos += size
        bounds.append(f"{blk[0]}..{blk[-1]}")
    layout = "/".join(str(s) for s in sizes)
    return True, (
        f"[OK] block layout for {block_arms}: in-sample {start_p} .. {end_p} = "
        f"{insample_months} months -> {layout} months per block "
        f"({'; '.join(bounds)})."
    )


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
    ap.add_argument("--objectives", default="sharpe",
                    help="Comma-separated objective arms; block metrics "
                         "(block_min/block_median/block_mean_std) enable the "
                         "block-layout gate (default: sharpe = gate inert)")
    ap.add_argument("--n-blocks", type=int, default=3,
                    help="Blocks for the block-layout gate (default: 3)")
    ap.add_argument("--min-block-months", type=int, default=10,
                    help="Minimum calendar months per block (default: 10)")
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

    # ── Block-layout gate (inert unless a block objective is requested) ──
    objectives = [o.strip() for o in args.objectives.split(",") if o.strip()]
    ok, msg = check_block_layout(
        window_start=window_start,
        window_end=data_end,
        holdout_months=holdout_months,
        objectives=objectives,
        n_blocks=args.n_blocks,
        min_block_months=args.min_block_months,
    )
    print(f"    {msg}")
    sys.exit(0 if ok else 2)


if __name__ == "__main__":
    main()
