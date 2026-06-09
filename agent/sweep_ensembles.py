"""
Automated Ensemble Sweep (Cartesian Pairing)

This script executes a Cartesian sweep of Long and Short models, pairing them
together to find the ultimate strategy combinations without triggering 
expensive retraining logic.

Tags: cartesian sweep, ensemble sweep, pairing, combinatorics
"""

import json
import subprocess
import os
import re
import argparse
import pandas as pd

def get_models_from_dir(directory, prefix=""):
    models = []
    if os.path.exists(directory):
        for root, dirs, files in os.walk(directory):
            if "oos_predictions.csv" in files:
                basename = os.path.basename(root)
                if prefix == "" or basename.startswith(prefix) or prefix in root:
                    models.append(root.replace("\\", "/"))
    return models

def run_backtest(long_path, short_path, base_config, data_path, temp_config, long_threshold=None, short_threshold=None):
    # Load base config fresh for each pair
    with open(base_config, "r") as f:
        cfg = json.load(f)

    # Ensure models block exists
    if "models" not in cfg:
        cfg["models"] = {"long": {}, "short": {}}

    # --- Resolve thresholds ---
    # Priority: CLI override > base config value > safe default of 0.55
    base_long_thr  = cfg["models"].get("long",  {}).get("threshold", 0.55)
    base_short_thr = cfg["models"].get("short", {}).get("threshold", 0.55)
    final_long_thr  = long_threshold  if long_threshold  is not None else base_long_thr
    final_short_thr = short_threshold if short_threshold is not None else base_short_thr

    # --- Patch Long model ---
    cfg["models"]["long"]["experiment_id"]   = long_path.split("/")[-1]
    cfg["models"]["long"]["model_path"]       = long_path
    cfg["models"]["long"]["predictions_path"] = f"{long_path}/oos_predictions.csv"
    cfg["models"]["long"]["threshold"]        = final_long_thr   # enforce threshold!

    # For TieredEnsembleStrategy: also write into tiers[*].min_prob,
    # which is the actual source of truth for execution.
    for tier in cfg.get("long", {}).get("tiers", []):
        tier["min_prob"] = final_long_thr

    # --- Patch Short model ---
    cfg["models"]["short"]["experiment_id"]   = short_path.split("/")[-1]
    cfg["models"]["short"]["model_path"]       = short_path
    cfg["models"]["short"]["predictions_path"] = f"{short_path}/oos_predictions.csv"
    cfg["models"]["short"]["threshold"]        = final_short_thr  # enforce threshold!

    # For TieredEnsembleStrategy: also write into tiers[*].min_prob.
    for tier in cfg.get("short", {}).get("tiers", []):
        tier["min_prob"] = final_short_thr

    # Save temp config
    with open(temp_config, "w") as f:
        json.dump(cfg, f, indent=4)
        
    cmd = [
        "python", "agent/backtest_engine.py",
        "--config", temp_config,
        "--data", data_path,
        "--slippage-per-side", "0.01"
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    out = result.stdout
    
    trades = re.search(r"Total Trades:\s+(\d+)", out)
    trades = trades.group(1) if trades else "0"
    
    pnl = re.search(r"Total Net PnL:\s+\$\s+([\-\d\.,]+)", out)
    pnl = pnl.group(1) if pnl else "0"
    
    winrate = re.search(r"Win Rate:\s+([\d\.]+%)", out)
    winrate = winrate.group(1) if winrate else "0%"
    
    pf = re.search(r"Profit Factor:\s+([\d\.]+)", out)
    pf = pf.group(1) if pf else "0.0"
    
    max_dd = re.search(r"Max Drawdown:\s+\$\s+([\-\d\.,]+)", out)
    max_dd = max_dd.group(1) if max_dd else "0"
    
    tail_pnl = 0.0
    for line in out.splitlines():
        if line.strip().startswith("2025") or line.strip().startswith("2026"):
            if "Total" in line:
                match = re.search(r"\$\s*([\-\d\.,]+)\s*\|", line)
                if match:
                    val = match.group(1).replace(",", "")
                    tail_pnl += float(val)

    return {"trades": trades, "pnl": pnl, "winrate": winrate, "pf": pf, "max_dd": max_dd, "tail_pnl": f"{tail_pnl:,.2f}"}


def main():
    parser = argparse.ArgumentParser(description="Sweep Model Ensembles")
    parser.add_argument("--base-config", required=True, help="Base JSON strategy")
    parser.add_argument("--data", required=True, help="Parquet Dataset")
    parser.add_argument("--long-dir", required=True, help="Directory containing Long models")
    parser.add_argument("--short-dir", required=True, help="Directory containing Short models")
    parser.add_argument("--long-prefix", default="", help="Prefix string to filter Long models")
    parser.add_argument("--short-prefix", default="", help="Prefix string to filter Short models")
    parser.add_argument("--output-csv", default=None, help="Output CSV report")
    parser.add_argument("--output-md", default=None, help="Output MD report")
    parser.add_argument("--long-threshold", type=float, default=None, help="Override Buy probability threshold (e.g. 0.55)")
    parser.add_argument("--short-threshold", type=float, default=None, help="Override Sell probability threshold (e.g. 0.55)")
    args = parser.parse_args()

    print(f"\nScanning for models...")
    long_models = get_models_from_dir(args.long_dir, args.long_prefix)
    short_models = get_models_from_dir(args.short_dir, args.short_prefix)

    print(f"Discovered {len(long_models)} Long and {len(short_models)} Short candidates.")
    if not long_models or not short_models:
        print("Missing models to sweep. Exiting.")
        return

    # Resolve thresholds for display
    with open(args.base_config) as f:
        _base = json.load(f)
    _base_long_thr  = _base.get("models", {}).get("long",  {}).get("threshold", 0.55)
    _base_short_thr = _base.get("models", {}).get("short", {}).get("threshold", 0.55)
    eff_long_thr  = args.long_threshold  if args.long_threshold  is not None else _base_long_thr
    eff_short_thr = args.short_threshold if args.short_threshold is not None else _base_short_thr
    print(f"Thresholds enforced: Buy >= {eff_long_thr}, Sell >= {eff_short_thr}")

    temp_cfg = "configs/strategies/temp_sweep_config.json"
    results = []
    
    from concurrent.futures import ThreadPoolExecutor, as_completed
    
    def process_pair(lname, lpath, sname, spath):
        # Temp config needs to be unique for concurrent execution
        thread_temp_cfg = temp_cfg.replace(".json", f"_{lname}_{sname}.json")
        met = run_backtest(
            lpath, spath, args.base_config, args.data, thread_temp_cfg, 
            args.long_threshold, args.short_threshold
        )
        if os.path.exists(thread_temp_cfg):
            os.remove(thread_temp_cfg)
        if not met: return None
        return {
            "Long Model": lname,
            "Short Model": sname,
            "Trades": int(met['trades']),
            "Win Rate": met['winrate'],
            "Profit Factor": float(met['pf']) if met['pf'] else 0.0,
            "Net PnL": float(met['pnl'].replace(",", "")) if met['pnl'] else 0.0,
            "Max DD": met['max_dd'],
            "Tail PnL": met['tail_pnl'],
            "print_str": f"{lname[:40]:<40} | {sname[:40]:<40} | {met['trades']:<4} | {met['winrate']:<5} | {met['pf']:<5} | {met['pnl']:<10} | {met['max_dd']:<10} | {met['tail_pnl']:<10}"
        }

    print(f"{'Long Model':<40} | {'Short Model':<40} | {'Trds':<4} | {'WR%':<5} | {'PF':<5} | {'Net PnL':<10} | {'Max DD':<10} | {'Tail PnL':<10}")
    print("-" * 145)

    futures = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        for lpath in long_models:
            lparts = lpath.split("/")
            lname = f"{lparts[-2]}_{lparts[-1]}" if len(lparts) >= 2 else lparts[-1]
            for spath in short_models:
                sparts = spath.split("/")
                sname = f"{sparts[-2]}_{sparts[-1]}" if len(sparts) >= 2 else sparts[-1]
                futures.append(executor.submit(process_pair, lname, lpath, sname, spath))
                
        for future in as_completed(futures):
            res = future.result()
            if res:
                print(res.pop("print_str"))
                results.append(res)
        
    if results:
        df = pd.DataFrame(results)
        df = df.sort_values(by="Profit Factor", ascending=False)
        
        if hasattr(args, 'output_csv') and args.output_csv:
            os.makedirs(os.path.dirname(args.output_csv), exist_ok=True)
            df.to_csv(args.output_csv, index=False)
            print(f"\nSaved Sorted CSV Report to {args.output_csv}")
            
        if hasattr(args, 'output_md') and args.output_md:
            os.makedirs(os.path.dirname(args.output_md), exist_ok=True)
            with open(args.output_md, 'w') as f:
                f.write(f"# Ensemble Sweep Results\n\n")
                try:
                    f.write(df.to_markdown(index=False))
                except ImportError:
                    # tabulate not installed — fall back to plain text table
                    f.write("```\n")
                    f.write(df.to_string(index=False))
                    f.write("\n```")
            print(f"\nSaved Sorted MD Report to {args.output_md}")

if __name__ == "__main__":
    main()
