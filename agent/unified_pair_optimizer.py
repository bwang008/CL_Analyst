import argparse
import os
import re
import sys
import json
import itertools
from tabulate import tabulate

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

def parse_markdown_table(filepath, direction, metric, objective, progress_data):
    models = []
    if not os.path.exists(filepath):
        print(f"Warning: {filepath} not found.")
        return models
        
    with open(filepath, 'r', encoding='utf-8-sig') as f:
        lines = f.readlines()
    
    in_table = False
    current_section = None
    
    for line in lines:
        line = line.strip()
        if line.startswith("### "):
            current_section = line
            continue
            
        if line.startswith("|") and "Experiment" in line:
            # We found a table header. Check if the section matches.
            if not current_section:
                continue
            
            # Extract direction and metric from section
            # e.g., "### Long Model (Logloss)"
            is_long = "Long" in current_section
            is_short = "Short" in current_section
            is_logloss = "Logloss" in current_section
            is_ap = "Average Precision" in current_section
            
            if (direction == "long" and is_long) or (direction == "short" and is_short):
                if (metric == "logloss" and is_logloss) or (metric == "average_precision" and is_ap):
                    in_table = True
                    continue
                else:
                    in_table = False
            else:
                in_table = False
                
        elif line.startswith("|") and line.replace(" ", "").startswith("|---"):
            continue
        elif line.startswith("|") and in_table:
            # Parse row
            # | Experiment | Trades (pre) | Trades (opt) | PF (pre) | PF (opt) | PnL (pre) | PnL (opt) | PnL (holdout) | ...
            cols = [c.strip() for c in line.split("|")][1:-1]
            if len(cols) < 8:
                continue
            
            exp_label = cols[0]
            trades_str = cols[2]
            pnl_opt_str = cols[6]
            pnl_holdout_str = cols[7]
            
            try:
                if '/' in trades_str:
                    trades = int(trades_str.split('/')[0])
                else:
                    trades = int(trades_str)
                    
                pnl_opt = float(pnl_opt_str.replace('$', '').replace(',', ''))
                pnl_holdout = float(pnl_holdout_str.replace('$', '').replace(',', ''))
                
                # Holdout weight 2 (was 6): the 6x weight was calibrated when
                # post_optimizer_holdout_months was 6; at the 12-month holdout the
                # window PnL is ~2x larger, so 6x made selection effectively
                # holdout-only and promoted peak-of-noise holdout draws.
                robustness = pnl_opt + (pnl_holdout * 2)

                # Minimums: PnL (opt) > 0, PnL (holdout) > 0, Trades (opt) >= 100
                passed_filter = pnl_opt > 0 and pnl_holdout > 0 and trades >= 100
                if not passed_filter:
                    # Heavily penalize failed models so they only get picked if we don't have enough passing models
                    robustness -= 1000000.0
                    
                # Compute unique prefix
                gcs_prefix = None
                for exp in progress_data.get("experiments", []):
                    if exp["label"] == exp_label:
                        gcs_prefix = exp["gcs_prefix"]
                        break
                if gcs_prefix:
                    # Use the flat CSV naming convention from new orchestrator
                    unique_target = f"oos_predictions_{gcs_prefix}_{direction}_{metric}"
                    
                    models.append({
                        "experiment": exp_label,
                        "direction": direction,
                        "metric": metric,
                        "objective": objective,
                        "unique_target": unique_target,
                        "trades": trades,
                        "pnl_opt": pnl_opt,
                        "pnl_holdout": pnl_holdout,
                        "robustness": robustness,
                        "passed_filter": passed_filter
                    })
            except Exception as e:
                pass
        else:
            in_table = False
            
    return models

VALID_OBJECTIVES = ("sharpe", "sortino", "block_min", "block_median", "block_mean_std")


def select_pairs_for_objective(batch_dir, objective, progress_data, top_n):
    """Per-arm pass-2 pair selection: read ONLY this arm's summary md and
    write its own top-pairs JSON (top_pairs.json for sharpe — parity
    compatible — else top_pairs_<arm>.json). NO cross-arm dedup ever:
    pooling arms would cross-contaminate the pass-2 A/B.
    """
    md_path = os.path.join(batch_dir, f"batch_summary_optimized_{objective}.md")
    if not os.path.exists(md_path):
        print(f"ERROR: {md_path} not found for objective '{objective}'")
        sys.exit(1)

    long_models = []
    short_models = []
    for direction in ["long", "short"]:
        for metric in ["logloss", "average_precision"]:
            models = parse_markdown_table(md_path, direction, metric, objective, progress_data)
            if direction == "long":
                long_models.extend(models)
            else:
                short_models.extend(models)

    # Sort and take top N
    long_models = sorted(long_models, key=lambda x: x["robustness"], reverse=True)[:top_n]
    short_models = sorted(short_models, key=lambda x: x["robustness"], reverse=True)[:top_n]

    print(f"## Top {top_n} Long Models ({objective})")
    table_long = [[m["experiment"], m["metric"], m["objective"], m["pnl_opt"], m["pnl_holdout"], m["trades"], m["robustness"]] for m in long_models]
    print(tabulate(table_long, headers=["Experiment", "Metric", "Objective", "Opt PnL", "Holdout PnL", "Trades", "Robustness Score"], tablefmt="github", floatfmt=".2f"))

    print(f"\n## Top {top_n} Short Models ({objective})")
    table_short = [[m["experiment"], m["metric"], m["objective"], m["pnl_opt"], m["pnl_holdout"], m["trades"], m["robustness"]] for m in short_models]
    print(tabulate(table_short, headers=["Experiment", "Metric", "Objective", "Opt PnL", "Holdout PnL", "Trades", "Robustness Score"], tablefmt="github", floatfmt=".2f"))

    # Combinatorial Pairing. The qualify flags are ADDITIVE keys: every
    # reader resolves pairs via .get("target_long")/.get("target_short")
    # and compare_parity only checks the pair COUNT — but downstream report
    # generators need to know when a slot was filled by a PENALIZED
    # (-1e6 robustness) non-qualifying leg rather than a real candidate.
    pairs = []
    for l, s in itertools.product(long_models, short_models):
        pairs.append({
            "target_long": l["unique_target"],
            "target_short": s["unique_target"],
            "long_passed_filter": bool(l["passed_filter"]),
            "short_passed_filter": bool(s["passed_filter"]),
        })

    out_name = "top_pairs.json" if objective == "sharpe" else f"top_pairs_{objective}.json"
    out_json = os.path.join(batch_dir, out_name)
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(pairs, f, indent=4)

    print(f"\nPairs saved to {out_json}")

    print("\nTo run the execution pipeline, execute the following command:")
    print(f"python agent/batch_post_optimizer.py --batch-dir {batch_dir} --target-pairs-json {out_json} --objective {objective}")


def main():
    parser = argparse.ArgumentParser(description="Top-N Unified Selection & Pairing Engine (N x N combinatorial pairs)")
    parser.add_argument("--batch-dir", required=True, help="Path to batch directory")
    parser.add_argument("--top-n", type=int, default=2,
                        help="Number of top models to select per side (manifest optuna.pair_selection_top_n; default 2 -> 4 pairs)")
    parser.add_argument(
        "--objectives", default="sharpe",
        help="Comma-separated objective arms. Each arm reads ONLY its own "
             "batch_summary_optimized_<arm>.md and writes top_pairs.json "
             "(sharpe) or top_pairs_<arm>.json — never pooled across arms "
             "(default: sharpe)"
    )
    args = parser.parse_args()

    batch_dir = args.batch_dir
    progress_path = os.path.join(batch_dir, "batch_progress.json")
    if not os.path.exists(progress_path):
        print(f"ERROR: {progress_path} not found")
        sys.exit(1)

    with open(progress_path, 'r', encoding='utf-8-sig') as f:
        progress_data = json.load(f)

    arms = []
    for raw in args.objectives.split(","):
        arm = raw.strip()
        if not arm:
            continue
        for obj in (["sharpe", "sortino"] if arm == "both" else [arm]):
            if obj not in VALID_OBJECTIVES:
                print(f"ERROR: unknown objective '{arm}'; valid: {', '.join(VALID_OBJECTIVES)} (or 'both')")
                sys.exit(1)
            if obj not in arms:
                arms.append(obj)
    if not arms:
        print(f"ERROR: --objectives {args.objectives!r} yields no objective arms")
        sys.exit(1)

    for objective in arms:
        select_pairs_for_objective(batch_dir, objective, progress_data, args.top_n)

if __name__ == "__main__":
    main()
