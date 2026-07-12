import os
import sys
import json
import argparse
import subprocess
import shutil
import re
from datetime import datetime
from pathlib import Path

# Ensure project root is on path for script-mode runs
# (python agent/generate_ensemble_artifacts.py from the repo root / VM).
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# T6 (t6-config-generator-fix_07052026_0043): shared tag helper + instrument
# validation. All three are stdlib-leaf-safe (no heavy deps), so the VM-side
# invocation is unaffected. Identity of derive_dataset_tag is test-pinned —
# do NOT re-implement the derivation inline.
from src.core.dataset_tag import derive_dataset_tag
from src.core.instrument_master import (
    default_slippage_points,
    dollars_per_point,
    get_instrument,
)
from src.live_execution.instrument_context import resolve_instrument_context

def parse_experiment_key(key, direction):
    """
    key format: sweep_hs13a_3x1_6h_scout_20260618_1721_E2E_HourSet_13A_long_average_precision
    returns: sweep_prefix, experiment_id, metric
    """
    marker = f"_{direction}_"
    if marker not in key:
        return "", "", ""
    parts = key.split(marker)
    metric = parts[1]
    rest = parts[0]
    
    if rest.startswith("oos_predictions_"):
        rest = rest[len("oos_predictions_"):]
        
    idx = rest.rfind("_E2E_")
    if idx == -1:
        return rest, "", metric
        
    sweep_prefix = rest[:idx]
    experiment_id = rest[idx+1:]
    return sweep_prefix, experiment_id, metric

def _layout_subdir(sweep: str) -> str:
    """Return the artifact-layout subdir for a sweep.

    v2 (E2E) pipelines emit artifacts under ``registry/production_output/``;
    legacy canary runs used ``registry/canary_output/``.  Prefer the v2 layout
    when present, falling back to the legacy name for backward compatibility.
    """
    prod = os.path.join("reports", sweep, "registry", "production_output")
    if os.path.isdir(prod):
        return "production_output"
    return "canary_output"

def _resolve_side_pred(sweep: str, layout_dir: str, side: str, metric: str, prefixed_key: str) -> str:
    """Resolve the on-disk per-side prediction CSV for a sweep, path-agnostically.

    Fix G: individual-mode sweeps emit a GENERIC filename
    ``oos_predictions_sweep_{side}_{metric}.csv`` (identical across every sweep).
    The unique ``oos_predictions_{sweep}_{side}_{metric}`` name is only a pred_map
    KEY, so it may not exist as a file. We look ONLY inside this sweep's own
    ``reports/<sweep>/registry/<layout>/`` directory, so we can never source a
    prediction file that belongs to a different sweep — regardless of OS/path style.

    Preference order (first existing wins):
      1. reports/<sweep>/registry/<layout>/<prefixed_key>.csv   (if materialized)
      2. reports/<sweep>/registry/<layout>/oos_predictions_sweep_{side}_{metric}.csv
      3. reports/<sweep>/registry/<layout>/oos_predictions_{side}_{metric}.csv (legacy)
    Falls back to the prefixed name (for the error/verification message) if none exist.
    """
    base = os.path.join("reports", sweep, "registry", layout_dir)
    candidates = [
        os.path.join(base, f"{prefixed_key}.csv"),
        os.path.join(base, f"oos_predictions_sweep_{side}_{metric}.csv"),
        os.path.join(base, f"oos_predictions_{side}_{metric}.csv"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c.replace("\\", "/")
    return candidates[0].replace("\\", "/")


_METRIC_RANK = {"logloss": 0, "average_precision": 1}


def _ensemble_sort_key(item):
    """Deterministic TOTAL ordering for ensemble pairs (fallback only).

    The previous sort key was built from the sweep prefix alone and dropped the
    metric. When every pair comes from a single sweep (canary), all keys tie and
    Python's stable sort falls back to opt_data insertion order — the
    ProcessPoolExecutor as_completed() order, which is non-deterministic. This
    key adds a fixed (long_metric, short_metric) tie-break — logloss < AP — so it
    never ties and matches the canonical top_pairs ordering.
    """
    pair_key, _ = item
    keys = pair_key.split("|")
    long_key = keys[0]
    short_key = keys[1] if len(keys) > 1 else ""
    long_sweep, _, long_metric = parse_experiment_key(long_key, "long")
    short_sweep, _, short_metric = parse_experiment_key(short_key, "short")
    exp_col = f"{long_sweep} / {short_sweep}" if long_sweep and short_sweep else (long_sweep or short_sweep or "-")
    natural = [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', exp_col)]
    return (natural,
            _METRIC_RANK.get(long_metric, 99),
            _METRIC_RANK.get(short_metric, 99),
            long_metric, short_metric)


def _canonical_pair_order(opt_data, batch_dir, objective="sharpe"):
    """Return (pair_key, pair_val) items in the arm's canonical top-pairs order.

    E-slot labels (E01..E04) are assigned by enumeration position, so the order
    MUST match the arm's pairs file — the single source of truth for which pair
    is ensemble N. Driving the order off ``sorted(opt_data.items(), ...)`` let
    the non-deterministic as_completed() insertion order leak into the E labels
    whenever the sort key tied (single-sweep canary), mislabeling the emitted
    predictions/configs/backtests run-to-run.

    Per-arm selection (unified_pair_optimizer --objectives) writes
    ``top_pairs.json`` for sharpe and ``top_pairs_<arm>.json`` for other arms,
    so each arm's file is tried first, then ``top_pairs.json`` (covers legacy
    both-objective runs, where sortino shared sharpe's selection).

    Falls back to the deterministic, metric-aware ``_ensemble_sort_key`` when
    no pairs file is readable. Any opt_data pair not declared in the pairs file
    is appended (never dropped) in deterministic order; any declared pair
    missing from opt_data is reported loudly.
    """
    candidates = ["top_pairs.json"] if objective == "sharpe" else [
        f"top_pairs_{objective}.json", "top_pairs.json",
    ]
    top_pairs = None
    tp_name = None
    for name in candidates:
        path = os.path.join(batch_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                top_pairs = json.load(f)
            tp_name = name
            break
        except Exception as e:
            print(f"  [WARN] {name} unreadable ({e}); trying next pairs source")

    if not top_pairs:
        print(f"  [WARN] no readable pairs file for objective '{objective}' "
              f"(tried: {', '.join(candidates)}); using deterministic sort fallback for E-order")
        return sorted(opt_data.items(), key=_ensemble_sort_key)

    ordered = []
    used = set()
    missing = []
    for p in top_pairs:
        pk = f"{p.get('target_long', '')}|{p.get('target_short', '')}"
        if pk in opt_data:
            ordered.append((pk, opt_data[pk]))
            used.add(pk)
        else:
            missing.append(pk)
    leftover = sorted(
        ((k, v) for k, v in opt_data.items() if k not in used),
        key=_ensemble_sort_key,
    )
    if missing:
        print(f"  [WARN] {len(missing)} {tp_name} pair(s) not found in opt_data (cannot emit): {missing}")
    if leftover:
        print(f"  [WARN] {len(leftover)} opt_data pair(s) not declared in {tp_name}; appending in deterministic order after declared pairs")
    return ordered + leftover


def resolve_exec_data_path(manifest, cli_exec_data):
    """Resolve the execution data path using a fallback chain.

    Priority order:
      1. cli_exec_data (if non-empty)
      2. manifest['defaults']['local_exec_data'] (if non-empty)
      3. manifest['baseline']['execution_workflow']['execution_data_path']
         - If GCS URI (gs://...): extract filename, build local path via
           get_data_root() / 'processed' / filename, validate existence.
         - If local path: return directly.
      4. Empty string (nothing found)
    """
    # 1. CLI takes precedence
    if cli_exec_data:
        return cli_exec_data

    # 2. Check defaults.local_exec_data
    local_exec = manifest.get("defaults", {}).get("local_exec_data", "")
    if local_exec:
        return local_exec

    # 3. Check baseline.execution_workflow.execution_data_path
    exec_path = (
        manifest.get("baseline", {})
        .get("execution_workflow", {})
        .get("execution_data_path", "")
    )
    if exec_path:
        if exec_path.startswith("gs://"):
            # Extract filename from GCS URI and build local path
            filename = exec_path.split("/")[-1]
            try:
                from src.data_paths import get_data_root
                local_path = str(get_data_root() / "processed" / filename)
            except Exception:
                # Fallback when CL_DATA_ROOT is not configured
                local_path = str(Path("data") / "processed" / filename)
            if not os.path.exists(local_path):
                raise FileNotFoundError(
                    f"Resolved local exec_data path {local_path} does not exist. "
                    f"Please download the dataset first."
                )
            return local_path
        else:
            return exec_path

    # 4. Nothing found
    return ""


def validate_exec_data(manifest, cli_exec_data, symbol):
    """Validate that an exec_data path can be resolved; raise FATAL if not.

    Calls resolve_exec_data_path() and raises ValueError with a clear
    diagnostic message when no path is available.
    """
    resolved = resolve_exec_data_path(manifest, cli_exec_data)
    if not resolved:
        raise ValueError(
            f"FATAL: No exec_data path resolved for symbol {symbol}. "
            f"Ensure manifest contains 'baseline.execution_workflow.execution_data_path' "
            f"or equivalent."
        )
    return resolved


def _guard_shipped_pair_config(batch_dir, objective, pair_key, base_config_path, out_path):
    """Reconstruct the SHIPPED config for a guard-kept pass-2 ensemble.

    A guard revert ships the pair's GRAFTED baseline (each leg's pass-1
    winner applied to its side — batch_post_optimizer._build_pair_base_config),
    NOT the raw base config. Emitting the raw base config for guard rows
    produced promotion configs and verification backtests that silently
    diverged from the summary report (03B E1: summary holdout +$25.2k vs
    backtest of the generic config +$5.8k — caught by the operator).

    Re-derives the graft from the same local inputs the optimizer VM used
    (pass-1 optimization_results_<arm>.json + batch_progress prefixes).
    Returns the config dict, or None when pass-1 results are unavailable
    (legacy / opt_mode=ensemble batches — where guard baseline == raw base
    config and the old behavior is exact by identity).
    """
    # Deferred import: batch_post_optimizer imports _canonical_pair_order from
    # THIS module — a module-level import here would be circular.
    from agent.batch_post_optimizer import (
        _build_pair_base_config,
        _build_gcs_prefix_to_label,
    )

    p1_path = os.path.join(batch_dir, f"optimization_results_{objective}.json")
    progress_path = os.path.join(batch_dir, "batch_progress.json")
    if not (os.path.exists(p1_path) and os.path.exists(progress_path)):
        return None
    keys = pair_key.split("|")
    if len(keys) != 2:
        return None
    with open(p1_path, encoding="utf-8-sig") as f:
        pass1_results = json.load(f)
    with open(progress_path, encoding="utf-8-sig") as f:
        progress = json.load(f)
    gcs_to_label = _build_gcs_prefix_to_label(progress)
    shipped_path = _build_pair_base_config(
        base_config_path, pass1_results, gcs_to_label, keys[0], keys[1], out_path
    )
    with open(shipped_path, encoding="utf-8-sig") as f:
        return json.load(f)


def build_config(opt_result, objective, ensemble_idx, batch_dir, date_str, dataset_tag, base_config):
    # opt_result key example: "sweep...|sweep..."
    keys = list(opt_result.keys())
    # The dictionary passed is the value for that pair. Wait, opt_result is a dict of {key: {status: ...}}
    # The caller will pass the item key and the item value
    pass

def main():
    parser = argparse.ArgumentParser(description="Generate ensemble backtest artifacts")
    parser.add_argument("--batch-dir", required=True, help="Path to batch directory")
    parser.add_argument("--data", default=None, help="Path to OHLCV parquet (overrides manifest local_data_path)")
    parser.add_argument("--exec-data", default="", help="Path to raw execution parquet")
    parser.add_argument(
        "--slippage-per-side", type=float, default=None,
        help="Slippage per side in price points. Default: the batch symbol's "
             "1-tick value from the instrument registry (CL resolves to 0.01, "
             "identical to the old hardcoded default).")
    parser.add_argument("--objectives", default="sharpe,sortino", help="Objectives to process")
    args = parser.parse_args()

    batch_dir = args.batch_dir
    if not os.path.isdir(batch_dir):
        print(f"Error: Batch dir {batch_dir} not found.")
        sys.exit(1)

    # Output dirs
    configs_dir = os.path.join(batch_dir, "configs")
    predictions_dir = os.path.join(batch_dir, "predictions")
    os.makedirs(configs_dir, exist_ok=True)
    os.makedirs(predictions_dir, exist_ok=True)

    # Load manifest
    manifest_path = os.path.join(batch_dir, "manifest.json")
    if not os.path.isfile(manifest_path):
        print(f"Error: Manifest {manifest_path} not found.")
        sys.exit(1)
    with open(manifest_path, "r") as f:
        manifest = json.load(f)

    # -------------------------------------------------------------------------
    # Resolve + validate the batch symbol (T6)
    # -------------------------------------------------------------------------
    # Every emitted strategy config is stamped from manifest.baseline.symbol
    # (execution_symbol + models.<side>.symbol). House rule: NO silent
    # defaults — a manifest without it is a hard batch failure, never a
    # fallback to CL.
    baseline_symbol = manifest.get("baseline", {}).get("symbol")
    if not baseline_symbol:
        raise ValueError(
            f"FATAL: manifest 'baseline.symbol' is missing or empty in "
            f"{manifest_path}. Every batch manifest must declare its "
            f"instrument symbol explicitly — refusing to default to CL."
        )
    # Fail-fast: unknown symbols raise ValueError('Unknown instrument symbol: ...')
    get_instrument(baseline_symbol)

    # Per-symbol economics: dollars-per-point always from the registry; slippage
    # defaults to the symbol's 1-tick value unless explicitly overridden.
    contract_multiplier = dollars_per_point(baseline_symbol)
    if args.slippage_per_side is None:
        args.slippage_per_side = default_slippage_points(baseline_symbol)
    print(f"Economics: symbol={baseline_symbol}  $/pt={contract_multiplier}  "
          f"slippage/side={args.slippage_per_side}")

    # -------------------------------------------------------------------------
    # Resolve Data Path (Manifest -> CLI Fallback)
    # -------------------------------------------------------------------------
    # CLI args.data takes precedence over manifest if explicitly provided
    data_path = args.data
    if not data_path:
        data_path = manifest.get("defaults", {}).get("local_data_path")
        
    if not data_path:
        raise ValueError(
            "FATAL: No local_data_path specified in manifest 'defaults', and no --data provided! "
            "The dataset must be explicitly defined in the manifest to prevent dataset mismatches."
        )
    args.data = data_path  # Override args.data so the rest of the script uses the resolved path

    # Attempt to resolve exec_data from manifest if not provided via CLI
    if not args.exec_data:
        args.exec_data = manifest.get("defaults", {}).get("local_exec_data", "")

    base_config_name = manifest.get("defaults", {}).get("strategy_config", "hourly_ensemble_010.json")
    base_config_path = os.path.join("configs", "strategies", base_config_name)
    if not os.path.isfile(base_config_path):
        print(f"Error: Base config {base_config_path} not found.")
        sys.exit(1)
    with open(base_config_path, "r") as f:
        base_config = json.load(f)

    # Parse date and tag from batch_dir
    batch_name = os.path.basename(os.path.normpath(batch_dir))
    m_date = re.search(r'_(\d{8})_', batch_name)
    if m_date:
        ymd = m_date.group(1)
        mmddyyyy = f"{ymd[4:6]}{ymd[6:8]}{ymd[0:4]}"
    else:
        mmddyyyy = "00000000"

    parts = batch_name.split('_')
    if len(parts) > 3:
        dataset_tag = parts[3]
    else:
        experiments = manifest.get("experiments", [])
        if experiments and "label" in experiments[0]:
            dataset_tag = experiments[0]["label"].split(" ")[0]
        else:
            dataset_tag = "TAG"

    objectives = [o.strip() for o in args.objectives.split(",")]

    for objective in objectives:
        opt_results_path = os.path.join(batch_dir, f"optimization_results_ensembles_{objective}.json")
        if not os.path.isfile(opt_results_path):
            print(f"Skipping {objective}, no results found.")
            continue

        with open(opt_results_path, "r") as f:
            opt_data = json.load(f)

        markdown_lines = []
        markdown_lines.append(f"# Ensemble Backtests ({objective.capitalize()} Optimized)")
        markdown_lines.append(f"**Batch**: {batch_name}")
        markdown_lines.append(f"**Generated**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        markdown_lines.append(f"**Data**: {args.data}")
        markdown_lines.append(f"**Exec Data**: {args.exec_data} | Slippage: {args.slippage_per_side}")
        markdown_lines.append("")
        markdown_lines.append("---")
        markdown_lines.append("")
        # E-slot order MUST follow the arm's own pairs file (the canonical
        # selection), not the non-deterministic opt_data insertion order.
        sorted_opt_data = _canonical_pair_order(opt_data, batch_dir, objective)

        # Derive e2e_dataset_tag via the SAME shared helper vm_e2e_pipeline
        # uses (src.core.dataset_tag) — alignment is true by construction (T6).
        data_basename = os.path.splitext(os.path.basename(args.data))[0]
        e2e_dataset_tag = derive_dataset_tag(data_basename, baseline_symbol)
        e2e_prefix = f"E2E_{e2e_dataset_tag}"

        for ensemble_idx, (pair_key, pair_val) in enumerate(sorted_opt_data, start=1):
            opt_info = pair_val.get("optuna_info", {})
            metrics = pair_val.get("metrics", {})
            
            # Parse pair key
            keys = pair_key.split("|")
            long_key = keys[0]
            short_key = keys[1] if len(keys) > 1 else ""
            
            long_sweep, _, long_metric = parse_experiment_key(long_key, "long")
            short_sweep, _, short_metric = parse_experiment_key(short_key, "short")

            # Always use the exact prefix generated by vm_e2e_pipeline.py
            long_exp = e2e_prefix
            short_exp = e2e_prefix

            # Mappings for description
            long_desc = f"{long_metric.replace('average_precision', 'AP').replace('logloss', 'LL').upper()}_LONG"
            short_desc = f"{short_metric.replace('average_precision', 'AP').replace('logloss', 'LL').upper()}_SHORT"

            config_name = f"{dataset_tag}_{objective.capitalize()}_E{ensemble_idx:02d}_{mmddyyyy}.json"
            nickname = f"{dataset_tag}_{objective.capitalize()}_E{ensemble_idx:02d}_{mmddyyyy}"
            predictions_name = f"{dataset_tag}_{objective.capitalize()}_E{ensemble_idx:02d}_predictions.csv"

            # Create config
            cfg = json.loads(json.dumps(base_config))  # deep copy
            # T6: stamp the batch symbol — in-place overwrite of the base
            # config's existing key (value-preserving for CL, the fix for
            # non-CL batches that used to leak the CL base's value).
            cfg["execution_symbol"] = baseline_symbol
            cfg["nickname"] = nickname
            cfg["description"] = f"{objective.capitalize()} Ensemble #{ensemble_idx}: {long_desc} + {short_desc}"
            # Holdout is manifest-authoritative (same precedence as vm_post_optimize.sh):
            # v2 keeps it under baseline.training_workflow.optuna; only legacy/non-CL
            # manifests carry a defaults block. No silent fallback — a wrong value here
            # mislabels the opt/holdout split in every *_ensemble_backtests.md.
            _holdout = (
                manifest.get("baseline", {}).get("training_workflow", {}).get("optuna", {}).get("post_optimizer_holdout_months")
                if manifest.get("baseline") is not None else None
            )
            if _holdout is None:
                _holdout = manifest.get("defaults", {}).get("post_optimizer_holdout_months")
            if _holdout is None:
                raise ValueError(
                    "post_optimizer_holdout_months not found in manifest "
                    "(baseline.training_workflow.optuna or defaults) — refusing to default."
                )
            cfg["holdout_months"] = _holdout
            
            regression_triggered = opt_info.get("regression_guard_triggered", False)
            if regression_triggered:
                cfg["conflict_resolution"] = "hold"
            else:
                cfg["conflict_resolution"] = opt_info.get("params", {}).get("conflict_resolution", "hold")

            # Update models block
            if "models" not in cfg:
                cfg["models"] = {"long": {}, "short": {}}

            # Merged predictions
            # Resolve artifact layout per sweep: v2 emits production_output/, legacy canary_output/
            long_dir = _layout_subdir(long_sweep)
            short_dir = _layout_subdir(short_sweep)
            long_oos_key = f"oos_predictions_{long_sweep}_long_{long_metric}"
            short_oos_key = f"oos_predictions_{short_sweep}_short_{short_metric}"
            merged_csv_path = os.path.join("reports", long_sweep, "registry", long_dir, f"_merged_ens_{long_oos_key}_vs_{short_oos_key}.csv")

            use_merged = os.path.isfile(merged_csv_path)
            predictions_dst = os.path.join(predictions_dir, predictions_name)

            if use_merged:
                shutil.copy2(merged_csv_path, predictions_dst)
                pred_path_workspace = os.path.join(args.batch_dir, "predictions", predictions_name).replace("\\", "/")
                pred_path_long = pred_path_workspace
                pred_path_short = pred_path_workspace
            else:
                # Fix G alignment: sweeps write GENERIC per-side filenames
                # (oos_predictions_sweep_{side}_{metric}.csv) into <sweep>/registry/<layout>/.
                # The unique <prefix>-keyed name only exists as a pred_map KEY, not on disk.
                # Resolve to the real file: prefer the prefixed name if it was materialized,
                # else fall back to the generic per-side name INSIDE THE SWEEP'S OWN dir
                # (path-agnostic — scoped to reports/<sweep>/, so no cross-sweep mis-source).
                pred_path_long = _resolve_side_pred(long_sweep, long_dir, "long", long_metric, long_oos_key)
                pred_path_short = _resolve_side_pred(short_sweep, short_dir, "short", short_metric, short_oos_key)

            cfg["models"]["long"]["experiment_id"] = f"{long_exp}_long_{long_metric}"
            cfg["models"]["long"]["model_path"] = f"reports/{long_sweep}/registry/{long_dir}/registry/{long_exp}_long_{long_metric}/final_model.pkl"
            cfg["models"]["long"]["predictions_path"] = pred_path_long

            cfg["models"]["short"]["experiment_id"] = f"{short_exp}_short_{short_metric}"
            cfg["models"]["short"]["model_path"] = f"reports/{short_sweep}/registry/{short_dir}/registry/{short_exp}_short_{short_metric}/final_model.pkl"
            cfg["models"]["short"]["predictions_path"] = pred_path_short

            # T6 (D2): explicit model<->symbol handshake on EVERY emitted
            # config. Post-T6 experiment_ids are symbol-stripped, so this
            # field is the only surviving model-symbol cross-check.
            cfg["models"]["long"]["symbol"] = baseline_symbol
            cfg["models"]["short"]["symbol"] = baseline_symbol

            if regression_triggered:
                # Guard revert: the SHIPPED config is the grafted pair
                # baseline, not the raw base config. Reconstruct it and graft
                # its side blocks; tiers are authoritative for thresholds.
                # Intermediate graft source goes in the batch ROOT (like
                # _merged_ens_*.csv) — configs/ holds only promotable,
                # symbol-stamped configs and is swept by the validation gate.
                shipped = _guard_shipped_pair_config(
                    batch_dir, objective, pair_key, base_config_path,
                    os.path.join(batch_dir, f"_guard_pair_src_{objective}_E{ensemble_idx:02d}.json"),
                )
                if shipped is not None:
                    for _side in ("long", "short"):
                        if shipped.get(_side):
                            cfg[_side] = json.loads(json.dumps(shipped[_side]))
                            _tiers = shipped[_side].get("tiers") or []
                            if _tiers and _tiers[0].get("min_prob") is not None:
                                cfg["models"][_side]["threshold"] = _tiers[0]["min_prob"]
                    print(f"  [GUARD-RECON] E{ensemble_idx:02d}: shipped grafted baseline reconstructed for {pair_key}")
                else:
                    print(f"  [GUARD-RECON] WARN E{ensemble_idx:02d}: pass-1 results unavailable — emitting raw base config (pre-graft identity)")

            if not regression_triggered:
                # Override long and short params
                long_params = opt_info.get("long_params", {})
                if long_params:
                    cfg["models"]["long"]["threshold"] = long_params.get("entry_threshold", cfg["models"]["long"].get("threshold", 0.5))
                    
                    for k, v in long_params.items():
                        if k != "entry_threshold":
                            cfg["long"][k] = v
                    cfg["long"]["tiered_exits"] = [{"qty_pct": 1.0, "tp_atr_mult": long_params.get("tp_atr_mult", 0)}]
                    cfg["long"]["tiers"] = [{
                        "min_prob": long_params.get("entry_threshold", 0),
                        "lots": 1,
                        "tp_atr_mult": long_params.get("tp_atr_mult", 0),
                        "sl_atr_mult": long_params.get("sl_atr_mult", 0),
                        "trailing_atr_mult": long_params.get("trailing_atr_mult", 0),
                        "max_hold_bars": long_params.get("max_hold_bars", 0)
                    }]

                short_params = opt_info.get("short_params", {})
                if short_params:
                    cfg["models"]["short"]["threshold"] = short_params.get("entry_threshold", cfg["models"]["short"].get("threshold", 0.5))
                    
                    for k, v in short_params.items():
                        if k != "entry_threshold":
                            cfg["short"][k] = v
                    cfg["short"]["tiered_exits"] = [{"qty_pct": 1.0, "tp_atr_mult": short_params.get("tp_atr_mult", 0)}]
                    cfg["short"]["tiers"] = [{
                        "min_prob": short_params.get("entry_threshold", 0),
                        "lots": 1,
                        "tp_atr_mult": short_params.get("tp_atr_mult", 0),
                        "sl_atr_mult": short_params.get("sl_atr_mult", 0),
                        "trailing_atr_mult": short_params.get("trailing_atr_mult", 0),
                        "max_hold_bars": short_params.get("max_hold_bars", 0)
                    }]

            # T6 post-emission self-check: an emitted config that cannot
            # start live is a BATCH FAILURE — let the resolver's ValueError
            # propagate. model_path existence is WARN only (cloud-side
            # generation may reference not-yet-synced trees).
            resolve_instrument_context(cfg)
            for _side in ("long", "short"):
                _mp = cfg["models"][_side].get("model_path", "")
                if _mp and not os.path.isfile(_mp):
                    print(f"  [WARN] models.{_side}.model_path not found on disk (may be un-synced): {_mp}")

            # Save config
            config_path = os.path.join(configs_dir, config_name)
            with open(config_path, "w") as f:
                json.dump(cfg, f, indent=4)

            # Markdown
            markdown_lines.append(f"## Ensemble {ensemble_idx}: {long_sweep} / {short_sweep}")
            markdown_lines.append(f"**Long**: {long_desc} ({long_sweep}) | **Short**: {short_desc} ({short_sweep})")
            markdown_lines.append(f"**Config**: [{config_name}](configs/{config_name})")
            if use_merged:
                markdown_lines.append(f"**Predictions**: [{predictions_name}](predictions/{predictions_name})")
            else:
                markdown_lines.append(f"**Predictions**: Individual sweep paths")
            
            markdown_lines.append("")
            markdown_lines.append("### Verification Command")
            markdown_lines.append("```bash")
            rel_config_path = f"{batch_dir}/configs/{config_name}".replace("\\", "/")
            _econ_flags = (
                f"--slippage-per-side {args.slippage_per_side} "
                f"--contract-multiplier {contract_multiplier}"
            )
            if args.exec_data:
                markdown_lines.append(f'python agent/backtest_engine.py --config "{rel_config_path}" --data "{args.data}" --exec-data "{args.exec_data}" {_econ_flags}')
            else:
                markdown_lines.append(f'python agent/backtest_engine.py --config "{rel_config_path}" --data "{args.data}" {_econ_flags}')
            markdown_lines.append("```")
            markdown_lines.append("")

            # Run Backtest
            cmd = [
                sys.executable, "agent/backtest_engine.py",
                "--config", config_path,
                "--data", args.data,
                "--slippage-per-side", str(args.slippage_per_side),
                "--contract-multiplier", str(contract_multiplier),
            ]
            if args.exec_data:
                cmd.extend(["--exec-data", args.exec_data])

            print(f"Running backtest for {objective} ensemble {ensemble_idx}...")
            result = subprocess.run(cmd, capture_output=True, text=True)
            
            markdown_lines.append("### Backtest Results")
            markdown_lines.append("```")
            markdown_lines.append(result.stdout.strip())
            if result.stderr.strip():
                markdown_lines.append("\nErrors:")
                markdown_lines.append(result.stderr.strip())
            markdown_lines.append("```")
            markdown_lines.append("\n---")
            markdown_lines.append("")

        md_path = os.path.join(batch_dir, f"{objective}_ensemble_backtests.md")
        with open(md_path, "w") as f:
            f.write("\n".join(markdown_lines))
        print(f"Wrote {md_path}")

if __name__ == "__main__":
    main()
