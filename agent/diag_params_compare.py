"""
Diagnostic: What study params did the winning scout_local_e2e run use?
And how did the model params compare to the cloud scout runs?
"""
import json, os

print("=== scout_local_e2e study source ===")
# Check for any study DB files in the reports/scout_local_e2e directory
scout_local_dir = r'reports\scout_local_e2e'
for root, dirs, files in os.walk(scout_local_dir):
    for fn in files:
        print(os.path.join(root, fn))

print()
print("=== Optuna study DBs locally ===")
db_dir = r'models\optuna_studies'
if os.path.exists(db_dir):
    for fn in sorted(os.listdir(db_dir)):
        fp = os.path.join(db_dir, fn)
        size_kb = os.path.getsize(fp) // 1024
        print(f"  {fn} ({size_kb} KB)")

print()
print("=== Model params comparison: local vs cloud ===")
configs = {
    "scout_local_e2e (PF 1.19)": r"reports\scout_local_e2e\registry\E2E_HourSet_06_short_logloss\experiment_config.json",
    "scout_hourset06_unbucketed_opt_v2 (PF 0.91)": r"reports\scout_hourset06_unbucketed_opt_v2\registry\canary_output\registry\E2E_HourSet_06_short_logloss\experiment_config.json",
    "scout_hourset06_2way_split_v1 (PF 0.84)": r"reports\scout_hourset06_2way_split_v1\registry\canary_output\registry\E2E_HourSet_06_short_logloss\experiment_config.json",
}
for label, path in configs.items():
    if os.path.exists(path):
        with open(path) as f:
            cfg = json.load(f)
        mp = cfg.get('model_params', {})
        print(f"\n  {label}:")
        for k in ['num_leaves', 'max_depth', 'learning_rate', 'feature_fraction',
                  'min_child_samples', 'reg_alpha', 'reg_lambda', 'bagging_fraction',
                  'n_estimators', 'lookback_window_years']:
            if k in mp:
                print(f"    {k}: {mp[k]}")
        prov = cfg.get('provenance', {})
        if prov:
            print(f"    provenance: {prov}")
    else:
        print(f"\n  {label}: FILE NOT FOUND ({path})")
