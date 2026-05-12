"""Quick test: what happens if we invert the signals on a losing model?"""
import pandas as pd
import json, os, sys
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from agent.backtest_engine import BacktestEngine, load_ohlcv, load_predictions

# Load data
ohlcv = load_ohlcv('data/processed/CL_HourSet_07.parquet')
canary_dir = 'reports/scout_hs07_1x2_6h_20260510_1518/registry/canary_output'
config_path = os.path.join(canary_dir, 'ensemble_config_logloss.json')
pred_path = os.path.join(canary_dir, 'oos_predictions_ensemble_logloss.csv')

with open(config_path, 'r', encoding='utf-8-sig') as f:
    cfg = json.load(f)

preds = load_predictions(pred_path)

# --- BASELINE (normal) ---
be = BacktestEngine.from_config(cfg)
res_normal = be.run(preds, ohlcv)
print("=== BASELINE (Normal) ===")
print(f"  Trades: {res_normal.trade_count}")
print(f"  WR:     {res_normal.win_rate*100:.1f}%")
print(f"  PF:     {res_normal.profit_factor:.4f}")
print(f"  PnL:    ${res_normal.total_pnl:,.2f}")
print(f"  Max DD: ${res_normal.max_drawdown:,.2f}")
print()

# --- INVERTED: swap prob_Buy and prob_Sell ---
preds_inv = preds.copy()
if 'prob_Buy' in preds_inv.columns and 'prob_Sell' in preds_inv.columns:
    preds_inv['prob_Buy'], preds_inv['prob_Sell'] = preds['prob_Sell'].copy(), preds['prob_Buy'].copy()

be2 = BacktestEngine.from_config(cfg)
res_inv = be2.run(preds_inv, ohlcv)
print("=== INVERTED (Buy<->Sell swapped) ===")
print(f"  Trades: {res_inv.trade_count}")
print(f"  WR:     {res_inv.win_rate*100:.1f}%")
print(f"  PF:     {res_inv.profit_factor:.4f}")
print(f"  PnL:    ${res_inv.total_pnl:,.2f}")
print(f"  Max DD: ${res_inv.max_drawdown:,.2f}")
print()

delta_pnl = res_inv.total_pnl - res_normal.total_pnl
print("=== DELTA ===")
print(f"  PnL change: ${delta_pnl:+,.2f}")
print(f"  WR change:  {(res_inv.win_rate - res_normal.win_rate)*100:+.1f}pp")
