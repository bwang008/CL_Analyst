#!/usr/bin/env python
"""
build_screen_config.py — Stage-1 TARGET SCREEN config generator.

Emits a VALIDATED MasterConfig-shaped screen config
(``training_workflow.mode = "screen"``) from EITHER a v2 batch manifest OR a
dataset, so the /cloud-target-batch workflow is turnkey for any symbol. The
output is identical in shape to ``configs/sweeps/screen_si_hourset01b.json`` and
is fed to ``gcp/vm_e2e_pipeline.py --mode screen``.

Exactly one source (mutually exclusive, required):

  --from-manifest <path>   A v2 ``BatchSweepConfig`` JSON. Reads
                           ``baseline.symbol``,
                           ``baseline.data_workflow.dataset_version``,
                           ``baseline.training_workflow.train_cutoff_date``, and
                           the deduped, first-seen-order union of every
                           ``experiments[].overrides.training_workflow.target_columns``
                           (screens exactly what that batch intended).

  --from-dataset <ver>     Requires ``--symbol``. Resolves the parquet via
                           ``get_data_root()/processed/<symbol>_<ver>.parquet``
                           (or ``<ver>.parquet`` if ``<ver>`` already starts with
                           the symbol — mirrors ``vm_e2e_pipeline.main()``). Reads
                           the columns and collects all ``TARGET_TRIPLE_*`` ending
                           in ``_LONG``/``_SHORT`` (``_MULTI`` excluded).

The generated config is VALIDATED with ``MasterConfig(**cfg)`` BEFORE writing. If
validation fails or zero targets are found, the tool prints the error and exits
non-zero WITHOUT writing anything (fail loud, no half-written config).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Ensure we can import from src
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ---------------------------------------------------------------------------
# Target discovery helpers
# ---------------------------------------------------------------------------

def _is_screen_target(col: str) -> bool:
    """True for TARGET_TRIPLE_* columns ending in _LONG/_SHORT (exclude _MULTI)."""
    if not col.startswith("TARGET_TRIPLE_"):
        return False
    return col.endswith("_LONG") or col.endswith("_SHORT")


def _dedup_preserve_order(items):
    """Return a list with duplicates removed, first-seen order preserved."""
    seen = set()
    out = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _resolve_dataset_path(symbol: str, dataset_version: str) -> Path:
    """Resolve the processed parquet path.

    Mirrors ``gcp/vm_e2e_pipeline.main()``: ``<version>.parquet`` if the version
    already starts with the symbol (case-insensitive), else
    ``<symbol>_<version>.parquet`` — under ``get_data_root()/processed``.
    """
    from src.data_paths import get_data_root

    if dataset_version.upper().startswith(symbol.upper()):
        dataset_name = f"{dataset_version}.parquet"
    else:
        dataset_name = f"{symbol}_{dataset_version}.parquet"
    return get_data_root() / "processed" / dataset_name


def _targets_from_dataset(symbol: str, dataset_version: str) -> list:
    """Read parquet columns and collect the LONG/SHORT triple targets."""
    import pandas as pd

    path = _resolve_dataset_path(symbol, dataset_version)
    if not path.exists():
        raise FileNotFoundError(
            f"Dataset not found at derived path: {path}. Expected a processed "
            f"parquet for symbol={symbol!r}, dataset_version={dataset_version!r}."
        )
    # Read only the schema (columns) — no need to load the data.
    try:
        import pyarrow.parquet as pq

        columns = list(pq.ParquetFile(str(path)).schema.names)
    except Exception:
        columns = list(pd.read_parquet(path).columns)

    targets = [c for c in columns if _is_screen_target(c)]
    return _dedup_preserve_order(targets)


def _targets_from_manifest(manifest: dict) -> list:
    """Deduped, order-preserved union of every experiment's target_columns."""
    union = []
    for exp in manifest.get("experiments", []) or []:
        overrides = (exp or {}).get("overrides", {}) or {}
        tw = overrides.get("training_workflow", {}) or {}
        for col in tw.get("target_columns", []) or []:
            union.append(col)
    return _dedup_preserve_order(union)


# ---------------------------------------------------------------------------
# Config construction + validation
# ---------------------------------------------------------------------------

def _derive_gcs_base_dir(symbol: str, dataset_version: str) -> str:
    """Derive a sensible gcs_base_dir for a screen run.

    e.g. symbol=SI, dataset_version=HourSet_01B -> screens/si_01b
    """
    ds = dataset_version.lower()
    # Strip a leading "<symbol>_" if the version was symbol-prefixed.
    sym_prefix = f"{symbol.lower()}_"
    if ds.startswith(sym_prefix):
        ds = ds[len(sym_prefix):]
    # Compact common "hourset_" prefixes to a short tag (hourset_01b -> 01b).
    tag = ds.replace("hourset_", "").replace("hourset", "")
    tag = tag.strip("_") or ds
    return f"gs://cltrainer-optuna-results/screens/{symbol.lower()}_{tag}"


def build_config(
    *,
    symbol: str,
    dataset_version: str,
    train_cutoff_date: str,
    holdout_cutoff_date,
    random_seed: int,
    target_columns: list,
) -> dict:
    """Build a MasterConfig-shaped screen config dict and VALIDATE it.

    Raises if the config is invalid (including empty target_columns) — the caller
    must not write on failure.
    """
    if not target_columns:
        raise ValueError(
            "No screen targets found — refusing to build a config with an empty "
            "target_columns list (nothing to screen)."
        )

    cfg = {
        "symbol": symbol,
        "data_workflow": {
            "dataset_version": dataset_version,
            "resolution": "1h",
            "targets": {
                "raw_horizon": 120,
                "atr_period": 14,
                "definitions": [
                    {
                        "type": "triple_barrier_grid",
                        "tp_multipliers": [2.0, 3.0, 4.0],
                        "sl_multiplier": 1.0,
                        "horizons": [3, 6, 24, 36],
                    }
                ],
            },
        },
        "training_workflow": {
            "mode": "screen",
            "train_cutoff_date": train_cutoff_date,
            "holdout_cutoff_date": holdout_cutoff_date,
            "random_seed": random_seed,
            "gcs_base_dir": _derive_gcs_base_dir(symbol, dataset_version),
            "target_columns": list(target_columns),
            "optuna": {"post_optimizer_holdout_months": 6},
        },
    }

    # VALIDATE before writing (fail loud). No execution_workflow in screen mode.
    from src.config.schemas import MasterConfig

    MasterConfig(**cfg)
    return cfg


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="build_screen_config.py",
        description=(
            "Emit a validated Stage-1 target-screen MasterConfig "
            "(training_workflow.mode='screen') from a v2 batch manifest OR a dataset."
        ),
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--from-manifest",
        metavar="PATH",
        help="Path to a v2 BatchSweepConfig JSON. Screens the deduped union of "
             "every experiment's target_columns.",
    )
    src.add_argument(
        "--from-dataset",
        metavar="DATASET_VERSION",
        help="Dataset version (requires --symbol). Screens all TARGET_TRIPLE_*"
             "_LONG/_SHORT columns present in the parquet.",
    )
    parser.add_argument(
        "--symbol",
        default=None,
        help="Instrument symbol. Required for --from-dataset; for --from-manifest "
             "it is taken from the baseline (must not conflict if provided).",
    )
    parser.add_argument(
        "--train-cutoff-date",
        default=None,
        help="Optional train_cutoff_date override. Required for --from-dataset "
             "(no silent default).",
    )
    parser.add_argument(
        "--holdout-cutoff-date",
        default=None,
        help="Optional holdout_cutoff_date (default null -> 2-way split).",
    )
    parser.add_argument(
        "--random-seed",
        type=int,
        default=42,
        help="Screen random seed (default: 42).",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Output path (default: configs/sweeps/screen_<symbol>_<dataset>.json).",
    )
    return parser, parser.parse_args(argv)


def _default_out_path(symbol: str, dataset_version: str) -> str:
    ds = dataset_version.lower()
    sym_prefix = f"{symbol.lower()}_"
    if ds.startswith(sym_prefix):
        ds = ds[len(sym_prefix):]
    return os.path.join(
        "configs", "sweeps", f"screen_{symbol.lower()}_{ds}.json"
    )


def main(argv=None) -> int:
    parser, args = _parse_args(argv)

    # ---- Resolve source -> (symbol, dataset_version, train_cutoff, targets) ----
    if args.from_manifest:
        manifest_path = Path(args.from_manifest)
        if not manifest_path.exists():
            print(f"ERROR: manifest not found: {manifest_path}", file=sys.stderr)
            return 2
        with open(manifest_path, "r") as f:
            manifest = json.load(f)

        baseline = manifest.get("baseline", {}) or {}
        symbol = baseline.get("symbol")
        if not symbol:
            print("ERROR: manifest baseline.symbol is missing.", file=sys.stderr)
            return 2
        if args.symbol is not None and args.symbol != symbol:
            print(
                f"ERROR: --symbol {args.symbol!r} conflicts with manifest "
                f"baseline.symbol {symbol!r}. For --from-manifest the symbol is "
                f"taken from the manifest; remove --symbol or make it match.",
                file=sys.stderr,
            )
            return 2

        dw = baseline.get("data_workflow", {}) or {}
        dataset_version = dw.get("dataset_version")
        if not dataset_version:
            print(
                "ERROR: manifest baseline.data_workflow.dataset_version is missing.",
                file=sys.stderr,
            )
            return 2

        tw = baseline.get("training_workflow", {}) or {}
        train_cutoff_date = args.train_cutoff_date or tw.get("train_cutoff_date")
        if not train_cutoff_date:
            print(
                "ERROR: no train_cutoff_date available (not in baseline and not "
                "provided via --train-cutoff-date).",
                file=sys.stderr,
            )
            return 2

        try:
            targets = _targets_from_manifest(manifest)
        except Exception as e:
            print(f"ERROR reading manifest targets: {e}", file=sys.stderr)
            return 2
        source_desc = f"manifest {manifest_path}"

    else:  # --from-dataset
        if not args.symbol:
            parser.error("--from-dataset requires --symbol")
        symbol = args.symbol
        dataset_version = args.from_dataset
        train_cutoff_date = args.train_cutoff_date
        if not train_cutoff_date:
            print(
                "ERROR: --from-dataset requires --train-cutoff-date "
                "(no silent default).",
                file=sys.stderr,
            )
            return 2
        try:
            targets = _targets_from_dataset(symbol, dataset_version)
        except FileNotFoundError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        except Exception as e:
            print(f"ERROR reading dataset columns: {e}", file=sys.stderr)
            return 2
        source_desc = f"dataset {symbol} {dataset_version}"

    # ---- Build + VALIDATE (fail loud, write nothing on failure) ----
    try:
        cfg = build_config(
            symbol=symbol,
            dataset_version=dataset_version,
            train_cutoff_date=train_cutoff_date,
            holdout_cutoff_date=args.holdout_cutoff_date,
            random_seed=args.random_seed,
            target_columns=targets,
        )
    except Exception as e:
        print(
            f"ERROR: config validation failed — writing nothing.\n{e}",
            file=sys.stderr,
        )
        return 1

    # ---- Write (only after successful validation) ----
    out_path = args.out or _default_out_path(symbol, dataset_version)
    out_path = str(out_path)
    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")

    print(
        f"Wrote screen config: {out_path}\n"
        f"  source:  {source_desc}\n"
        f"  symbol:  {symbol}\n"
        f"  dataset: {dataset_version}\n"
        f"  targets: {len(targets)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
