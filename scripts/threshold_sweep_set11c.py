"""Threshold sweep on set_11c long_logloss OOS predictions using BacktestEngine."""
import json, sys, os, copy
import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from agent.backtest_engine import BacktestEngine, load_predictions, load_ohlcv

def run_sweep():
    preds_path = "canary_results/canary_output/registry/E2E_set_11c_long_logloss/oos_predictions.csv"
    data_path = "C:/CL_Analyst_Data/data/processed/cl-5m_bk_set_11c.parquet"

    print("Loading predictions...")
    preds = load_predictions(preds_path)
    print(f"  Predictions: {len(preds)} rows, prob_Buy range: [{preds['prob_Buy'].min():.4f}, {preds['prob_Buy'].max():.4f}]")

    print("Loading OHLCV data...")
    ohlcv = load_ohlcv(data_path)

    # Base config: ensemble6_canary (TP=3.5x, SL=1.5x, consecutive=2) — same as canary run
    base_config = {
        "nickname": "set_11c_sweep",
        "execution_class": "ConservativeEnsembleStrategy",
        "models": {
            "long": {"experiment_id": "E2E_set_11c_long_logloss", "predictions_path": preds_path, "threshold": 0.60},
            "short": {"experiment_id": "none", "predictions_path": "", "threshold": 1.0}
        },
        "tp_atr_mult": 3.5,
        "sl_atr_mult": 1.5,
        "trailing_atr_mult": 99.0,
        "consecutive_signal_threshold": 2,
        "cooldown_bars": 6,
        "max_holding_bars": 288,
        "sizing_tiers": {"0.80": 3, "0.70": 2, "0.60": 1}
    }

    thresholds = np.arange(0.45, 0.66, 0.01)
    results = []

    print(f"\n{'='*70}")
    print(f"THRESHOLD SWEEP — set_11c long_logloss (0.45 → 0.65)")
    print(f"{'='*70}")
    print(f"{'Threshold':>10} {'Trades':>8} {'WR':>8} {'PF':>8} {'PnL':>12}")
    print(f"{'-'*10} {'-'*8} {'-'*8} {'-'*8} {'-'*12}")

    for th in thresholds:
        cfg = copy.deepcopy(base_config)
        cfg["models"]["long"]["threshold"] = float(th)
        # Update sizing tiers to match threshold
        cfg["sizing_tiers"] = {f"{th+0.20:.2f}": 3, f"{th+0.10:.2f}": 2, f"{th:.2f}": 1}

        bt = BacktestEngine.from_config(cfg)
        res = bt.run(preds, ohlcv, label=f"TH_{th:.2f}")

        pnl = res.total_pnl
        pf = res.profit_factor
        wr = res.win_rate
        trades = res.trade_count

        marker = " <<<" if wr > 0.30 and trades >= 150 else (" *" if wr > 0.30 else "")
        print(f"{th:>10.2f} {trades:>8} {wr:>7.1%} {pf:>8.2f} ${pnl:>11,.2f}{marker}")

        results.append({
            "threshold": round(float(th), 2),
            "trades": int(trades),
            "win_rate": round(float(wr), 4),
            "profit_factor": round(float(pf), 4),
            "pnl": round(float(pnl), 2)
        })

    print(f"{'='*70}")

    # Find best by criteria: WR > 30% and max trades
    eligible = [r for r in results if r["win_rate"] > 0.30]
    if eligible:
        best = max(eligible, key=lambda x: x["trades"])
        print(f"\nBEST (max trades, WR>30%): threshold={best['threshold']:.2f}")
        print(f"  Trades: {best['trades']}, WR: {best['win_rate']:.1%}, PF: {best['profit_factor']:.2f}, PnL: ${best['pnl']:,.2f}")
    else:
        best_wr = max(results, key=lambda x: x["win_rate"])
        print(f"\nNo threshold with WR>30%. Best WR: {best_wr['win_rate']:.1%} at threshold={best_wr['threshold']:.2f}")

    # Also find best by PnL
    best_pnl = max(results, key=lambda x: x["pnl"])
    print(f"\nBEST BY PNL: threshold={best_pnl['threshold']:.2f}")
    print(f"  Trades: {best_pnl['trades']}, WR: {best_pnl['win_rate']:.1%}, PF: {best_pnl['profit_factor']:.2f}, PnL: ${best_pnl['pnl']:,.2f}")

    # Save results
    with open("reports/threshold_sweep_set11c.json", "w") as f:
        json.dump({"sweep": results, "best_max_trades_wr30": best if eligible else None, "best_pnl": best_pnl}, f, indent=2)
    print(f"\nSaved to reports/threshold_sweep_set11c.json")

if __name__ == "__main__":
    run_sweep()
