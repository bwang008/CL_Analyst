#!/usr/bin/env python3
"""
compare_parity.py — Validate that a v2 batch run reproduces a reference run.

The reference (e.g. batch_20260626_0521_CANARY_V1) was produced by the
opt_mode=individual two-pass chain:
  pass 1: per-side individual optimization      -> batch_summary_optimized_<obj>.md
  select: unified_pair_optimizer (Top 4 pairs)  -> top_pairs.json
  pass 2: ensemble optimization on top pairs     -> batch_summary_optimized_ensembles_<obj>.md
                                                 -> <obj>_ensemble_backtests.md

This script does a STRUCTURAL + SANITY parity check (not bit-identical — the
sweep models differ run-to-run). It verifies:
  1. The correct artifact set exists (individual-mode files), and the
     divergent ensemble-mode files (batch_ensemble_pre_opt.md, top_8_ensembles.json)
     are ABSENT.
  2. The ensemble report selected the same NUMBER of ensembles (Top 4).
  3. The ensemble backtest report contains NO tracebacks/FileNotFound crashes.
  4. Slippage matches the reference (0.01).
  5. Headline economics are sane (no -$2.5M class blowups).

Usage:
  python scripts/compare_parity.py --run <batch_dir> [--ref <reference_batch_dir>]

Exit code 0 = parity PASS, 1 = parity FAIL.
"""
import argparse
import json
import os
import re
import sys

# Files the individual-mode (parity) chain MUST produce.
REQUIRED_INDIVIDUAL = [
    "batch_summary_optimized_sharpe.md",
    "batch_summary_optimized_sortino.md",
    "optimization_results_sharpe.json",
    "optimization_results_sortino.json",
    "batch_summary_optimized_ensembles_sharpe.md",
    "batch_summary_optimized_ensembles_sortino.md",
    "top_pairs.json",
    "sharpe_ensemble_backtests.md",
    "sortino_ensemble_backtests.md",
]

# Files that indicate the DIVERGENT ensemble-mode brute-force path was used.
FORBIDDEN_ENSEMBLE_MODE = [
    "batch_ensemble_pre_opt.md",
    "top_8_ensembles.json",
]


def _read(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def check(run_dir, ref_dir):
    failures = []
    warnings = []

    # 1. Artifact set --------------------------------------------------------
    for fn in REQUIRED_INDIVIDUAL:
        if not os.path.isfile(os.path.join(run_dir, fn)):
            failures.append(f"MISSING required parity artifact: {fn}")
    for fn in FORBIDDEN_ENSEMBLE_MODE:
        if os.path.isfile(os.path.join(run_dir, fn)):
            failures.append(
                f"PRESENT divergent ensemble-mode artifact: {fn} "
                f"(run used opt_mode=ensemble, not individual)"
            )

    # 2. Top-N ensembles -----------------------------------------------------
    tp = os.path.join(run_dir, "top_pairs.json")
    if os.path.isfile(tp):
        try:
            n = len(json.load(open(tp)))
            if n != 4:
                failures.append(f"top_pairs.json has {n} pairs (expected 4)")
        except Exception as e:
            failures.append(f"top_pairs.json unreadable: {e}")

    ens = os.path.join(run_dir, "batch_summary_optimized_ensembles_sharpe.md")
    if os.path.isfile(ens):
        m = re.search(r"Ensembles \(Top (\d+)\)", _read(ens))
        if m and int(m.group(1)) != 4:
            failures.append(f"ensemble report header says Top {m.group(1)} (expected Top 4)")

    # 3. Crashes in ensemble backtests --------------------------------------
    # FileNotFoundError / ModuleNotFoundError are SYSTEMIC (data/code missing ->
    # every backtest fails) and always hard-fail. Other tracebacks (e.g. the
    # benign holdout ZeroDivisionError that exists in CANARY_V1) are tolerated
    # up to the reference's own count.
    def _traceback_count(text):
        return text.count("Traceback (most recent call last)")

    for fn in ("sharpe_ensemble_backtests.md", "sortino_ensemble_backtests.md"):
        p = os.path.join(run_dir, fn)
        if not os.path.isfile(p):
            continue
        txt = _read(p)
        if "FileNotFoundError" in txt or "ModuleNotFoundError" in txt:
            failures.append(
                f"{fn} contains systemic FileNotFound/ModuleNotFound crashes "
                f"(data/code missing — backtests failed)"
            )
        run_tb = _traceback_count(txt)
        ref_p = os.path.join(ref_dir, fn) if ref_dir else None
        ref_tb = _traceback_count(_read(ref_p)) if ref_p and os.path.isfile(ref_p) else 0
        if run_tb > ref_tb:
            failures.append(
                f"{fn} has {run_tb} tracebacks (reference has {ref_tb}) — new crashes introduced"
            )
        elif run_tb:
            warnings.append(f"{fn} has {run_tb} traceback(s) (matches/under reference {ref_tb})")
        # 4. Slippage
        slip = re.search(r"Slippage[:\s]+([0-9.]+)", txt)
        if slip and abs(float(slip.group(1)) - 0.01) > 1e-9:
            failures.append(f"{fn} slippage={slip.group(1)} (expected 0.01)")

    # 5. Sane economics ------------------------------------------------------
    ores = os.path.join(run_dir, "optimization_results_ensembles_sharpe.json")
    if os.path.isfile(ores):
        try:
            data = json.load(open(ores))
            blob = json.dumps(data)
            for big in re.findall(r"-?\d{7,}\.?\d*", blob):
                if float(big) < -1_000_000:
                    failures.append(
                        f"catastrophic PnL detected ({big}) — -$2.5M slippage-bug class"
                    )
                    break
        except Exception as e:
            warnings.append(f"could not scan optimization_results_ensembles_sharpe.json: {e}")

    # Reference cross-check (informational) ---------------------------------
    if ref_dir and os.path.isdir(ref_dir):
        for fn in REQUIRED_INDIVIDUAL:
            r = os.path.join(ref_dir, fn)
            g = os.path.join(run_dir, fn)
            if os.path.isfile(r) and not os.path.isfile(g):
                warnings.append(f"ref has {fn} but run does not")

    return failures, warnings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True, help="batch_runs/<id> dir to validate")
    ap.add_argument(
        "--ref",
        default="reports/batch_runs/batch_20260626_0521_CANARY_V1",
        help="reference batch dir (default: CANARY_V1)",
    )
    args = ap.parse_args()

    if not os.path.isdir(args.run):
        print(f"ERROR: run dir not found: {args.run}")
        sys.exit(2)

    failures, warnings = check(args.run, args.ref)

    print("=" * 60)
    print(f" PARITY CHECK: {os.path.basename(args.run)}")
    print(f" vs reference: {os.path.basename(args.ref)}")
    print("=" * 60)
    for w in warnings:
        print(f"  [WARN] {w}")
    if failures:
        for f in failures:
            print(f"  [FAIL] {f}")
        print("\n  RESULT: PARITY FAIL")
        sys.exit(1)
    print("  All structural + sanity checks passed.")
    print("\n  RESULT: PARITY PASS")
    sys.exit(0)


if __name__ == "__main__":
    main()
