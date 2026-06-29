import pandas as pd
import json

df = pd.read_parquet(r'C:\CL_Analyst_Data\data\processed\CL_HourSet_14B.parquet')

cols_to_drop = []
for col in df.columns:
    if col.startswith("VOLFLOW_DIVERGENCE_"): cols_to_drop.append(col)
    elif col.startswith("MACRO_POS_"): cols_to_drop.append(col)
    elif col.startswith("MOM_STOCH_K_") or col.startswith("MOM_STOCH_D_"): cols_to_drop.append(col)
    elif col.startswith("VOL_PARK_") or col.startswith("VOL_RS_"): cols_to_drop.append(col)
    elif col == "DIST_ZSCORE_72": cols_to_drop.append(col)
    elif col.startswith("TREND_LR_SLOPE_") and col not in ("TREND_LR_SLOPE_24", "TREND_LR_SLOPE_72"): cols_to_drop.append(col)
    elif col.startswith("LIQ_CORWIN_") and col not in ("LIQ_CORWIN_24", "LIQ_CORWIN_72"): cols_to_drop.append(col)
    elif col.startswith("TS_"):
        if "STOCH_K" in col or "VOL_PARK" in col: cols_to_drop.append(col)
        elif "VOL_YZ" in col or "CORWIN" in col:
            if "DIFF" in col or "INVERT" in col: cols_to_drop.append(col)
            elif "RATIO" in col and "LOG_RATIO" not in col: cols_to_drop.append(col)
        elif "DONCHIAN" in col or "EFFICIENCY" in col:
            if "RATIO" in col or "INVERT" in col: cols_to_drop.append(col)
        elif "LR_SLOPE" in col or "VWAP_DIST" in col or "CMF" in col:
            if "SIGN_AGREE" in col: cols_to_drop.append(col)

cols_to_drop = sorted(list(set(cols_to_drop)))

with open('configs/master/DataMap_CL_HourSet_14B.json', 'r') as f:
    config = json.load(f)

config['data_workflow']['features']['drop_features'] = cols_to_drop

with open('configs/master/DataMap_CL_HourSet_14B.json', 'w') as f:
    json.dump(config, f, indent=2)

print(f"Successfully wrote {len(cols_to_drop)} explicit features to drop_features in DataMap_CL_HourSet_14B.json")
