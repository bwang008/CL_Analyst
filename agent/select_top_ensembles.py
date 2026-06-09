import os
import re
import json
import math
import argparse
import pandas as pd
import sys

def parse_markdown_table(md_path):
    # Parse markdown table to pandas dataframe
    with open(md_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
        
    # Find start of table
    table_lines = []
    in_table = False
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith('|') and 'Long Model' in line:
            in_table = True
            table_lines.append(line)
        elif in_table and line.startswith('|'):
            # Skip separator line
            if set(line.strip('|').replace('-', '').replace(' ', '').replace(':', '')) == set():
                continue
            table_lines.append(line)
            
    if not table_lines:
        return pd.DataFrame()
        
    # Process lines
    rows = []
    headers = [col.strip() for col in table_lines[0].strip('|').split('|')]
    
    for line in table_lines[1:]:
        cols = [col.strip() for col in line.strip('|').split('|')]
        rows.append(cols)
        
    df = pd.DataFrame(rows, columns=headers)
    return df

def main():
    parser = argparse.ArgumentParser(description="Select Top 8 Ensembles from Markdown")
    parser.add_argument("--md-report", required=True, help="Path to batch_ensemble_pre_opt.md")
    parser.add_argument("--output-json", required=True, help="Path to output top_8_ensembles.json")
    parser.add_argument("--top-n", type=int, default=8, help="Number of ensembles to select")
    args = parser.parse_args()

    if not os.path.exists(args.md_report):
        print(f"Error: {args.md_report} not found.")
        return

    df = parse_markdown_table(args.md_report)
    if df.empty:
        print("Error: Could not parse any data from the markdown table.")
        return

    # Columns from sweep_ensembles.py DataFrame: 'Long Model', 'Short Model', 'Trades', 'Win Rate', 'Profit Factor', 'Net PnL', 'Max DD', 'Tail PnL'
    # Also handle legacy abbreviated names: 'Trds', 'WR%', 'PF'
    # Parse 'Trds' to int, 'Net PnL' to float
    
    parsed_data = []
    for _, row in df.iterrows():
        long_model = row['Long Model']
        short_model = row['Short Model']
        
        try:
            trades_col = 'Trades' if 'Trades' in row.index else 'Trds'
            trades = int(row[trades_col])
        except (ValueError, KeyError):
            trades = 0

        pnl_col = 'Net PnL' if 'Net PnL' in row.index else 'PnL'
        pnl_str = str(row[pnl_col]).replace(',', '').replace('$', '')
        try:
            pnl = float(pnl_str)
        except ValueError:
            pnl = 0.0
            
        parsed_data.append({
            'long_prefix': long_model,
            'short_prefix': short_model,
            'trades': trades,
            'pnl': pnl
        })
        
    pdf = pd.DataFrame(parsed_data)
    
    # 1. Filter: Drop any combinations where Holdout_Trade_Count < 30 or Total_PnL <= 0.
    filtered_df = pdf[(pdf['trades'] >= 30) & (pdf['pnl'] > 0)].copy()
    
    # 2. Score & Sort: Create a new column called Obj_Score calculated as: Total_PnL * math.sqrt(Holdout_Trade_Count).
    filtered_df['obj_score'] = filtered_df['pnl'] * filtered_df['trades'].apply(math.sqrt)
    
    # Sort the remaining models descending by Obj_Score and select the Top 8.
    sorted_df = filtered_df.sort_values(by='obj_score', ascending=False)
    
    top_n = sorted_df.head(args.top_n)
    
    if top_n.empty:
        print("CRITICAL: No valid ensembles found!")
        sys.exit(1)
    
    output_data = []
    for _, row in top_n.iterrows():
        output_data.append({
            "target_long": row['long_prefix'],
            "target_short": row['short_prefix'],
            "trades": row['trades'],
            "pnl": row['pnl'],
            "obj_score": row['obj_score']
        })
        
    print(f"Selected Top {len(output_data)} ensembles.")
    for rank, entry in enumerate(output_data, 1):
        print(f"{rank}. Long: {entry['target_long']} | Short: {entry['target_short']} | Trades: {entry['trades']} | PnL: {entry['pnl']:.2f} | Score: {entry['obj_score']:.2f}")

    os.makedirs(os.path.dirname(os.path.abspath(args.output_json)), exist_ok=True)
    with open(args.output_json, 'w') as f:
        json.dump(output_data, f, indent=4)
        
    print(f"Saved to {args.output_json}")

if __name__ == "__main__":
    main()
