import pandas as pd

# Load the 468 column dataframe
df = pd.read_parquet(r'C:\CL_Analyst_Data\data\processed\CL_HourSet_14B.parquet')

cols_to_drop = []
for col in df.columns:
    # 1. Base Feature Pruning
    if col.startswith("VOLFLOW_DIVERGENCE_"): cols_to_drop.append(col)
    elif col.startswith("MACRO_POS_"): cols_to_drop.append(col)
    elif col.startswith("MOM_STOCH_K_") or col.startswith("MOM_STOCH_D_"): cols_to_drop.append(col)
    elif col.startswith("VOL_PARK_") or col.startswith("VOL_RS_"): cols_to_drop.append(col)
    elif col == "DIST_ZSCORE_72": cols_to_drop.append(col)
    elif col.startswith("TREND_LR_SLOPE_") and col not in ("TREND_LR_SLOPE_24", "TREND_LR_SLOPE_72"): cols_to_drop.append(col)
    elif col.startswith("LIQ_CORWIN_") and col not in ("LIQ_CORWIN_24", "LIQ_CORWIN_72"): cols_to_drop.append(col)
    
    # 2. Term Structure Pruning
    elif col.startswith("TS_"):
        if "STOCH_K" in col or "VOL_PARK" in col: cols_to_drop.append(col)
        elif "VOL_YZ" in col or "CORWIN" in col:
            if "DIFF" in col or "INVERT" in col: cols_to_drop.append(col)
            elif "RATIO" in col and "LOG_RATIO" not in col: cols_to_drop.append(col)
        elif "DONCHIAN" in col or "EFFICIENCY" in col:
            if "RATIO" in col or "INVERT" in col: cols_to_drop.append(col)
        elif "LR_SLOPE" in col or "VWAP_DIST" in col or "CMF" in col:
            if "SIGN_AGREE" in col: cols_to_drop.append(col)

cols_to_drop = list(set(cols_to_drop))
print(f"Legacy script would drop {len(cols_to_drop)} columns.")

# Let's read the JSON config and see what IT would drop
import json
with open('configs/master/DataMap_CL_HourSet_14B.json', 'r') as f:
    config = json.load(f)
    
json_drop_patterns = config['data_workflow']['features']['drop_features']

json_cols_to_drop = set()
for pattern in json_drop_patterns:
    if pattern.endswith('*'):
        prefix = pattern[:-1]
        json_cols_to_drop.update([c for c in df.columns if c.startswith(prefix)])
    else:
        if pattern in df.columns:
            json_cols_to_drop.add(pattern)
            
safe_to_drop = [c for c in json_cols_to_drop if c in df.columns and not c.startswith("TARGET_") and c not in ["Open", "High", "Low", "Close", "Volume", "DateTime"]]
json_cols_to_drop = list(set(safe_to_drop))

print(f"JSON config would drop {len(json_cols_to_drop)} columns.")

diff_missing_in_json = set(cols_to_drop) - set(json_cols_to_drop)
diff_extra_in_json = set(json_cols_to_drop) - set(cols_to_drop)
print(f"Missing in JSON ({len(diff_missing_in_json)}): {diff_missing_in_json}")
print(f"Extra in JSON ({len(diff_extra_in_json)}): {diff_extra_in_json}")

