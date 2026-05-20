"""
Generate Optimized Strategy Configs from Batch Optimization Results.

Reads optimization_results.json + the base ensemble configs from each
experiment's canary_output directory and produces correctly-formatted
strategy JSON configs with ALL top-level keys properly set.

This solves the "missing top-level keys" problem where apply_trial_params()
wrote atr_period, trailing_sl_atr_offset, etc. to the top level during
optimization but those values were lost when configs were manually extracted.

Output: <batch_dir>/configs/<label>_<metric>_opt.json for each ensemble
result with status "OK".

Usage:
    python agent/generate_batch_configs.py --batch-dir reports/batch_runs/batch_20260515_2005
    python agent/generate_batch_configs.py --batch-dir reports/batch_runs/batch_20260515_2005 --min-trades 50
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)


def _apply_side_params(cfg: dict, params: dict, side: str) -> None:
    """Apply optimized parameters to config — matching apply_trial_params behavior.

    Writes to:
      1. cfg[side] (side-level keys)
      2. cfg[side]["tiers"][*] (tier overrides)
      3. cfg[side]["tiered_exits"][*] (exit overrides)
      4. cfg["models"][side]["threshold"] (entry threshold)
      5. cfg (top-level keys — THIS IS THE CRITICAL PART that was missing)
    """
    side_cfg = cfg.get(side, {})

    tp = params.get("tp_atr_mult")
    sl = params.get("sl_atr_mult")
    trailing = params.get("trailing_atr_mult")
    max_hold = params.get("max_hold_bars")
    threshold = params.get("entry_threshold")

    # 1. Side-level keys
    if tp is not None:
        side_cfg["tp_atr_mult"] = tp
    if sl is not None:
        side_cfg["sl_atr_mult"] = sl
    if trailing is not None:
        side_cfg["trailing_atr_mult"] = trailing
    if max_hold is not None:
        side_cfg["max_hold_bars"] = max_hold
    if "cooldown_bars" in params:
        side_cfg["cooldown_bars"] = params["cooldown_bars"]
    if "consecutive_signal_threshold" in params:
        side_cfg["consecutive_signal_threshold"] = params["consecutive_signal_threshold"]
    if "atr_period" in params:
        side_cfg["atr_period"] = params["atr_period"]
    # Support both new canonical key and legacy key from optimization results
    for _tso_key in ("trailing_sl_atr_offset", "trailing_activation_mult"):
        if _tso_key in params:
            side_cfg["trailing_sl_atr_offset"] = params[_tso_key]
            break

    # 2. Tier overrides
    for tier in side_cfg.get("tiers", []):
        if threshold is not None:
            tier["min_prob"] = threshold
        if tp is not None:
            tier["tp_atr_mult"] = tp
        if sl is not None:
            tier["sl_atr_mult"] = sl
        if trailing is not None:
            tier["trailing_atr_mult"] = trailing
        if max_hold is not None:
            tier["max_hold_bars"] = max_hold

    # 3. Tiered exit overrides
    for exit_tier in side_cfg.get("tiered_exits", []):
        if tp is not None:
            exit_tier["tp_atr_mult"] = tp

    cfg[side] = side_cfg

    # 4. Model threshold
    if threshold is not None:
        if "models" in cfg and side in cfg["models"]:
            cfg["models"][side]["threshold"] = threshold

    # 5. Top-level keys (CRITICAL — BacktestEngine.from_config reads these)
    #    Note: for simultaneous ensemble optimization, the SHORT side's
    #    apply_trial_params is called LAST, so its values overwrite the
    #    top-level. We replicate that behavior here by always writing.
    TOP_LEVEL_KEYS = (
        "tp_atr_mult", "sl_atr_mult", "trailing_atr_mult",
        "cooldown_bars", "max_hold_bars", "consecutive_signal_threshold",
        "atr_period", "trailing_sl_atr_offset",
    )
    for key in TOP_LEVEL_KEYS:
        if key in params:
            cfg[key] = params[key]

    if threshold is not None:
        cfg["entry_threshold"] = threshold


def build_config(
    base_cfg: dict,
    long_params: dict,
    short_params: dict,
    optuna_info: dict,
    label: str,
    metric: str,
    gcs_prefix: str,
    batch_id: str,
) -> dict:
    """Build a complete optimized config from base config + optimization results.

    Args:
        base_cfg:     Base ensemble config from canary_output.
        long_params:  Optimized long side parameters.
        short_params: Optimized short side parameters.
        optuna_info:  Full optuna_info block from optimization_results.json.
        label:        Experiment label (e.g. "HS08 5x1 24H").
        metric:       Metric name (e.g. "logloss").
        gcs_prefix:   GCS prefix (e.g. "sweep_hs08_5x1_24h_20260515_2005").
        batch_id:     Batch ID (e.g. "batch_20260515_2005").

    Returns:
        Complete config dict ready for BacktestEngine and live_trader.
    """
    cfg = copy.deepcopy(base_cfg)

    trial_num = optuna_info.get("trial_number", "?")
    n_trials = optuna_info.get("n_trials", "?")
    cfg["description"] = (
        f"{label} {metric} ensemble. Optimized from {batch_id} "
        f"trial #{trial_num}/{n_trials}."
    )

    # Apply long params FIRST, then short (replicates optimizer call order)
    _apply_side_params(cfg, long_params, "long")
    _apply_side_params(cfg, short_params, "short")

    # Set holdout_months from optuna_info
    holdout_months = optuna_info.get("holdout_months", 6)
    cfg["holdout_months"] = holdout_months

    # Attach full optuna_info for provenance
    cfg["optuna_info"] = copy.deepcopy(optuna_info)

    return cfg


def main():
    parser = argparse.ArgumentParser(
        description="Generate optimized strategy configs from batch optimization results."
    )
    parser.add_argument(
        "--batch-dir", required=True,
        help="Path to batch directory (e.g. reports/batch_runs/batch_20260515_2005)"
    )
    parser.add_argument(
        "--min-trades", type=int, default=10,
        help="Skip configs with fewer optimized trades than this (default: 10)"
    )
    parser.add_argument(
        "--min-pf", type=float, default=1.0,
        help="Skip configs with profit factor below this (default: 1.0)"
    )
    parser.add_argument(
        "--objective", default="sharpe",
        help="Objective label for filename namespacing (default: sharpe)"
    )
    args = parser.parse_args()

    batch_dir = args.batch_dir
    batch_id = os.path.basename(batch_dir)

    # Load optimization results
    results_path = os.path.join(batch_dir, f"optimization_results_{args.objective}.json")
    # Fallback to legacy filename if objective-specific one doesn't exist
    if not os.path.exists(results_path):
        results_path = os.path.join(batch_dir, "optimization_results.json")
    if not os.path.exists(results_path):
        print(f"ERROR: {results_path} not found")
        sys.exit(1)
    with open(results_path, encoding="utf-8-sig") as f:
        all_results = json.load(f)

    # Load batch progress for experiment metadata
    progress_path = os.path.join(batch_dir, "batch_progress.json")
    if not os.path.exists(progress_path):
        print(f"ERROR: {progress_path} not found")
        sys.exit(1)
    with open(progress_path, encoding="utf-8-sig") as f:
        progress = json.load(f)

    # Build experiment lookup: label -> experiment info
    exp_lookup: dict[str, dict] = {}
    for exp in progress.get("experiments", []):
        if exp.get("status") == "COMPLETED":
            exp_lookup[exp["label"]] = exp

    # Create output directory
    configs_dir = os.path.join(batch_dir, "configs")
    os.makedirs(configs_dir, exist_ok=True)

    generated = 0
    skipped = 0

    for key, result in sorted(all_results.items()):
        if result.get("status") != "OK":
            continue

        # Parse key: "{label}|{side}|{metric}" (per-side) or "{label}|ensemble|{metric}" (legacy)
        parts = key.split("|")
        if len(parts) != 3:
            continue

        label, direction, metric = parts
        # Accept both per-side (long/short) and legacy (ensemble) keys
        if direction not in ("long", "short", "ensemble"):
            continue
        exp = exp_lookup.get(label)
        if exp is None:
            print(f"  SKIP {key}: experiment not found in progress")
            skipped += 1
            continue

        # Check min trades/PF filters
        metrics = result.get("metrics", {})
        trade_count = metrics.get("trade_count", 0)
        profit_factor = metrics.get("profit_factor", 0)
        if trade_count < args.min_trades:
            print(f"  SKIP {key}: {trade_count} trades < min {args.min_trades}")
            skipped += 1
            continue
        if profit_factor < args.min_pf:
            print(f"  SKIP {key}: PF {profit_factor:.2f} < min {args.min_pf}")
            skipped += 1
            continue

        # Resolve optuna_info (handles both in-memory and JSON-loaded structures)
        if "optuna_info" in result:
            optuna_info = result["optuna_info"]
        else:
            optuna_info = result.get("config", {}).get("optuna_info", {})

        long_params = optuna_info.get("long_params", {})
        short_params = optuna_info.get("short_params", {})

        if not long_params and not short_params:
            # For per-side results, params are in the top-level "params" key
            side_params = optuna_info.get("params", {})
            optimize_side = optuna_info.get("optimize_side", direction)
            if direction == "long" or optimize_side == "long":
                long_params = side_params
                short_params = {}
            elif direction == "short" or optimize_side == "short":
                short_params = side_params
                long_params = {}
            else:
                print(f"  SKIP {key}: cannot determine side params")
                skipped += 1
                continue

        # Load base ensemble config from canary_output
        local_dir = exp["local_dir"]
        canary_dir = os.path.join(local_dir, "registry", "canary_output")
        gcs_prefix = exp.get("gcs_prefix", "")

        # Try new naming convention first, fallback to legacy
        base_config_new = os.path.join(canary_dir, f"{gcs_prefix}_{metric}.json")
        base_config_old = os.path.join(canary_dir, f"ensemble_config_{metric}.json")
        base_config_path = base_config_new if os.path.exists(base_config_new) else base_config_old

        if not os.path.exists(base_config_path):
            print(f"  SKIP {key}: base config not found at {base_config_path}")
            skipped += 1
            continue

        with open(base_config_path, encoding="utf-8-sig") as f:
            base_cfg = json.load(f)

        # Build the complete optimized config
        # For per-side results, only apply the optimized side's params
        opt_cfg = build_config(
            base_cfg=base_cfg,
            long_params=long_params,
            short_params=short_params,
            optuna_info=optuna_info,
            label=label,
            metric=metric,
            gcs_prefix=gcs_prefix,
            batch_id=batch_id,
        )

        # Save to configs directory — include side and objective in filename
        safe_label = label.lower().replace(" ", "_")
        filename = f"{safe_label}_{metric}_{direction}_{args.objective}_opt.json"
        filepath = os.path.join(configs_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(opt_cfg, f, indent=4)

        pnl = metrics.get("total_pnl", 0)
        ho_pnl = optuna_info.get("holdout_metrics", {}).get("total_pnl", "N/A")
        print(
            f"  OK {key}: {filename}  "
            f"(PnL=${pnl:,.0f}, HO=${ho_pnl if isinstance(ho_pnl, str) else f'{ho_pnl:,.0f}'}, "
            f"{trade_count} trades, PF={profit_factor:.2f})"
        )
        generated += 1

    print(f"\n{'='*60}")
    print(f"Generated: {generated} configs")
    print(f"Skipped:   {skipped} (filtered or missing)")
    print(f"Output:    {configs_dir}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
