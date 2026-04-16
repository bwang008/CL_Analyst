"""
Sanitize HourSet_03 model PKLs by exporting the booster as a pure
LightGBM text file (final_model_pure.txt). LGBMLearner.load() will
automatically prefer these over the .pkl when present.
"""
import os, sys
import numpy as np
import joblib
import lightgbm as lgb

# Must define focal_obj here so joblib can deserialize the PKL
# (it was defined in __main__ when the retrain script ran)
FOCAL_GAMMA = 2.0

def _sigmoid(x):
    x = np.clip(np.asarray(x, dtype=float), -60, 60)
    return 1.0 / (1.0 + np.exp(-x))

def focal_obj(preds, train_set):
    labels = train_set.get_label().astype(int)
    p = _sigmoid(preds)
    p_t = np.where(labels == 1, p, 1 - p)
    grad = (p - labels) * ((1 - p_t) ** FOCAL_GAMMA)
    hess = (p * (1 - p)) * ((1 - p_t) ** FOCAL_GAMMA)
    return grad, hess

MODEL_DIRS = [
    r"reports/canary/registry/canary_output/registry/E2E_HourSet_03_long_average_precision",
    r"reports/canary/registry/canary_output/registry/E2E_HourSet_03_short_logloss",
]

for d in MODEL_DIRS:
    pkl_path = os.path.join(d, "final_model.pkl")
    txt_path = os.path.join(d, "final_model_pure.txt")

    print(f"\nProcessing: {os.path.basename(d)}")
    print(f"  PKL: {pkl_path}")

    data = joblib.load(pkl_path)
    booster = data["model"] if isinstance(data, dict) else data

    booster.save_model(txt_path)
    
    # Enforce LF line endings over CRLF. LightGBM C++ text parser relies on exact 
    # byte offsets to skip sections. CRLF causes format errors like 'met ge=0.00995528'
    with open(txt_path, "rb") as f:
        content = f.read()
    if b"\r\n" in content:
        with open(txt_path, "wb") as f:
            f.write(content.replace(b"\r\n", b"\n"))

    txt_size = os.path.getsize(txt_path)
    print(f"  Saved: {txt_path} ({txt_size/1024:.0f} KB)")
    print(f"  Trees: {booster.num_trees()} | Features: {booster.num_feature()}")

print("\nDone. Live trader will now load _pure.txt versions automatically.")
