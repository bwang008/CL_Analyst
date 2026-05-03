"""
Deeper diagnostic: Compare the working HourSet_03 model vs current HourSet_06 models.
Key questions:
1. Are the HourSet_03 OOS probabilities actually better than 0.50 center? 
2. What TP/SL config was used in the "working" baseline (PF 1.10)?
3. How does the holdout probability distribution compare?
"""
import pandas as pd
import numpy as np
import os

# ----- 1. Load both OOS prediction sets -----
oos_06_path = r'reports\scout_hourset06_unbucketed_opt_v2\registry\canary_output\oos_predictions_long_logloss.csv'
oos_03_path = r'models\registry\E2E_HourSet_03_long_average_precision\oos_predictions.csv'

oos06 = pd.read_csv(oos_06_path, index_col=0, parse_dates=True) if os.path.exists(oos_06_path) else None
oos03 = pd.read_csv(oos_03_path, index_col=0, parse_dates=True) if os.path.exists(oos_03_path) else None

print("=== OOS Probability Distribution Comparison ===")
print()

for label, oos, prob_col in [
    ("HourSet_06 (72H, new scout)",  oos06, "prob_Buy"),
    ("HourSet_03 (72H, baseline)",   oos03, "prob_Buy"),
]:
    if oos is None:
        print(f"{label}: NOT FOUND")
        continue
    p = oos[prob_col]
    print(f"  {label}")
    print(f"    Rows: {len(p):,} | Date range: {oos.index.min().date()} -> {oos.index.max().date()}")
    print(f"    Mean: {p.mean():.4f} | Std: {p.std():.4f} | Median: {p.median():.4f}")
    print(f"    Min: {p.min():.4f} | Max: {p.max():.4f}")
    for thr in [0.50, 0.52, 0.55, 0.57, 0.60, 0.62, 0.65]:
        n = (p > thr).sum()
        pct = n/len(p)*100
        print(f"    > {thr:.2f}: {n:>5,} ({pct:.2f}%)")
    # y_true hit rate at each threshold
    if 'y_true' in oos.columns:
        y = oos['y_true']
        print(f"    y_true=1 rate overall: {(y==1).mean():.3f}")
        for thr in [0.50, 0.55, 0.60]:
            mask = p > thr
            if mask.sum() > 10:
                wr = (y[mask] == 1).mean()
                print(f"    Precision@{thr:.2f}: {wr:.3f} ({mask.sum():,} signals)")
    print()

# ----- 2. Check the "best" ensemble configuration from the working run -----
print("=== Working Baseline Strategy Config ===")
config_paths = [
    r'configs\strategies\hourly_ensemble_004.json',
    r'configs\strategies\hourly_ensemble_005.json',
]
import json
for cp in config_paths:
    if os.path.exists(cp):
        with open(cp) as f:
            cfg = json.load(f)
        print(f"\n  {os.path.basename(cp)}:")
        for k in ['tp_atr_mult', 'sl_atr_mult', 'entry_threshold', 'consecutive_signal_threshold', 'cooldown_bars']:
            print(f"    {k}: {cfg.get(k, 'N/A')}")
        if 'models' in cfg:
            for side, m in cfg['models'].items():
                print(f"    models.{side}.threshold: {m.get('threshold', 'N/A')}")

# ----- 3. Check the "reported" PF 1.10 run -----
print()
print("=== Checking previously reported PF 1.10 run ===")
summary_paths = [
    r'reports\scout_hourset06_unbucketed_opt_v2\registry\canary_output\pipeline_summary.json',
    r'reports\scout_hourset06_2way_split_v1\registry\canary_output\pipeline_summary.json',
]
for sp in summary_paths:
    if os.path.exists(sp):
        with open(sp) as f:
            s = json.load(f)
        print(f"\n  {sp.split(chr(92))[-3]}:")
        ens = s.get('backtest_results', {}).get('ensemble_logloss', {})
        print(f"    PF: {ens.get('profit_factor','N/A')} | PnL: {ens.get('total_pnl','N/A')} | Trades: {ens.get('trade_count','N/A')}")
        print(f"    Drawdown: {ens.get('max_drawdown','N/A')}")

# ----- 4. Check reports directory for ALL pipeline summaries -----
print()
print("=== All Experiment Results in reports/ ===")
for root, dirs, files in os.walk('reports'):
    for fn in files:
        if fn == 'pipeline_summary.json':
            fp = os.path.join(root, fn)
            with open(fp) as f:
                s = json.load(f)
            cutoff = s.get('train_cutoff_date', '?')
            ens = s.get('backtest_results', {}).get('ensemble_logloss', {})
            exp_name = fp.split(os.sep)[-3]
            pf = ens.get('profit_factor', '?')
            pnl = ens.get('total_pnl', '?')
            trades = ens.get('trade_count', '?')
            dd = ens.get('max_drawdown', '?')
            print(f"  [{exp_name}] cutoff={cutoff} | PF={pf} | PnL=${pnl:,.0f} | T={trades} | DD=${dd:,.0f}" if isinstance(pnl, (int,float)) else f"  [{exp_name}] {pf}")
