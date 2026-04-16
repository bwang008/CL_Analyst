import pandas as pd
oos = pd.read_csv(r"models\registry\E2E_HourSet_03_long_average_precision\oos_predictions.csv")
above = (oos["prob_Buy"] >= 0.55).sum()
print(f"OOS rows above 0.55 threshold: {above} / {len(oos)} ({100*above/len(oos):.1f}%)")
print(f"Max prob_Buy: {oos['prob_Buy'].max():.4f}")
print(f"Mean prob_Buy: {oos['prob_Buy'].mean():.4f}")

# Now compare to live
print("\n--- Live buy probs ---")
print(f"Live max prob_buy: 0.4970  (from telemetry)")
print(f"Live mean prob_buy: 0.4620")
print(f"OOS mean prob_Buy: {oos['prob_Buy'].mean():.4f}")
print(f"\nDelta in means: {oos['prob_Buy'].mean() - 0.4620:.4f}")
