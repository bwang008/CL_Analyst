import json
import os
import sys
import copy
import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from agent.backtest_engine import BacktestEngine, load_predictions, load_ohlcv

def run_sweep():
    print("Loading predictions...")
    preds = load_predictions("reports/canary_asym/oos_predictions_asym.csv")
    
    print("Loading historical data...")
    ohlcv = load_ohlcv("data/CL_set_11_asym.parquet")
    
    with open("configs/strategies/ensemble_asym.json", "r") as f:
        base_config = json.load(f)
        
    thresholds = np.arange(0.50, 0.96, 0.05)
    results = []
    
    print("\n--- Running Tier 2 Threshold Sweep ---")
    for th in thresholds:
        print(f"Testing threshold: {th:.2f}... ", end="", flush=True)
        
        cfg = copy.deepcopy(base_config)
        cfg["nickname"] = f"Asym_TH_{th:.2f}"
        cfg["models"]["long"]["threshold"] = float(th)
        cfg["models"]["short"]["threshold"] = float(th)
        # For TieredEnsembleStrategy: also write into tiers[*].min_prob,
        # which is the actual source of truth for execution.
        for tier in cfg.get("long", {}).get("tiers", []):
            tier["min_prob"] = float(th)
        for tier in cfg.get("short", {}).get("tiers", []):
            tier["min_prob"] = float(th)
        
        bt = BacktestEngine.from_config(cfg)
        res = bt.run(preds, ohlcv, label=f"TH_{th:.2f}")
        
        pnl = res.total_pnl
        pf = res.profit_factor
        wr = res.win_rate
        trades = res.trade_count
        
        print(f"PnL: ${pnl:>10,.2f} | PF: {pf:.2f} | WR: {wr:.1%} | Trades: {trades}")
        
        results.append({
            "threshold": float(th),
            "pnl": float(pnl),
            "profit_factor": float(pf),
            "win_rate": float(wr),
            "trades": int(trades)
        })
        
    print("\n--- Sweep Complete ---")
    if len(results) > 0:
        profitable = [r for r in results if r["pnl"] > 0]
        if profitable:
            best = max(profitable, key=lambda x: x["pnl"])
            print(f"\nBEST THRESHOLD BY PNL: {best['threshold']:.2f} -> ${best['pnl']:,.2f} (PF: {best['profit_factor']:.2f}, WR: {best['win_rate']:.1%}, Trades: {best['trades']})")
        else:
            best_loss = max(results, key=lambda x: x["pnl"])
            print(f"NO PROFITABLE THRESHOLDS. Best was {best_loss['threshold']:.2f} -> ${best_loss['pnl']:,.2f}")

if __name__ == "__main__":
    run_sweep()
