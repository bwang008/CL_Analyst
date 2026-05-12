"""Generate a target distribution report for HourSet_07."""
import argparse
import os
import pandas as pd
from collections import defaultdict
from pathlib import Path

def format_markdown_table(headers, rows):
    """Formats a list of headers and rows into a perfectly aligned Markdown table."""
    # Compute max widths for each column
    col_widths = [len(str(h)) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], len(str(cell)))
            
    # Format headers
    header_str = "| " + " | ".join(str(h).ljust(w) for h, w in zip(headers, col_widths)) + " |"
    sep_str = "|" + "|".join("-" * (w + 2) for w in col_widths) + "|"
    
    # Format rows
    table_lines = [header_str, sep_str]
    for row in rows:
        row_str = "| " + " | ".join(str(cell).ljust(w) for cell, w in zip(row, col_widths)) + " |"
        table_lines.append(row_str)
        
    return "\n".join(table_lines)

def generate_report(data_path):
    dataset_name = Path(data_path).stem
    print(f"Loading {data_path}...")
    df = pd.read_parquet(data_path)
    target_cols = [c for c in df.columns if c.startswith("TARGET_") and ("LONG" in c or "SHORT" in c or "MULTI" in c)]

    # Filter to only binary (0/1) targets
    binary_targets = []
    for c in sorted(target_cols):
        uniq = sorted(df[c].dropna().unique().tolist())
        if set(uniq).issubset({0, 0.0, 1, 1.0}):
            binary_targets.append(c)

    # Also collect the MULTI (3-class) targets separately
    multi_targets = []
    for c in sorted(target_cols):
        uniq = sorted(df[c].dropna().unique().tolist())
        if set(uniq).issubset({0, 0.0, 1, 1.0, 2, 2.0}) and (2.0 in uniq or 2 in uniq):
            multi_targets.append(c)

    # Separate binary into LONG and SHORT for side-by-side comparison
    pairs = defaultdict(dict)
    for c in binary_targets:
        parts = c.replace("TARGET_TRIPLE_", "")
        if parts.endswith("_LONG"):
            key = parts.replace("_LONG", "")
            pairs[key]["long"] = c
        elif parts.endswith("_SHORT"):
            key = parts.replace("_SHORT", "")
            pairs[key]["short"] = c

    # Build Markdown Content
    md_lines = []
    md_lines.append(f"# {dataset_name} Target Distribution Report\n")
    md_lines.append(f"> Dataset: `{dataset_name}.parquet` — {len(df):,} rows\n")
    md_lines.append(f"> Total Binary Targets: {len(binary_targets)}")
    md_lines.append(f"> Total Multi-class Targets: {len(multi_targets)}\n")

    # ---- Binary Targets ----
    md_lines.append("## Binary Target Distributions\n")
    
    bin_headers = ["Target", "Total", "True", "False", "True %", "Imbalance (F:T)"]
    bin_rows = []
    
    for c in binary_targets:
        total = df[c].dropna().shape[0]
        n_nan = int(df[c].isna().sum())
        n_true = int((df[c] == 1).sum())
        n_false = int((df[c] == 0).sum())
        pct_true = n_true / total * 100 if total > 0 else 0
        ratio = n_false / n_true if n_true > 0 else float("inf")
        
        # Emphasize severe imbalance or reverse imbalance
        ratio_str = f"{ratio:.1f}:1"
        if ratio < 1.0:
            ratio_str = f"**{ratio_str}** ⚠️"
        elif ratio > 10.0:
            ratio_str = f"**{ratio_str}** 🔴"
            
        pct_str = f"{pct_true:.1f}%"
        if pct_true > 50:
            pct_str = f"**{pct_str}**"
        elif pct_true < 10:
            pct_str = f"**{pct_str}**"
            
        bin_rows.append([f"`{c}`", f"{total:,}", f"{n_true:,}", f"{n_false:,}", pct_str, ratio_str])

    md_lines.append(format_markdown_table(bin_headers, bin_rows))

    md_lines.append("\n## Paired Long/Short Overlap\n")
    md_lines.append("This shows how often both Long AND Short are true simultaneously (conflicting signals):\n")
    
    pair_headers = ["Barrier Config", "L True", "L %", "S True", "S %", "Both True (Overlap)"]
    pair_rows = []

    for key in sorted(pairs.keys()):
        p = pairs[key]
        long_col = p.get("long")
        short_col = p.get("short")
        if not long_col or not short_col:
            continue
        
        mask = df[long_col].notna() & df[short_col].notna()
        sub = df[mask]
        total = len(sub)
        l_true = int((sub[long_col] == 1).sum())
        s_true = int((sub[short_col] == 1).sum())
        both = int(((sub[long_col] == 1) & (sub[short_col] == 1)).sum())
        l_pct = l_true / total * 100 if total > 0 else 0
        s_pct = s_true / total * 100 if total > 0 else 0
        both_pct = both / total * 100 if total > 0 else 0

        overlap_str = f"{both:,} ({both_pct:.1f}%)"
        if both_pct > 10.0:
            overlap_str = f"**{overlap_str}** ⚠️"

        pair_rows.append([key, f"{l_true:,}", f"{l_pct:.1f}%", f"{s_true:,}", f"{s_pct:.1f}%", overlap_str])

    md_lines.append(format_markdown_table(pair_headers, pair_rows))
    md_lines.append("\n> *Neither = bars where both Long AND Short are 0 (no trade opportunity)*\n")

    # ---- Multi-class Targets ----
    if multi_targets:
        md_lines.append("## Multi-Class Target Distributions\n")
        multi_headers = ["Target", "Total", "Class 0", "Class 1", "Class 2", "C0 %", "C1 %", "C2 %"]
        multi_rows = []
        
        for c in multi_targets:
            total = df[c].dropna().shape[0]
            n_nan = int(df[c].isna().sum())
            c0 = int((df[c] == 0).sum())
            c1 = int((df[c] == 1).sum())
            c2 = int((df[c] == 2).sum())
            p0 = c0 / total * 100 if total > 0 else 0
            p1 = c1 / total * 100 if total > 0 else 0
            p2 = c2 / total * 100 if total > 0 else 0
            
            multi_rows.append([f"`{c}`", f"{total:,}", f"{c0:,}", f"{c1:,}", f"{c2:,}", f"{p0:.1f}%", f"{p1:.1f}%", f"{p2:.1f}%"])

        md_lines.append(format_markdown_table(multi_headers, multi_rows))

    report_content = "\n".join(md_lines)
    
    os.makedirs("reports", exist_ok=True)
    report_path = os.path.join("reports", f"{dataset_name}_target_distribution_report.md")
    
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_content)
        
    print(f"Report generated successfully: {report_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate a markdown report of target distributions.")
    parser.add_argument("--data", required=True, help="Path to the processed parquet dataset (e.g. data/processed/CL_HourSet_07.parquet)")
    args = parser.parse_args()
    
    generate_report(args.data)
