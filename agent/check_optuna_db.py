"""
check_optuna_db.py — Check progress of an Optuna study.

Usage:
    python agent/check_optuna_db.py models/optuna_studies/wf_v2_long_logloss_set08.db
    python agent/check_optuna_db.py models/optuna_studies/long_logloss_100t.db
"""

from __future__ import annotations

import argparse
import sys

import optuna

optuna.logging.set_verbosity(optuna.logging.WARNING)


def main() -> None:
    parser = argparse.ArgumentParser(description="Check Optuna study progress")
    parser.add_argument("db_path", help="Path to SQLite study DB")
    parser.add_argument("--study-name", default=None, help="Study name (auto-detect if omitted)")
    args = parser.parse_args()

    storage = f"sqlite:///{args.db_path}"

    # Auto-detect study name if not provided
    if args.study_name:
        study_name = args.study_name
    else:
        summaries = optuna.study.get_all_study_summaries(storage=storage)
        if not summaries:
            print(f"No studies found in {args.db_path}")
            sys.exit(1)
        study_name = summaries[0].study_name
        if len(summaries) > 1:
            print(f"Multiple studies found, using first: {study_name}")

    study = optuna.load_study(study_name=study_name, storage=storage)
    trials = study.trials

    complete = [t for t in trials if t.state.name == "COMPLETE"]
    running = [t for t in trials if t.state.name == "RUNNING"]
    failed = [t for t in trials if t.state.name == "FAIL"]
    pruned = [t for t in trials if t.state.name == "PRUNED"]

    print(f"Study: {study_name}")
    print(f"  Complete: {len(complete)}  |  Running: {len(running)}  |  Failed: {len(failed)}  |  Pruned: {len(pruned)}")

    if complete:
        durs = [t.duration.total_seconds() for t in complete]
        avg_dur = sum(durs) / len(durs)
        print(f"  Avg trial: {avg_dur:.0f}s ({avg_dur/60:.1f} min)")
        remaining = max(0, 100 - len(complete) - len(running))
        if remaining > 0:
            est_hours = (remaining * avg_dur) / 3600
            # Adjust for parallelism (count running workers)
            n_workers = max(1, len(running))
            est_hours_parallel = est_hours / n_workers
            print(f"  Est. remaining: ~{est_hours_parallel:.1f} hours ({remaining} trials, {n_workers} workers)")

        best = study.best_trial
        print(f"\n  Best trial: #{best.number}")
        print(f"  Best value: {best.value:.6f}")
        print(f"  Best params:")
        for k, v in best.params.items():
            if isinstance(v, float):
                print(f"    {k}: {v:.6f}")
            else:
                print(f"    {k}: {v}")
    else:
        print("  No completed trials yet — check back soon.")


if __name__ == "__main__":
    main()
