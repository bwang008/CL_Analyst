import sys
import os
import pandas as pd
import json
import warnings
warnings.filterwarnings('ignore')

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

try:
    from agent.backtest_engine import BacktestEngine
    from src.live_execution.strategies.execution_models import SingleModelStrategy
except ImportError:
    print("Cannot import BacktestEngine. Make sure script runs from project root.")
    sys.exit(1)

def run_compare():
    # Base config values like in ensemble4.json
    strategy_config = {
        "execution_class": "SingleModelStrategy",
        "tp_atr_mult": 2.5,
        "sl_atr_mult": 1.5,
        "trailing_atr_mult": 1.5,
        "tp_cooldown_bars": 0,
        "sl_cooldown_bars": 5,
        "max_hold_bars": 288,
        "allow_concurrent": False,
        "direction": "SHORT",
        "entry_threshold": 0.60
    }
    
    base_dir = r"c:\Users\bwang\Documents\GitHub\CL_Analyst_Development\reports"
    
    canaries = [
        ("Canary 2 (1.5x1 120H) LL", os.path.join(base_dir, "canary_exp2", "registry", "canary_output", "registry", "E2E_HourSet_02_short_logloss", "oos_predictions.csv")),
        ("Canary 3 (2.0x1 120H) AP", os.path.join(base_dir, "canary_exp3", "registry", "canary_output", "registry", "E2E_HourSet_02_short_average_precision", "oos_predictions.csv")),
        ("Canary 3 (2.0x1 120H) LL", os.path.join(base_dir, "canary_exp3", "registry", "canary_output", "registry", "E2E_HourSet_02_short_logloss", "oos_predictions.csv")),
        ("Canary 4 (2.5x1 120H) LL", os.path.join(base_dir, "canary_exp4", "registry", "canary_output", "registry", "E2E_HourSet_02_short_logloss", "oos_predictions.csv")),
        ("Canary 4 (2.5x1 120H) AP", os.path.join(base_dir, "canary_exp4", "registry", "canary_output", "registry", "E2E_HourSet_02_short_average_precision", "oos_predictions.csv")),
    ]
    
    print(f"{'Model':<25} | {'Orig Trades':>11} | {'Orig PnL':>10} | {'Orig PF':>8} || {'New Trades':>10} | {'New PnL':>10} | {'New PF':>8}")
    print("-" * 110)
    
    for name, path in canaries:
        if not os.path.exists(path):
            print(f"Skipping {name} - file not found at {path}")
            continue
            
        df = pd.read_csv(path, parse_dates=['DateTime'])
        if 'DateTime' in df.columns:
            df.set_index('DateTime', inplace=True)
            
        # Original (consecutive = 2)
        cfg_2 = strategy_config.copy()
        cfg_2["consecutive_signal_threshold"] = 2
        engine_orig = BacktestEngine.from_config(cfg_2)
        res_orig = engine_orig.run(df, df)
        
        # New (consecutive = 0)
        cfg_0 = strategy_config.copy()
        cfg_0["consecutive_signal_threshold"] = 0
        engine_new = BacktestEngine.from_config(cfg_0)
        res_new = engine_new.run(df, df)
        
        print(f"{name:<25} | {res_orig.trade_count:>11} | {res_orig.total_pnl:>10.2f} | {res_orig.profit_factor:>8.2f} || {res_new.trade_count:>10} | {res_new.total_pnl:>10.2f} | {res_new.profit_factor:>8.2f}")

if __name__ == '__main__':
    run_compare()
