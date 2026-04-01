import pandas as pd
import json
import joblib
from pathlib import Path
from src.data_paths import resolve_cli_path, PROJECT_ROOT

config_path = PROJECT_ROOT / 'configs/strategies/hourly_ensemble_003.json'
with open(config_path, 'r') as f: config = json.load(f)

print('Loading dataset...')
df = pd.read_parquet(resolve_cli_path('data/cl-1h_bk_HourSet_02.parquet'))
print(f'Data spans {df.index.min()} to {df.index.max()}')

for side, model_info in config.get('models', {}).items():
    pred_path_raw = model_info['predictions_path']
    pred_path = resolve_cli_path(pred_path_raw)
    
    # Try multiple common artifact paths
    model_dir = Path(pred_path).parent
    model_path = model_dir / 'model.pkl'
    if not model_path.exists():
        # Maybe it's in models/registry/<name> instead of reports/canary/...
        model_name = model_dir.name
        alt_path = PROJECT_ROOT / 'models' / 'registry' / model_name / 'model.pkl'
        if alt_path.exists(): model_path = alt_path

    print(f'\n--- {side.upper()} MODEL ---')
    print(f'Loading model from: {model_path}')
    if not model_path.exists():
        print(f'ERROR: Could not find model for {side}')
        continue
        
    model_obj = joblib.load(model_path)
    
    features = None
    if isinstance(model_obj, dict):
        model = model_obj['model']
        features = model_obj.get('features')
    else:
        model = model_obj
        feat_path = model_dir / 'features.json'
        if not feat_path.exists(): 
            feat_path = model_path.parent / 'features.json'
        
        if feat_path.exists():
            with open(feat_path, 'r') as f: features = json.load(f)
        else:
            feat_imp = model_dir / 'feature_importance.csv'
            if not feat_imp.exists(): 
                feat_imp = model_path.parent / 'feature_importance.csv'
            if feat_imp.exists():
                features = pd.read_csv(feat_imp)['Feature'].tolist()

    if not features:
        print('ERROR: Could not find feature list!')
        continue
        
    print(f'Found {len(features)} required features')
    missing = [f for f in features if f not in df.columns]
    if missing:
        print(f'WARNING: Missing {len(missing)} features in df, filling with 0')
        for m in missing: df[m] = 0.0

    df_pred = df[features].copy()
    initial_len = len(df_pred)
    df_pred = df_pred.dropna(how='any')
    print(f'Dropped {initial_len - len(df_pred)} rows with NaNs')
    
    print('Generating predictions...')
    # Use the appropriate proba index (usually index 1 for the positive class)
    preds = model.predict_proba(df_pred)[:, 1]
    
    prob_col = 'prob_buy' if side == 'long' else 'prob_sell'
    res_df = pd.DataFrame({prob_col: preds}, index=df_pred.index)
    res_df.to_csv(pred_path)
    print(f'Saved {len(res_df)} predictions to {pred_path} ending {res_df.index.max()}')

print('\nAll inference completed.')
