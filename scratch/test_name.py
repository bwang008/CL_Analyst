import json
import re

def build_unique_name(label, direction, metric, progress_data):
    gcs_prefix = None
    for exp in progress_data.get("experiments", []):
        if exp["label"] == label:
            gcs_prefix = exp["gcs_prefix"]
            break
    if not gcs_prefix:
        return None
    
    match = re.search(r'HS(\d+)', label)
    hs_num = match.group(1) if match else "00"
    
    basename = f"E2E_HourSet_{hs_num}_{direction}_{metric}"
    return f"{gcs_prefix}_{basename}"

print(build_unique_name("HS11 3x1 12H", "long", "logloss", {"experiments": [{"label": "HS11 3x1 12H", "gcs_prefix": "sweep_hs11_3x1_12h_20260614_1923"}]}))
