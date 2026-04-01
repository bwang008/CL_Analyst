import joblib, json, os, pickle

out = []

def inspect_pkl(path, label):
    out.append(f"=== {label} ===")
    out.append(f"path: {path}")
    # Try joblib first, then pickle
    try:
        payload = joblib.load(path)
    except Exception as e:
        out.append(f"joblib failed: {e}")
        return
    out.append(f"type: {type(payload)}")
    if isinstance(payload, dict):
        out.append(f"keys: {list(payload.keys())}")
        if 'params' in payload:
            out.append(f"params: {json.dumps(payload['params'], indent=2, default=str)}")
        if 'model' in payload:
            m = payload['model']
            out.append(f"model type: {type(m)}")
            out.append(f"model num_trees: {m.num_trees()}")
            out.append(f"model num_feature: {m.num_feature()}")
            out.append(f"model feature_names sample: {m.feature_name()[:5]}")
        if 'feature_names' in payload:
            out.append(f"feature_names count: {len(payload['feature_names'])}")
        for k in ['n_features_in_', 'target_col', 'cutoff', 'balance_mode']:
            if k in payload:
                out.append(f"{k}: {payload[k]}")
    else:
        # Raw Booster
        import lightgbm as lgb
        if isinstance(payload, lgb.Booster):
            out.append(f"num_trees: {payload.num_trees()}")
            out.append(f"num_feature: {payload.num_feature()}")
            out.append(f"feature_names sample: {payload.feature_name()[:5]}")
            # Try to get params
            try:
                p = payload.params
                out.append(f"booster params: {json.dumps(p, indent=2, default=str)}")
            except Exception as e:
                out.append(f"params error: {e}")
    out.append("")

# Long model
inspect_pkl(
    'reports/canary/registry/canary_output/registry/E2E_HourSet_02_long_average_precision/final_model.pkl',
    'LONG (average_precision)'
)

# Also check experiment_config for long
cfg_path = 'reports/canary/registry/canary_output/registry/E2E_HourSet_02_long_average_precision/experiment_config.json'
with open(cfg_path) as f:
    cfg = json.load(f)
out.append("=== LONG experiment_config.json ===")
for k, v in cfg.items():
    if k != 'model_params':
        out.append(f"  {k}: {v}")
    else:
        out.append(f"  model_params: {json.dumps(v, indent=4, default=str)}")
out.append("")

# Short model
inspect_pkl(
    'models/registry/HourSet_02_2p5x1_120H_short_logloss/final_model.pkl',
    'SHORT (logloss)'
)

# Check short dir for config
short_dir = 'models/registry/HourSet_02_2p5x1_120H_short_logloss'
for fname in os.listdir(short_dir):
    if fname.endswith('.json'):
        with open(os.path.join(short_dir, fname)) as fh:
            cfg2 = json.load(fh)
        out.append(f"=== SHORT {fname} ===")
        for k, v in cfg2.items():
            out.append(f"  {k}: {json.dumps(v, default=str) if isinstance(v, dict) else v}")
        out.append("")

report = "\n".join(out)
with open("tmp/model_metadata.txt", "w", encoding="utf-8") as f:
    f.write(report)
print("done -> tmp/model_metadata.txt")
