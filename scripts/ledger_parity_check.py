#!/usr/bin/env python
"""
Ledger-level backtest-vs-livetest parity check.

This is the *ledger reconciliation* layer of parity validation — distinct from the
offline unit suite in `/validate-parity`. It answers: does the production LiveTrader
(driven by the livetest harness) produce the SAME trade ledger as agent/backtest_engine.py
from identical inputs, trade-by-trade?

Two independent implementations are compared:
  * BacktestEngine (agent/backtest_engine.py)                      -> reference ledger
  * production LiveTrader via scripts/livetest_engine.py (harness) -> livetest ledger

The harness runs in **Parity Mode** (`--predictions-dir`): backtest predictions are
injected so entry SIGNALS are identical by construction, isolating the matching/exit
engine from model inference.

Flow (the livetest run happens BETWEEN the two subcommands, because it is a heavy,
~15-minute separate process):

    1. python scripts/ledger_parity_check.py setup      ...   # build subset + patched config
    2. python scripts/livetest_engine.py --config <patched> --data <subset> \
             --warmup-bars <W> --predictions-dir . --output <livetest.csv>   # run harness
    3. python scripts/ledger_parity_check.py reconcile   ...  # run backtest + compare ledgers

See .agents/workflows/validate-ledger-parity.md for the full runbook, expected
results, and pitfalls. Run everything in the `trader` conda env:
    conda run -n trader python scripts/ledger_parity_check.py <cmd> ...
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]

# Backtest ExitReason -> livetest exit_reason mapping (see validate-ledger-parity.md
# Step 3). Trades that differ ONLY by this mapping with identical PnL are IN parity.
EXIT_REASON_MAP = {
    "SL": "SL_HIT",
    "TP": "TP_HIT",
    "TRAILING_BE": "SL_HIT",   # backtest labels a trailed stop-out TRAILING_BE; live sees SL_HIT
    "TIME_BARRIER": "TIME_BARRIER",
    "SIGNAL_EXIT": "SIGNAL_EXIT",
}


def _bootstrap_repo() -> None:
    """Put the repo on sys.path and cwd there so repo-relative config/prediction
    paths resolve identically for both the backtest and the livetest harness."""
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    os.chdir(REPO)


def _to_dt_indexed(df: pd.DataFrame) -> pd.DataFrame:
    """Return df indexed by a DatetimeIndex, discovering the datetime column if needed."""
    if isinstance(df.index, pd.DatetimeIndex):
        return df
    for col in ("DateTime", "datetime", "date", "timestamp"):
        if col in df.columns:
            return df.set_index(pd.to_datetime(df[col]), drop=False)
    raise ValueError(
        "Could not find a datetime index/column (looked for DateTime/datetime/date/timestamp)."
    )


# --------------------------------------------------------------------------- setup
def cmd_setup(args: argparse.Namespace) -> int:
    """Build the short data subset and the patched parity config."""
    _bootstrap_repo()
    from agent.backtest_engine import load_predictions

    out_dir = Path(args.work_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Predictions coverage end -> anchor the replay window there so the injected
    # predictions fully cover the replayed bars.
    preds = load_predictions(args.predictions)
    pred_end = pd.Timestamp(args.pred_end) if args.pred_end else preds.index.max()

    # Short subset: warmup bars (for feature/ATR warmup) + replay bars.
    df = _to_dt_indexed(pd.read_parquet(args.data))
    df = df[df.index <= pred_end]
    total = args.warmup_bars + args.replay_bars
    if len(df) < total:
        raise ValueError(
            f"Source data has only {len(df)} bars <= {pred_end}, need {total} "
            f"(warmup {args.warmup_bars} + replay {args.replay_bars})."
        )
    subset = df.iloc[-total:]
    subset_path = out_dir / "livetest_subset.parquet"
    subset.to_parquet(subset_path)
    replay_start = subset.index[-args.replay_bars]

    # Patched config: local model pkls (needed ONLY for feature_names at init — inference
    # is bypassed in parity mode) + a full repo-relative predictions_path so BOTH the
    # livetest (--predictions-dir .) and the backtest resolve the same file.
    for p in (args.long_model, args.short_model, args.predictions):
        if not Path(p).exists():
            raise FileNotFoundError(
                f"Required parity input does not exist: {p}\n"
                "  - models: any locally-available pkls with the SAME feature schema "
                "(inference is bypassed in parity mode; only feature_names is read).\n"
                "  - predictions: the injected backtest predictions CSV."
            )
    with open(args.config) as fh:
        cfg = json.load(fh)
    cfg["models"]["long"]["model_path"] = args.long_model
    cfg["models"]["long"]["predictions_path"] = args.predictions
    cfg["models"]["short"]["model_path"] = args.short_model
    cfg["models"]["short"]["predictions_path"] = args.predictions
    cfg_path = out_dir / "parity_config.json"
    with open(cfg_path, "w") as fh:
        json.dump(cfg, fh, indent=2)

    # Persist the run parameters so `reconcile` reuses identical values.
    meta = {
        "subset_path": str(subset_path),
        "config_path": str(cfg_path),
        "predictions": args.predictions,
        "warmup_bars": args.warmup_bars,
        "replay_bars": args.replay_bars,
        "replay_start": str(replay_start),
        "commission_per_side": args.commission_per_side,
        "slippage_per_side": args.slippage_per_side,
        "contract_multiplier": args.contract_multiplier,
    }
    (out_dir / "parity_meta.json").write_text(json.dumps(meta, indent=2))

    livetest_out = out_dir / "livetest_ledger.csv"
    print(f"[setup] subset      : {subset_path}  ({len(subset)} bars, "
          f"{subset.index[0]} -> {subset.index[-1]})")
    print(f"[setup] replay start: {replay_start}  ({args.replay_bars} bars)")
    print(f"[setup] config      : {cfg_path}")
    print(f"[setup] meta        : {out_dir / 'parity_meta.json'}")
    print("\n[setup] NEXT — run the livetest harness (long-running; use background):")
    print(
        f"  conda run -n trader python scripts/livetest_engine.py \\\n"
        f"    --config \"{cfg_path}\" \\\n"
        f"    --data \"{subset_path}\" \\\n"
        f"    --warmup-bars {args.warmup_bars} \\\n"
        f"    --predictions-dir . \\\n"
        f"    --output \"{livetest_out}\""
    )
    print("\n[setup] THEN reconcile:")
    print(
        f"  conda run -n trader python scripts/ledger_parity_check.py reconcile \\\n"
        f"    --work-dir \"{out_dir}\" --livetest \"{livetest_out}\""
    )
    return 0


# ----------------------------------------------------------------------- reconcile
def cmd_reconcile(args: argparse.Namespace) -> int:
    """Run the backtest over the subset and reconcile it against the livetest ledger.

    Exit code is 0 when parity holds within tolerance, 1 when any violation is found,
    so this doubles as a CI/regression gate.
    """
    _bootstrap_repo()
    from agent.backtest_engine import (
        BacktestEngine,
        load_ohlcv_dual,
        load_predictions,
        _resolve_prob_column,
    )
    from src.live_execution.config_loader import load_strategy_config

    out_dir = Path(args.work_dir)
    meta = json.loads((out_dir / "parity_meta.json").read_text())
    replay_start = pd.Timestamp(meta["replay_start"])

    # --- Backtest reference ledger over the identical subset -----------------
    cfg = load_strategy_config(meta["config_path"])
    bt = BacktestEngine.from_config(
        cfg,
        commission_per_side=meta["commission_per_side"],
        slippage_per_side=meta["slippage_per_side"],
        contract_multiplier=meta["contract_multiplier"],
    )
    ohlcv_a, ohlcv_exec_a = load_ohlcv_dual(meta["subset_path"])

    ldf = load_predictions(meta["predictions"])
    sdf = load_predictions(meta["predictions"])
    lc = _resolve_prob_column(ldf, "buy")
    sc = _resolve_prob_column(sdf, "sell")
    preds = (
        ldf[[lc]].rename(columns={lc: "prob_Buy"})
        .join(sdf[[sc]].rename(columns={sc: "prob_Sell"}), how="outer")
        .fillna(0.0)
    )
    result = bt.run(preds, ohlcv_a, ohlcv_exec_df=ohlcv_exec_a, label="parity")
    bt_led = result.to_dataframe()
    bt_led["entry_time"] = pd.to_datetime(bt_led["entry_time"])
    bt = bt_led[bt_led["entry_time"] >= replay_start].reset_index(drop=True)
    bt_path = out_dir / "backtest_ledger.csv"
    bt.to_csv(bt_path, index=False)

    # --- Livetest ledger -----------------------------------------------------
    lt = pd.read_csv(args.livetest)
    lt["entry_time"] = pd.to_datetime(lt["entry_time"])

    # --- Reconcile (merge on entry_time; verify side + exit mapping on matches) --
    m = bt.merge(lt, on="entry_time", suffixes=("_bt", "_lt"), how="outer", indicator=True)
    matched = m[m["_merge"] == "both"].copy()
    bt_only = int((m["_merge"] == "left_only").sum())
    lt_only = int((m["_merge"] == "right_only").sum())

    matched["fill_delta"] = (matched["entry_fill_bt"] - matched["entry_fill_lt"]).abs()
    matched["pnl_delta"] = (matched["net_pnl_dollars_bt"] - matched["net_pnl_dollars_lt"]).abs()
    matched["side_ok"] = matched["signal_side_bt"] == matched["signal_side_lt"]
    matched["exit_ok"] = matched.apply(
        lambda r: EXIT_REASON_MAP.get(r["exit_reason_bt"], r["exit_reason_bt"]) == r["exit_reason_lt"],
        axis=1,
    )

    exact = int((matched["pnl_delta"] < 0.005).sum())
    viol = matched[
        (matched["pnl_delta"] > args.pnl_tolerance)
        | (~matched["side_ok"])
        | (~matched["exit_ok"])
        | (matched["fill_delta"] > args.fill_tolerance)
    ]

    print(f"trades: backtest={len(bt)}  livetest={len(lt)}  matched={len(matched)}")
    print(f"unmatched: bt_only={bt_only}  lt_only={lt_only}")
    print(f"exact-cent matches: {exact}/{len(matched)}")
    print(f"side match:  {matched['side_ok'].all()}  ({matched['side_ok'].sum()}/{len(matched)})")
    print(f"exit mapping match: {matched['exit_ok'].all()}  ({matched['exit_ok'].sum()}/{len(matched)})")
    print(f"max entry_fill delta: ${matched['fill_delta'].max():.4f}  (tol ${args.fill_tolerance})")
    print(f"max per-trade PnL delta: ${matched['pnl_delta'].max():.2f}  (tol ${args.pnl_tolerance:.2f})")
    print(
        f"total PnL: backtest=${bt['net_pnl_dollars'].sum():,.2f}  "
        f"livetest=${lt['net_pnl_dollars'].sum():,.2f}  "
        f"delta=${abs(bt['net_pnl_dollars'].sum() - lt['net_pnl_dollars'].sum()):,.2f}"
    )
    print(f"backtest ledger -> {bt_path}")

    total_violations = len(viol) + bt_only + lt_only
    if len(viol):
        print(f"\nPER-TRADE VIOLATIONS (>{args.pnl_tolerance} or side/exit/fill mismatch): {len(viol)}")
        print(
            viol[["entry_time", "signal_side_bt", "exit_reason_bt", "exit_reason_lt",
                  "fill_delta", "pnl_delta"]].to_string(index=False)
        )
    if bt_only or lt_only:
        print(f"\nUNMATCHED trades (trade-count divergence): bt_only={bt_only} lt_only={lt_only}")

    if total_violations == 0:
        print("\nPARITY: PASS ✅")
        return 0
    print(f"\nPARITY: FAIL ❌  ({total_violations} issue(s) — see known-open list in "
          "validate-ledger-parity.md before flagging as a NEW regression)")
    return 1


def main() -> int:
    p = argparse.ArgumentParser(description="Ledger-level backtest-vs-livetest parity check.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("setup", help="Build the data subset + patched parity config.")
    s.add_argument("--config", required=True, help="Source ensemble strategy config JSON to patch.")
    s.add_argument("--data", required=True, help="Source processed data parquet (e.g. CL_HourSet_14B).")
    s.add_argument("--predictions", required=True, help="Injected backtest predictions CSV.")
    s.add_argument("--long-model", required=True, help="Local long-side model pkl (feature schema only).")
    s.add_argument("--short-model", required=True, help="Local short-side model pkl (feature schema only).")
    s.add_argument("--warmup-bars", type=int, default=2200, help="Warmup bars (default 2200).")
    s.add_argument("--replay-bars", type=int, default=336, help="Replay bars (~2 weeks 1h; default 336).")
    s.add_argument("--pred-end", default=None, help="Replay anchor end (default = predictions coverage end).")
    s.add_argument("--commission-per-side", type=float, default=2.5)
    s.add_argument("--slippage-per-side", type=float, default=0.01)
    s.add_argument("--contract-multiplier", type=float, default=1000.0)
    s.add_argument("--work-dir", default=str(REPO / "reports" / "_ledger_parity"),
                   help="Working dir for subset/config/ledgers (default reports/_ledger_parity).")
    s.set_defaults(func=cmd_setup)

    r = sub.add_parser("reconcile", help="Run the backtest + reconcile against the livetest ledger.")
    r.add_argument("--work-dir", default=str(REPO / "reports" / "_ledger_parity"))
    r.add_argument("--livetest", required=True, help="Livetest ledger CSV from scripts/livetest_engine.py.")
    r.add_argument("--pnl-tolerance", type=float, default=5.0, help="Per-trade PnL tolerance $ (default 5).")
    r.add_argument("--fill-tolerance", type=float, default=0.011, help="Entry-fill tolerance (default 0.011).")
    r.set_defaults(func=cmd_reconcile)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
