"""
Probability Calibration Audit — Phase 1

Loads the E2E_HourSet_03_long 'pure' model, runs predict() on backtest
data, and compares to the OOS prob_Buy column to determine if the
live trader is applying the correct transformation.

Goal: Verify whether booster.predict() on _pure.txt returns RAW LOGITS
or calibrated probabilities, and identify the double-sigmoid / missing-
sigmoid bug.
"""

import sys, os
import numpy as np
import pandas as pd
import lightgbm as lgb

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

def _sigmoid(x):
    x = np.clip(np.asarray(x, dtype=float), -60, 60)
    return 1.0 / (1.0 + np.exp(-x))

# -------------------------------------------------------------------
# 1. Load the pure model
# -------------------------------------------------------------------
MODEL_PATH = "models/registry/E2E_HourSet_03_long_average_precision/final_model_pure.txt"
booster = lgb.Booster(model_file=MODEL_PATH)
print(f"Model: {MODEL_PATH}")
print(f"  Features: {booster.num_feature()}")
print(f"  Trees: {booster.num_trees()}")
feature_names = booster.feature_name()

# -------------------------------------------------------------------
# 2. Load OOS predictions (ground truth — sigmoid was applied at training time)
# -------------------------------------------------------------------
OOS_PATH = "models/registry/E2E_HourSet_03_long_average_precision/oos_predictions.csv"
oos_df = pd.read_csv(OOS_PATH, index_col=0, parse_dates=True)
print(f"\nOOS predictions: {len(oos_df):,} rows")
print(f"  prob_Buy range: [{oos_df['prob_Buy'].min():.6f}, {oos_df['prob_Buy'].max():.6f}]")
print(f"  prob_Buy mean: {oos_df['prob_Buy'].mean():.6f}")

# -------------------------------------------------------------------
# 3. Load backtest dataset (to get features for the same timestamps)
# -------------------------------------------------------------------
DATA_PATH = r"C:\CL_Analyst_Data\data\processed\cl-1h_bk_HourSet_03.parquet"
df = pd.read_parquet(DATA_PATH)
print(f"\nBacktest data: {len(df):,} rows")

# Align to OOS timestamps
overlap_idx = df.index.intersection(oos_df.index)
print(f"Overlapping timestamps: {len(overlap_idx):,}")

# Take first 100 and last 100 for sampling
sample_idx = overlap_idx[:100].append(overlap_idx[-100:])
sample_idx = sample_idx.unique()
print(f"Sample size: {len(sample_idx)}")

# -------------------------------------------------------------------
# 4. Run booster.predict() on the sample
# -------------------------------------------------------------------
X_sample = df.loc[sample_idx, feature_names]
raw_pred = booster.predict(X_sample)
raw_pred = np.asarray(raw_pred, dtype=float).ravel()

# -------------------------------------------------------------------
# 5. Compare raw predictions to OOS prob_Buy
# -------------------------------------------------------------------
oos_prob = oos_df.loc[sample_idx, "prob_Buy"].values

print("\n" + "=" * 70)
print("CALIBRATION AUDIT RESULTS")
print("=" * 70)

print("\n1. RAW booster.predict() output:")
print(f"   Range: [{raw_pred.min():.6f}, {raw_pred.max():.6f}]")
print(f"   Mean:  {raw_pred.mean():.6f}")
print(f"   Std:   {raw_pred.std():.6f}")

are_logits = raw_pred.min() < 0 or raw_pred.max() > 1
print(f"   Contains values outside [0,1]: {are_logits}")
print(f"   → booster.predict() returns: {'RAW LOGITS' if are_logits else 'PROBABILITIES (already sigmoided)'}")

print("\n2. OOS prob_Buy (ground truth, from training script):")
print(f"   Range: [{oos_prob.min():.6f}, {oos_prob.max():.6f}]")
print(f"   Mean:  {oos_prob.mean():.6f}")

# Test: is raw_pred ≈ oos_prob? (direct match means no transform needed)
direct_err = np.abs(raw_pred - oos_prob)
print(f"\n3. Direct match (raw_pred vs oos_prob):")
print(f"   Mean abs error: {direct_err.mean():.6f}")
print(f"   Max abs error:  {direct_err.max():.6f}")
print(f"   Match? {'YES' if direct_err.mean() < 0.001 else 'NO'}")

# Test: is sigmoid(raw_pred) ≈ oos_prob? (sigmoid needed)
sigmoid_pred = _sigmoid(raw_pred)
sigmoid_err = np.abs(sigmoid_pred - oos_prob)
print(f"\n4. Sigmoid match (sigmoid(raw_pred) vs oos_prob):")
print(f"   Mean abs error: {sigmoid_err.mean():.6f}")
print(f"   Max abs error:  {sigmoid_err.max():.6f}")
print(f"   Match? {'YES' if sigmoid_err.mean() < 0.001 else 'NO'}")

# Test: is sigmoid(sigmoid(raw_pred)) ≈ oos_prob? (double sigmoid scenario)
double_sigmoid = _sigmoid(sigmoid_pred)
double_err = np.abs(double_sigmoid - oos_prob)
print(f"\n5. Double sigmoid (sigmoid(sigmoid(raw_pred)) vs oos_prob):")
print(f"   Mean abs error: {double_err.mean():.6f}")
print(f"   Max abs error:  {double_err.max():.6f}")
print(f"   Match? {'YES' if double_err.mean() < 0.001 else 'NO'}")

# -------------------------------------------------------------------
# 6. Simulate what the live trader does (_run_inference logic)
# -------------------------------------------------------------------
print("\n" + "=" * 70)
print("LIVE TRADER SIMULATION")
print("=" * 70)

# configurable_strategy._run_inference line 337-343:
#   raw_pred = learner.model.predict(features)
#   raw_val = float(np.asarray(raw_pred).ravel()[0])
#   if raw_val < 0 or raw_val > 1:
#       return _sigmoid(raw_val)
#   return raw_val

live_probs = np.where(
    (raw_pred < 0) | (raw_pred > 1),
    _sigmoid(raw_pred),
    raw_pred,  # <-- THIS IS THE BUG: logits in [0,1] are NOT sigmoided
)

live_err = np.abs(live_probs - oos_prob)
print(f"\nLive trader _run_inference output:")
print(f"   Range: [{live_probs.min():.6f}, {live_probs.max():.6f}]")
print(f"   Mean:  {live_probs.mean():.6f}")
print(f"   Error vs OOS: {live_err.mean():.6f}")

# Count how many raw_pred values are in [0, 1] (where the bug bites)
in_01 = ((raw_pred >= 0) & (raw_pred <= 1)).sum()
print(f"\n   Predictions in [0,1] range (NOT sigmoided by _run_inference): {in_01} / {len(raw_pred)} ({in_01/len(raw_pred)*100:.1f}%)")

# Show the suppression effect
print(f"\n   OOS prob_Buy mean:        {oos_prob.mean():.4f}")
print(f"   Live _run_inference mean: {live_probs.mean():.4f}")
print(f"   Difference:               {oos_prob.mean() - live_probs.mean():.4f}")
print(f"   → This is the Train-Serve Skew!")

# -------------------------------------------------------------------
# 7. Correct fix: always apply sigmoid
# -------------------------------------------------------------------
correct_probs = _sigmoid(raw_pred)
correct_err = np.abs(correct_probs - oos_prob)
print(f"\n   Correct (always sigmoid):  {correct_probs.mean():.4f}")
print(f"   Error vs OOS:             {correct_err.mean():.6f}")

# Distribution comparison
print("\n" + "=" * 70)
print("DISTRIBUTION COMPARISON")
print("=" * 70)
for threshold in [0.50, 0.55, 0.60, 0.65, 0.70]:
    n_oos = (oos_prob >= threshold).sum()
    n_live = (live_probs >= threshold).sum()
    n_correct = (correct_probs >= threshold).sum()
    print(f"  >= {threshold:.2f}: OOS={n_oos:>4} | Live(buggy)={n_live:>4} | Correct(sigmoid)={n_correct:>4}")

print("\n" + "=" * 70)
print("DIAGNOSIS COMPLETE")
print("=" * 70)
