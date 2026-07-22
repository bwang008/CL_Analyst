"""Compute weak-prob exit floors (mean - k*std) from model predictions.

Produces the per-side ``weak_prob_exit_threshold`` values required by
conflict_resolution='close_existing_position_if_own_weak'
(TieredEnsembleStrategy): the floor below which the own-side probability is
considered "collapsed" (default k=1.0 -> one standard deviation below the
mean of the model's OOS probability distribution).

Usage:
    # From a strategy config (resolves models.<side>.predictions_path):
    python scripts/compute_weak_prob_thresholds.py configs/strategies/SI01B_Sharpe_E02_07062026.json

    # Directly from a predictions CSV (DateTime, prob_Buy, prob_Sell):
    python scripts/compute_weak_prob_thresholds.py --csv reports/batch_runs/<batch>/predictions/<ens>_predictions.csv

    # Different multiplier:
    python scripts/compute_weak_prob_thresholds.py <config.json> --std-mult 1.5

Read-only: prints suggested config values; never modifies the config.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

_SIDE_COLUMNS = {"long": "prob_Buy", "short": "prob_Sell"}


def _resolve(path_str: str) -> Path:
    p = Path(path_str)
    return p if p.is_absolute() else _PROJECT_ROOT / p


def _load_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        sys.exit(f"ERROR: predictions CSV not found: {path}")
    df = pd.read_csv(path)
    missing = [c for c in _SIDE_COLUMNS.values() if c not in df.columns]
    if missing:
        sys.exit(
            f"ERROR: {path} is missing column(s) {missing}; "
            f"found {list(df.columns)}"
        )
    return df


def _side_stats(df: pd.DataFrame, column: str, std_mult: float) -> dict:
    series = df[column].dropna()
    mean = float(series.mean())
    std = float(series.std())
    floor = mean - std_mult * std
    return {
        "n": int(series.size),
        "mean": mean,
        "std": std,
        "floor": max(0.0, min(1.0, floor)),
        "raw_floor": floor,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compute weak_prob_exit_threshold floors (mean - k*std)."
    )
    parser.add_argument(
        "config", nargs="?", default=None,
        help="Strategy config JSON (reads models.<side>.predictions_path)",
    )
    parser.add_argument(
        "--csv", default=None,
        help="Predictions CSV (DateTime, prob_Buy, prob_Sell); overrides config paths",
    )
    parser.add_argument(
        "--std-mult", type=float, default=1.0,
        help="k in mean - k*std (default 1.0)",
    )
    args = parser.parse_args()

    if args.csv is None and args.config is None:
        parser.error("provide a strategy config JSON and/or --csv")

    # side -> csv path
    side_paths: dict[str, Path] = {}
    if args.csv is not None:
        csv_path = _resolve(args.csv)
        side_paths = {side: csv_path for side in _SIDE_COLUMNS}
    else:
        cfg_path = _resolve(args.config)
        if not cfg_path.exists():
            sys.exit(f"ERROR: config not found: {cfg_path}")
        cfg = json.loads(cfg_path.read_text())
        models = cfg.get("models", {})
        for side in _SIDE_COLUMNS:
            pred = (models.get(side) or {}).get("predictions_path")
            if pred:
                side_paths[side] = _resolve(pred)
        if not side_paths:
            sys.exit(
                "ERROR: config has no models.<side>.predictions_path entries; "
                "pass --csv instead"
            )

    cache: dict[Path, pd.DataFrame] = {}
    results: dict[str, dict] = {}
    for side, path in side_paths.items():
        if path not in cache:
            cache[path] = _load_csv(path)
        results[side] = _side_stats(cache[path], _SIDE_COLUMNS[side], args.std_mult)

    k = args.std_mult
    print(f"Weak-prob exit floors (floor = mean - {k:g}*std, clipped to [0,1])")
    print()
    print(f"{'side':<6} {'column':<10} {'n':>8} {'mean':>8} {'std':>8} {'floor':>8}")
    for side, st in results.items():
        clip = "" if st["floor"] == st["raw_floor"] else f"  (raw {st['raw_floor']:.4f}, clipped)"
        print(
            f"{side:<6} {_SIDE_COLUMNS[side]:<10} {st['n']:>8} "
            f"{st['mean']:>8.4f} {st['std']:>8.4f} {st['floor']:>8.4f}{clip}"
        )
    print()
    print("Suggested config additions:")
    print('    "conflict_resolution": "close_existing_position_if_own_weak",')
    for side, st in results.items():
        print(f'    "{side}": {{ ... "weak_prob_exit_threshold": {st["floor"]:.4f} }}')


if __name__ == "__main__":
    main()
