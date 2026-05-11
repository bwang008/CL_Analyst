import joblib

model_path = r"c:\Users\bwang\Documents\GitHub\CL_Analyst_Development\reports\hourset07_artifacts\canary_output\registry\E2E_HourSet_07_short_logloss\final_model.pkl"
try:
    m = joblib.load(model_path)
    print("Type:", type(m))
    print("best_iteration_:", getattr(m, "best_iteration_", "Not found"))
    if hasattr(m, "booster_"):
        print("Total Trees (booster):", m.booster_.num_trees())
    elif hasattr(m, "n_estimators"):
        print("n_estimators attribute:", m.n_estimators)
except Exception as e:
    print("Error:", e)
