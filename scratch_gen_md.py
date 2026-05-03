import os
import json
import pandas as pd

base_dir = r"c:\Users\bwang\Documents\GitHub\CL_Analyst_Development\reports\scout_hourset06_72h_modern_artifacts\canary_output\registry"

def get_markdown_for_model(direction, folder_name):
    folder = os.path.join(base_dir, folder_name)
    config_path = os.path.join(folder, "experiment_config.json")
    fi_path = os.path.join(folder, "feature_importance.csv")
    
    with open(config_path, "r") as f:
        config = json.load(f)
        
    df = pd.read_csv(fi_path)
    
    md = f"## {direction.upper()} Model\n\n"
    md += f"**Dataset Used:** `{config.get('data_path')}`\n"
    md += f"**Target:** `{config.get('target_name')}`\n"
    md += f"**Train Cutoff Date:** `{config.get('train_cutoff_date')}`\n"
    md += f"**Optuna Source:** `{config.get('optuna_provenance', {}).get('source')}`\n\n"
    
    md += "### Full Feature Importance (by Gain)\n\n"
    md += "| Rank | Feature Name | Gain |\n"
    md += "|---|---|---|\n"
    
    for i, row in df.iterrows():
        md += f"| {i+1} | `{row['feature']}` | {row['importance']:.2f} |\n"
        
    return md

long_md = get_markdown_for_model("Long", "E2E_HourSet_06_long_logloss")
short_md = get_markdown_for_model("Short", "E2E_HourSet_06_short_logloss")

output_md = "# Feature Importance Audit\n\n" + long_md + "\n\n" + short_md

with open(r"c:\Users\bwang\Documents\GitHub\CL_Analyst_Development\reports\feature_importance_audit.md", "w") as f:
    f.write(output_md)
print("Artifact generated.")
