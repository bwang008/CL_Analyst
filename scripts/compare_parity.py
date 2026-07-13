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
  4. The reported slippage matches the run's OWN frozen manifest
     (baseline.execution_workflow.slippage_per_side) — symbol-specific
     (CL=0.01, ES=0.25, ...), so it is read from the manifest, not hardcoded.
     This still guards the -$2.5M class (config-vs-actual slippage divergence).
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
# NOTE: the Sortino objective was dropped from the post-optimizer chain on
# 2026-07-04 (ticket drop-sortino-objective_07042026_2301) — new runs are
# sharpe-only. Sortino artifacts are therefore OPTIONAL-LEGACY: absent on new
# runs (not a failure), but still fully content-checked when present on
# historical (pre-2026-07-04) run folders.
#
# SINCE 2026-07-12 the pass-2 joint ensemble re-optimization is OPT-IN
# (-EnsembleOptimization): the default chain is pass-1 -> pair selection ->
# BASELINE (pass-1 graft) artifacts. The baseline set is REQUIRED on new runs;
# the pass-2 ensemble set is OPTIONAL and all-or-none when any of it exists.
REQUIRED_INDIVIDUAL = [
    "batch_summary_optimized_sharpe.md",
    "optimization_results_sharpe.json",
    "top_pairs.json",
    "batch_summary_baseline_sharpe.md",
    "sharpe_baseline_backtests.md",
]

# Pass-2 (opt-in -EnsembleOptimization) artifacts: absent on default runs
# (expected); when ANY is present, ALL must be present.
OPTIONAL_ENSEMBLE_OPT = [
    "batch_summary_optimized_ensembles_sharpe.md",
    "optimization_results_ensembles_sharpe.json",
    "sharpe_ensemble_backtests.md",
]

# Sortino artifacts from the pre-2026-07-04 both-objectives chain. Optional:
# their absence is expected on sharpe-only runs and is NOT a parity failure.
OPTIONAL_LEGACY_SORTINO = [
    "batch_summary_optimized_sortino.md",
    "optimization_results_sortino.json",
    "batch_summary_optimized_ensembles_sortino.md",
    "optimization_results_ensembles_sortino.json",
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


def _expected_slippage(run_dir):
    """Per-side slippage this run SHOULD have used, from its frozen manifest.

    Slippage is symbol-specific (CL=0.01, ES=0.25, ...) so it must come from the
    run's own manifest, never a hardcode. Returns (value, error): on any missing
    manifest/field it returns (None, msg) and the caller hard-fails — we do NOT
    silently assume a default (per the no-silent-null-defaults doctrine).
    """
    mpath = os.path.join(run_dir, "manifest.json")
    if not os.path.isfile(mpath):
        return None, "manifest.json missing from run dir — cannot verify slippage"
    try:
        m = json.load(open(mpath))
    except (ValueError, OSError) as e:
        return None, f"manifest.json unreadable — cannot verify slippage ({e})"
    # v2 schema first, then the retired legacy `defaults` schema (CANARY_V1-era
    # frozen manifests). Both read the run's ACTUAL configured value, not a default.
    for getter in (
        lambda d: d["baseline"]["execution_workflow"]["slippage_per_side"],
        lambda d: d["defaults"]["slippage_per_side"],
    ):
        try:
            return float(getter(m)), None
        except (KeyError, TypeError, ValueError):
            continue
    return None, (
        "manifest.json has no numeric slippage_per_side "
        "(checked baseline.execution_workflow and legacy defaults) — cannot verify slippage"
    )


def check(run_dir, ref_dir):
    failures = []
    warnings = []

    exp_slippage, slip_err = _expected_slippage(run_dir)
    if slip_err:
        failures.append(slip_err)

    # 1. Artifact set --------------------------------------------------------
    for fn in REQUIRED_INDIVIDUAL:
        if not os.path.isfile(os.path.join(run_dir, fn)):
            failures.append(f"MISSING required parity artifact: {fn}")
    ens_present = [
        fn for fn in OPTIONAL_ENSEMBLE_OPT
        if os.path.isfile(os.path.join(run_dir, fn))
    ]
    if ens_present and len(ens_present) != len(OPTIONAL_ENSEMBLE_OPT):
        missing_ens = sorted(set(OPTIONAL_ENSEMBLE_OPT) - set(ens_present))
        failures.append(
            "pass-2 ensemble artifact set is PARTIAL (opt-in set is "
            f"all-or-none): present={ens_present} missing={missing_ens}"
        )
    elif ens_present:
        warnings.append(
            "pass-2 ensemble artifacts present (opt-in -EnsembleOptimization "
            "run) — content-checked"
        )
    for fn in FORBIDDEN_ENSEMBLE_MODE:
        if os.path.isfile(os.path.join(run_dir, fn)):
            failures.append(
                f"PRESENT divergent ensemble-mode artifact: {fn} "
                f"(run used opt_mode=ensemble, not individual)"
            )
    # Sortino artifacts are optional-legacy: absent on new sharpe-only runs
    # (expected, not a failure); when present on historical runs they are noted
    # and still content-checked below.
    legacy_present = [
        fn for fn in OPTIONAL_LEGACY_SORTINO
        if os.path.isfile(os.path.join(run_dir, fn))
    ]
    if legacy_present:
        warnings.append(
            f"legacy sortino artifacts present ({len(legacy_present)}) - "
            f"pre-2026-07-04 both-objectives run; content-checked, not required"
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

    for fn in ("sharpe_baseline_backtests.md", "sharpe_ensemble_backtests.md",
               "sortino_ensemble_backtests.md"):
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
        # 4. Slippage — must match the run's own manifest (symbol-specific)
        slip = re.search(r"Slippage[:\s]+([0-9.]+)", txt)
        if slip and exp_slippage is not None and abs(float(slip.group(1)) - exp_slippage) > 1e-9:
            failures.append(
                f"{fn} slippage={slip.group(1)} (manifest expects {exp_slippage})"
            )

    # 5. Sane economics ------------------------------------------------------
    for ores_name in ("optimization_results_sharpe.json",
                      "optimization_results_ensembles_sharpe.json"):
        ores = os.path.join(run_dir, ores_name)
        if not os.path.isfile(ores):
            continue
        try:
            data = json.load(open(ores))
            blob = json.dumps(data)
            for big in re.findall(r"-?\d{7,}\.?\d*", blob):
                if float(big) < -1_000_000:
                    failures.append(
                        f"catastrophic PnL detected in {ores_name} ({big}) — "
                        "-$2.5M slippage-bug class"
                    )
                    break
        except Exception as e:
            warnings.append(f"could not scan {ores_name}: {e}")

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
