import os
import sys
import json
import argparse
import subprocess
import shutil
import re
from datetime import datetime

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


def _canonical_pair_order(opt_data, batch_dir):
    """Return (pair_key, pair_val) items in the canonical top_pairs.json order.

    E-slot labels (E01..E04) are assigned by enumeration position, so the order
    MUST match top_pairs.json — the single source of truth for which pair is
    ensemble N. Driving the order off ``sorted(opt_data.items(), ...)`` let the
    non-deterministic as_completed() insertion order leak into the E labels
    whenever the sort key tied (single-sweep canary), mislabeling the emitted
    predictions/configs/backtests run-to-run.

    Falls back to the deterministic, metric-aware ``_ensemble_sort_key`` when
    top_pairs.json is missing/unreadable. Any opt_data pair not declared in
    top_pairs.json is appended (never dropped) in deterministic order; any
    declared pair missing from opt_data is reported loudly.
    """
    tp_path = os.path.join(batch_dir, "top_pairs.json")
    top_pairs = None
    if os.path.isfile(tp_path):
        try:
            with open(tp_path, "r", encoding="utf-8-sig") as f:
                top_pairs = json.load(f)
        except Exception as e:
            print(f"  [WARN] top_pairs.json unreadable ({e}); using deterministic sort fallback for E-order")
            top_pairs = None

    if not top_pairs:
        if not os.path.isfile(tp_path):
            print("  [WARN] top_pairs.json missing; using deterministic sort fallback for E-order")
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
        print(f"  [WARN] {len(missing)} top_pairs.json pair(s) not found in opt_data (cannot emit): {missing}")
    if leftover:
        print(f"  [WARN] {len(leftover)} opt_data pair(s) not declared in top_pairs.json; appending in deterministic order after declared pairs")
    return ordered + leftover


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
    parser.add_argument("--slippage-per-side", type=float, default=0.01, help="Slippage per side")
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
        # E-slot order MUST follow top_pairs.json (the canonical selection), not
        # the non-deterministic opt_data insertion order. See _canonical_pair_order.
        sorted_opt_data = _canonical_pair_order(opt_data, batch_dir)

        # Derive e2e_dataset_tag the exact same way vm_e2e_pipeline.py does
        data_basename = os.path.splitext(os.path.basename(args.data))[0]
        match = re.search(r'bk_(.+)$', data_basename)
        e2e_dataset_tag = match.group(1) if match else data_basename
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
            cfg["nickname"] = nickname
            cfg["description"] = f"{objective.capitalize()} Ensemble #{ensemble_idx}: {long_desc} + {short_desc}"
            cfg["holdout_months"] = manifest.get("defaults", {}).get("post_optimizer_holdout_months", 6)
            
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
            if args.exec_data:
                markdown_lines.append(f'python agent/backtest_engine.py --config "{rel_config_path}" --data "{args.data}" --exec-data "{args.exec_data}" --slippage-per-side {args.slippage_per_side}')
            else:
                markdown_lines.append(f'python agent/backtest_engine.py --config "{rel_config_path}" --data "{args.data}" --slippage-per-side {args.slippage_per_side}')
            markdown_lines.append("```")
            markdown_lines.append("")
            
            # Run Backtest
            cmd = [
                sys.executable, "agent/backtest_engine.py",
                "--config", config_path,
                "--data", args.data,
                "--slippage-per-side", str(args.slippage_per_side)
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
