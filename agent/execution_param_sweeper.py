import optuna
import json
import subprocess
import os
import re
import argparse

def evaluate_config(cfg_path, data_path):
    cmd = [
        "python", "agent/backtest_engine.py",
        "--config", cfg_path,
        "--data", data_path,
        "--slippage-per-side", "0.01"
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    out = result.stdout
    
    trades = re.search(r"Total Trades:\s+(\d+)", out)
    trades = int(trades.group(1).replace(",", "")) if trades else 0
    
    pnl = re.search(r"Total Net PnL:\s+\$\s+([\-\d\.,]+)", out)
    pnl = float(pnl.group(1).replace(",", "")) if pnl else -999999.0
    
    max_dd = re.search(r"Max Drawdown:\s+\$\s+([\-\d\.,]+)", out)
    max_dd = float(max_dd.group(1).replace(",", "")) if max_dd else 999999.0
    
    return trades, pnl, max_dd

def objective(trial, base_config_path, data_path, temp_config_path):
    with open(base_config_path, "r") as f:
        cfg = json.load(f)

    # Cooldown parameters
    cfg["sl_cooldown_bars"] = trial.suggest_int("sl_cooldown_bars", 0, 6)
    cfg["tp_cooldown_bars"] = trial.suggest_int("tp_cooldown_bars", 0, 6)
    cfg["max_hold_bars"] = trial.suggest_int("max_hold_bars", 120, 300, step=24)
    
    # Thresholds
    long_thresh = trial.suggest_float("long_threshold", 0.50, 0.65, step=0.01)
    short_thresh = trial.suggest_float("short_threshold", 0.50, 0.65, step=0.01)
    cfg["models"]["long"]["threshold"] = round(long_thresh, 2)
    cfg["models"]["short"]["threshold"] = round(short_thresh, 2)
    
    # Tiers probabilities must match threshold for 1-lot trading
    if "tiers" in cfg["long"]:
        cfg["long"]["tiers"][0]["min_prob"] = round(long_thresh, 2)
    if "tiers" in cfg["short"]:
        cfg["short"]["tiers"][0]["min_prob"] = round(short_thresh, 2)
        
    # Exits
    long_tp = trial.suggest_float("long_tp_atr", 1.5, 4.0, step=0.1)
    long_sl = trial.suggest_float("long_sl_atr", 1.0, 4.0, step=0.1)
    short_tp = trial.suggest_float("short_tp_atr", 1.5, 4.0, step=0.1)
    short_sl = trial.suggest_float("short_sl_atr", 1.0, 4.0, step=0.1)
    
    cfg["long"]["tiered_exits"][0]["tp_atr_mult"] = round(long_tp, 1)
    cfg["long"]["sl_atr_mult"] = round(long_sl, 1)
    
    cfg["short"]["tiered_exits"][0]["tp_atr_mult"] = round(short_tp, 1)
    cfg["short"]["sl_atr_mult"] = round(short_sl, 1)
    
    # Save the permutation
    with open(temp_config_path, "w") as f:
        json.dump(cfg, f, indent=4)
        
    trades, pnl, max_dd = evaluate_config(temp_config_path, data_path)
    
    # Reward metric: PnL / Absolute Drawdown
    # Heavily penalize strategies with < 200 trades (curve fitting risk)
    if trades < 200:
        return -99999.0
        
    score = pnl / (abs(max_dd) + 1000)
    
    # Store artifacts
    trial.set_user_attr("trades", trades)
    trial.set_user_attr("pnl", pnl)
    trial.set_user_attr("max_dd", max_dd)
    
    return score

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-config", required=True)
    parser.add_argument("--data", required=True)
    parser.add_argument("--trials", type=int, default=100)
    parser.add_argument("--temp-cfg", default="configs/strategies/optuna_temp.json")
    args = parser.parse_args()
    
    study = optuna.create_study(direction="maximize")
    study.optimize(
        lambda t: objective(t, args.base_config, args.data, args.temp_cfg), 
        n_trials=args.trials,
        n_jobs=1
    )
    
    print("\n===============================")
    print("BEST PARAMETERS FOUND")
    print("===============================")
    best_trial = study.best_trial
    print(f"Score: {best_trial.value:.4f}")
    print(f"PnL:   ${best_trial.user_attrs['pnl']:,.2f}")
    print(f"Trades:{best_trial.user_attrs['trades']}")
    print(f"Max DD:${best_trial.user_attrs['max_dd']:,.2f}")
    print("-------------------------------")
    for key, value in best_trial.params.items():
        print(f"  {key}: {value}")
        
    if os.path.exists(args.temp_cfg):
        os.remove(args.temp_cfg)

if __name__ == "__main__":
    main()
