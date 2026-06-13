import argparse
import json
import os
import sys
import pandas as pd

# Ensure we can import from scripts
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.extract_feature_importance import process_model

REGISTRY_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "models", "registry",
)

REPORTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "reports",
)


def get_feature_importance(experiment_id):
    """Retrieve feature importance dataframe for an experiment, generating it if necessary."""
    model_dir = os.path.join(REGISTRY_DIR, experiment_id)
    csv_path = os.path.join(model_dir, "feature_importance.csv")

    if not os.path.exists(csv_path):
        print(f"[{experiment_id}] CSV not found. Extracting from model...")
        df = process_model(experiment_id, top_n=1000, save=True)
        if df is None:
            print(f"[{experiment_id}] Failed to extract feature importance.")
            return None
        return df

    # Read existing CSV
    df = pd.read_csv(csv_path)
    # Ensure standard columns
    if "mean_importance" in df.columns:
        df = df.rename(columns={"mean_importance": "gain"})
    elif "importance" in df.columns:
        df = df.rename(columns={"importance": "gain"})
    
    df = df.sort_values("gain", ascending=False).reset_index(drop=True)
    df.index += 1
    df.index.name = "rank"
    return df


def generate_markdown_table(df, title, limit=None):
    """Generate a markdown table from the feature importance dataframe."""
    if df is None or df.empty:
        return f"### {title}\n*No data available*\n"

    if limit and len(df) > limit:
        # Show top N and bottom N
        top_df = df.head(limit)
        bottom_df = df.tail(10)
        
        md = f"### {title} (Top {limit})\n\n"
        md += "| Rank | Feature | Importance (Gain) |\n|---|---|---|\n"
        for rank, row in top_df.iterrows():
            md += f"| {rank} | `{row['feature']}` | {row['gain']:.2f} |\n"
            
        md += f"\n### {title} (Bottom 10)\n\n"
        md += "| Rank | Feature | Importance (Gain) |\n|---|---|---|\n"
        for rank, row in bottom_df.iterrows():
            md += f"| {rank} | `{row['feature']}` | {row['gain']:.2f} |\n"
        return md
    else:
        # Show all
        md = f"### {title}\n\n"
        md += "| Rank | Feature | Importance (Gain) |\n|---|---|---|\n"
        for rank, row in df.iterrows():
            md += f"| {rank} | `{row['feature']}` | {row['gain']:.2f} |\n"
        return md


def process_config(config_path, output_name):
    """Process a strategy configuration JSON file."""
    with open(config_path, "r") as f:
        config = json.load(f)

    long_id = None
    short_id = None

    if "models" in config:
        if "long" in config["models"]:
            long_id = config["models"]["long"].get("experiment_id")
        if "short" in config["models"]:
            short_id = config["models"]["short"].get("experiment_id")

    if not long_id and not short_id:
        print(f"Error: Could not find 'models.long.experiment_id' or 'models.short.experiment_id' in {config_path}")
        sys.exit(1)

    md_content = f"# Feature Importance Report: {output_name}\n\n"
    md_content += f"**Source Config:** `{os.path.basename(config_path)}`\n\n"

    if long_id:
        print(f"Processing Long Model: {long_id}")
        long_df = get_feature_importance(long_id)
        md_content += f"## Long Model: `{long_id}`\n\n"
        md_content += generate_markdown_table(long_df, "Long Features", limit=50)
        md_content += "\n---\n\n"

    if short_id:
        print(f"Processing Short Model: {short_id}")
        short_df = get_feature_importance(short_id)
        md_content += f"## Short Model: `{short_id}`\n\n"
        md_content += generate_markdown_table(short_df, "Short Features", limit=50)

    return md_content


def process_single_model(experiment_id, output_name):
    """Process a single model experiment ID."""
    print(f"Processing Model: {experiment_id}")
    df = get_feature_importance(experiment_id)
    
    md_content = f"# Feature Importance Report: {output_name}\n\n"
    md_content += f"**Model:** `{experiment_id}`\n\n"
    md_content += generate_markdown_table(df, "Features", limit=50)
    
    return md_content


def main():
    parser = argparse.ArgumentParser(description="Generate a Markdown feature importance report from a config or model.")
    parser.add_argument("target", help="Path to a config.json or a model experiment_id")
    args = parser.parse_args()

    target = args.target
    
    os.makedirs(REPORTS_DIR, exist_ok=True)

    if target.endswith(".json") and os.path.exists(target):
        output_name = os.path.splitext(os.path.basename(target))[0]
        md_content = process_config(target, output_name)
    else:
        output_name = target
        md_content = process_single_model(target, output_name)

    output_path = os.path.join(REPORTS_DIR, f"{output_name}_feature_importance_report.md")
    
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(md_content)

    print(f"\nSuccessfully generated report at: {output_path}")


if __name__ == "__main__":
    main()
