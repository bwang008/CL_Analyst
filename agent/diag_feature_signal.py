"""Diagnostic: Compare feature signal strength for 72H vs 12H targets."""
import pandas as pd
import numpy as np
from scipy.stats import pointbiserialr

df = pd.read_parquet(r'C:\CL_Analyst_Data\data\processed\cl-1h_bk_HourSet_06.parquet')

feats = [c for c in df.columns if not c.startswith(('RAW_', 'TARGET_', 'META_'))
         and c not in {'Target', 'Open', 'High', 'Low', 'Close', 'Volume', 'DateTime'}]

t72 = 'TARGET_TRIPLE_2p0x1_72H_LONG'
t12 = 'TARGET_TRIPLE_2x1_12H_LONG'
t72s = 'TARGET_TRIPLE_2p0x1_72H_SHORT'
t12s = 'TARGET_TRIPLE_2x1_12H_SHORT'

train = df[df.index < pd.Timestamp('2022-01-01')].copy()
holdout = df[df.index >= pd.Timestamp('2022-01-01')].copy()

def target_stats(data, col):
    clean = data.dropna(subset=[col])
    y = clean[col].astype(int)
    pos_rate = y.mean()
    return len(clean), pos_rate

print("=== Target Statistics ===")
print(f"{'Target':<40} {'Train N':>10} {'Pos Rate':>10} {'Hold N':>10} {'Pos Rate':>10}")
print("-"*80)
for t in [t72, t72s, t12, t12s]:
    tn, tr = target_stats(train, t)
    hn, hr = target_stats(holdout, t)
    print(f"{t:<40} {tn:>10,} {tr:>9.1%} {hn:>10,} {hr:>9.1%}")

print()
print("=== Top Feature Correlations (72H LONG vs 12H LONG) ===")

rng = np.random.default_rng(42)
train72 = train.dropna(subset=[t72])
train12 = train.dropna(subset=[t12])

n = 15000
idx72 = rng.choice(len(train72), min(n, len(train72)), replace=False)
idx12 = rng.choice(len(train12), min(n, len(train12)), replace=False)

X72 = train72[feats].fillna(0).values[idx72]
X12 = train12[feats].fillna(0).values[idx12]
y72 = train72[t72].astype(int).values[idx72]
y12 = train12[t12].astype(int).values[idx12]

corrs = []
for i, f in enumerate(feats):
    try:
        c72, p72 = pointbiserialr(X72[:, i], y72)
        c12, p12 = pointbiserialr(X12[:, i], y12)
        corrs.append((f, c72, p72, c12, p12))
    except Exception:
        corrs.append((f, 0.0, 1.0, 0.0, 1.0))

corrs_72 = sorted(corrs, key=lambda x: -abs(x[1]))[:20]
print(f"{'Feature':<38} {'Corr_72H':>9} {'p_72H':>9} {'Corr_12H':>9} {'p_12H':>9}")
print("-"*80)
for f, c72, p72, c12, p12 in corrs_72:
    sig72 = "*" if p72 < 0.01 else " "
    sig12 = "*" if p12 < 0.01 else " "
    print(f"{f:<38} {c72:>8.4f}{sig72} {p72:>9.4f} {c12:>8.4f}{sig12} {p12:>9.4f}")

# Summary: count significant correlations
n_sig_72 = sum(1 for _, _, p72, _, _ in corrs if p72 < 0.01)
n_sig_12 = sum(1 for _, _, _, _, p12 in corrs if p12 < 0.01)
print()
print(f"Features with p<0.01 (72H): {n_sig_72}/{len(feats)}")
print(f"Features with p<0.01 (12H): {n_sig_12}/{len(feats)}")

# Also check OOS predictions from last run
print()
print("=== OOS Prediction Distribution (from last 72H run) ===")
import os
oos_path = r'reports\scout_hourset06_unbucketed_opt_v2\registry\canary_output\oos_predictions_long_logloss.csv'
if os.path.exists(oos_path):
    oos = pd.read_csv(oos_path, index_col=0, parse_dates=True)
    probs = oos['prob_Buy']
    print(f"72H Long OOS: mean={probs.mean():.4f}, std={probs.std():.4f}, max={probs.max():.4f}")
    print(f"  Threshold > 0.55: {(probs > 0.55).sum()} / {len(probs)}")
    print(f"  Threshold > 0.60: {(probs > 0.60).sum()} / {len(probs)}")

# HourSet_03 OOS preds for comparison
oos_03_path = r'models\registry\E2E_HourSet_03_long_average_precision\oos_predictions.csv'
if os.path.exists(oos_03_path):
    oos03 = pd.read_csv(oos_03_path, index_col=0, parse_dates=True)
    probs03 = oos03['prob_Buy']
    print(f"72H Long OOS (HourSet_03 baseline): mean={probs03.mean():.4f}, std={probs03.std():.4f}, max={probs03.max():.4f}")
    print(f"  Threshold > 0.55: {(probs03 > 0.55).sum()} / {len(probs03)}")
    print(f"  Threshold > 0.60: {(probs03 > 0.60).sum()} / {len(probs03)}")
